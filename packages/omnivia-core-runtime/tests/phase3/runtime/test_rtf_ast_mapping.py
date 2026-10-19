"""The Pandoc JSON AST to canonical segment mapping for RTF sources.

Most cases feed a hand-built AST through a stubbed reader, so each one pins a
single node shape without depending on what the pinned reader chooses to emit.
Two cases run the real reader end to end. This module imports no OCR code, so
it stays runnable on a host without the raster/OCR dependencies.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from typing import Any

import pytest
from _trusted_ocr import HOST_PANDOC, reader_settings
from omnivia_core_runtime.service import rtf_extractor
from omnivia_core_runtime.service.rtf_extractor import (
    MAX_AST_DEPTH,
    RTFExtractionInvalid,
    extract_rtf,
)

CAPTURE = "dcap-" + "a" * 64
SOURCE = rb"{\rtf1\ansi body\par}"
ATTR: list[Any] = ["", [], []]

Ast = dict[str, Any]


def s(text: str) -> list[Ast]:
    """Inline nodes for ``text``: one Str per word, Space between them."""
    words = text.split(" ")
    inline: list[Ast] = []
    for index, word in enumerate(words):
        if index:
            inline.append({"t": "Space"})
        inline.append({"t": "Str", "c": word})
    return inline


def para(text: str) -> Ast:
    return {"t": "Para", "c": s(text)}


def plain(text: str) -> Ast:
    return {"t": "Plain", "c": s(text)}


def link(label: str, target: str, title: str = "") -> Ast:
    return {"t": "Link", "c": [ATTR, s(label), [target, title]]}


def span(classes: list[str], inline: list[Ast]) -> Ast:
    return {"t": "Span", "c": [["", classes, []], inline]}


def cell(*blocks: Ast) -> list[Any]:
    return [ATTR, {"t": "AlignDefault"}, 1, 1, list(blocks)]


def row(*cells: list[Any]) -> list[Any]:
    return [ATTR, list(cells)]


def table(head: list[list[Any]], body: list[list[Any]], foot: list[list[Any]]) -> Ast:
    return {
        "t": "Table",
        "c": [
            ATTR,
            [None, [para("Caption")]],
            [[{"t": "AlignDefault"}, {"t": "ColWidthDefault"}]] * 2,
            [ATTR, head],
            [[ATTR, 0, [], body]],
            [ATTR, foot],
        ],
    }


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> Callable[[list[Any]], Any]:
    """Return a function that extracts ``blocks`` as the reader's AST."""

    def extract(blocks: list[Any]) -> Any:
        document = json.dumps(
            {"pandoc-api-version": [1, 23, 1], "meta": {}, "blocks": blocks}
        )

        def reader(
            arguments: Any, **options: Any
        ) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(arguments, 0, document.encode(), b"")

        monkeypatch.setattr(rtf_extractor.subprocess, "run", reader)
        return extract_rtf(SOURCE, capture_id=CAPTURE, settings=reader_settings())

    return extract


def view(result: Any) -> list[tuple[str, str, str | None]]:
    return [
        (item.semantic_role, item.source_locator["path"], item.text)
        for item in result.segments
    ]


def test_nested_lists_keep_item_paths_and_roles(
    run: Callable[[list[Any]], Any],
) -> None:
    inner = {
        "t": "OrderedList",
        "c": [
            [1, {"t": "Decimal"}, {"t": "Period"}],
            [[plain("deep one")], [plain("deep two")]],
        ],
    }
    outer = {"t": "BulletList", "c": [[plain("first"), inner], [plain("second")]]}

    result = run([para("Intro"), outer])

    assert view(result) == [
        ("body", "blocks/1:Para", "Intro"),
        ("list_item", "blocks/2:BulletList/item/1/1:Plain", "first"),
        (
            "list_item",
            "blocks/2:BulletList/item/1/2:OrderedList/item/1/1:Plain",
            "deep one",
        ),
        (
            "list_item",
            "blocks/2:BulletList/item/1/2:OrderedList/item/2/1:Plain",
            "deep two",
        ),
        ("list_item", "blocks/2:BulletList/item/2/1:Plain", "second"),
    ]
    assert result.limitations["astInventory"] == {"lists": 2}


def test_headings_definitions_quotes_and_code(run: Callable[[list[Any]], Any]) -> None:
    result = run(
        [
            {"t": "Header", "c": [2, ATTR, s("Title")]},
            {"t": "BlockQuote", "c": [para("quoted")]},
            {"t": "DefinitionList", "c": [[s("term"), [[plain("meaning")]]]]},
            {"t": "CodeBlock", "c": [ATTR, "x = 1"]},
            {"t": "LineBlock", "c": [s("one"), [], s("three")]},
            {"t": "HorizontalRule"},
            {"t": "RawBlock", "c": ["rtf", "raw"]},
        ]
    )

    assert view(result) == [
        ("heading", "blocks/1:Header", "Title"),
        ("body", "blocks/2:BlockQuote/1:Para", "quoted"),
        ("list_item", "blocks/3:DefinitionList/term/1", "term"),
        ("list_item", "blocks/3:DefinitionList/def/1.1/1:Plain", "meaning"),
        ("body", "blocks/4:CodeBlock", "x = 1"),
        ("body", "blocks/5:LineBlock", "one\n\nthree"),
    ]
    assert result.limitations["astInventory"] == {"rawBlocks": 1}
    assert result.blocks == 7


def test_tables_map_every_cell_with_its_section_row_and_column(
    run: Callable[[list[Any]], Any],
) -> None:
    result = run(
        [
            table(
                [row(cell(plain("H1")), cell(plain("H2")))],
                [row(cell(plain("a"), plain("a2")), cell(plain("b")))],
                [row(cell(plain("F1")), cell())],
            )
        ]
    )

    assert view(result) == [
        ("body", "blocks/1:Table/caption/1:Para", "Caption"),
        ("table_cell", "blocks/1:Table/head/row/1/cell/1/1:Plain", "H1"),
        ("table_cell", "blocks/1:Table/head/row/1/cell/2/1:Plain", "H2"),
        ("table_cell", "blocks/1:Table/body/1/row/1/cell/1/1:Plain", "a"),
        ("table_cell", "blocks/1:Table/body/1/row/1/cell/1/2:Plain", "a2"),
        ("table_cell", "blocks/1:Table/body/1/row/1/cell/2/1:Plain", "b"),
        ("table_cell", "blocks/1:Table/foot/row/1/cell/1/1:Plain", "F1"),
    ]
    assert result.limitations["astInventory"] == {"tables": 1}


def test_link_text_stays_visible_and_the_target_is_inert_inventory(
    run: Callable[[list[Any]], Any],
) -> None:
    long_target = "https://example.invalid/" + "a" * 5000
    blocks = [
        {
            "t": "Para",
            "c": [
                *s("See"),
                {"t": "Space"},
                link("the site", "https://example.invalid/a?b=1", "Title"),
                {"t": "Space"},
                link("a file", "file:///etc/passwd"),
                {"t": "Space"},
                link("here", "#section"),
                {"t": "Space"},
                link("long", long_target),
            ],
        }
    ]
    result = run(blocks)

    first, *inventory = result.segments
    assert first.semantic_role == "body"
    assert first.text == "See the site a file here long"
    assert [item.semantic_role for item in inventory] == ["object_inventory"] * 4
    assert [item.source_locator["path"] for item in inventory] == [
        f"blocks/1:Para/link/{n}" for n in (1, 2, 3, 4)
    ]
    assert all(item.extraction_method == "inventory" for item in inventory)
    assert all(item.visibility == "annotation" for item in inventory)
    values = [item.structured_value for item in inventory]
    assert values[0] == {
        "kind": "link",
        "scheme": "https",
        "target": "https://example.invalid/a?b=1",
        "targetTruncated": False,
        "title": "Title",
        "resolved": False,
    }
    assert values[1]["scheme"] == "file"
    assert values[1]["resolved"] is False
    assert values[2]["scheme"] == "fragment"
    assert values[3]["targetTruncated"] is True
    assert len(values[3]["target"]) == rtf_extractor.MAX_TARGET_CHARS
    assert result.limitations["astInventory"] == {"links": 4}
    assert result.limitations["referenceSchemes"] == {
        "file": 1,
        "fragment": 1,
        "https": 2,
    }


def test_images_keep_alt_text_and_inventory_their_target(
    run: Callable[[list[Any]], Any],
) -> None:
    image = {"t": "Image", "c": [ATTR, s("a chart"), ["../secret.png", ""]]}

    result = run([{"t": "Para", "c": [image]}])

    assert view(result)[0] == ("body", "blocks/1:Para", "a chart")
    assert result.segments[1].structured_value["kind"] == "image"
    assert result.segments[1].structured_value["scheme"] == "relative"


def test_notes_become_annotation_segments_after_their_paragraph(
    run: Callable[[list[Any]], Any],
) -> None:
    inner_note = {"t": "Note", "c": [para("nested note")]}
    note = {
        "t": "Note",
        "c": [{"t": "Para", "c": [*s("first note"), inner_note]}, para("second block")],
    }
    blocks = [
        {
            "t": "Para",
            "c": [*s("Body"), note, *s("after"), {"t": "Note", "c": [para("other")]}],
        },
        para("tail"),
    ]

    result = run(blocks)

    assert view(result) == [
        ("body", "blocks/1:Para", "Bodyafter"),
        ("note", "blocks/1:Para/note/1/1:Para", "first note"),
        ("note", "blocks/1:Para/note/1/1:Para/note/1/1:Para", "nested note"),
        ("note", "blocks/1:Para/note/1/2:Para", "second block"),
        ("note", "blocks/1:Para/note/2/1:Para", "other"),
        ("body", "blocks/2:Para", "tail"),
    ]
    assert {
        item.visibility for item in result.segments if item.semantic_role == "note"
    } == {"annotation"}
    assert result.limitations["astInventory"] == {"notes": 3}


def test_revision_and_comment_spans_use_existing_roles(
    run: Callable[[list[Any]], Any],
) -> None:
    blocks = [
        {
            "t": "Para",
            "c": [
                *s("Keep"),
                span(["deletion"], s("gone")),
                span(["insertion"], s("added")),
                span(["comment-start"], s("a remark")),
                span(["comment-end"], []),
                span([], s("plain")),
            ],
        }
    ]

    result = run(blocks)

    assert view(result) == [
        ("body", "blocks/1:Para", "Keepaddedplain"),
        ("deleted_text", "blocks/1:Para/deletion/1", "gone"),
        ("comment", "blocks/1:Para/comment/1", "a remark"),
    ]
    deleted, comment = result.segments[1:]
    assert (deleted.visibility, deleted.revision_state) == ("annotation", "deleted")
    assert (comment.visibility, comment.revision_state) == ("annotation", "current")
    assert result.segments[0].revision_state == "current"


def test_repeated_extraction_has_identical_segments_and_order(
    run: Callable[[list[Any]], Any],
) -> None:
    blocks = [
        para("a"),
        {
            "t": "BulletList",
            "c": [
                [
                    {
                        "t": "Plain",
                        "c": [
                            link("x", "http://e.invalid/"),
                            {"t": "Note", "c": [para("n")]},
                        ],
                    }
                ]
            ],
        },
        table([], [row(cell(plain("c")), cell(plain("d")))], []),
    ]

    first = run(blocks)
    second = run(blocks)

    assert first.segments == second.segments
    assert first.limitations == second.limitations
    paths = [item.source_locator["path"] for item in first.segments]
    assert len(paths) == len(set(paths))
    assert all(
        item.source_locator
        == {
            "captureId": CAPTURE,
            "unit": "rtf_ast",
            "path": item.source_locator["path"],
        }
        for item in first.segments
    )


@pytest.mark.parametrize(
    "blocks",
    [
        [{"t": "Mystery", "c": []}],
        ["not a block"],
        [{"c": []}],
        [{"t": "Para", "c": "not a list"}],
        [{"t": "Para", "c": [{"t": "Mystery"}]}],
        [{"t": "Para", "c": [{"t": "Str", "c": 7}]}],
        [{"t": "Para", "c": [{"t": "Link", "c": [ATTR, s("x")]}]}],
        [{"t": "Para", "c": [{"t": "Link", "c": [ATTR, s("x"), ["u"]]}]}],
        [{"t": "Para", "c": [{"t": "Link", "c": [ATTR, s("x"), [1, ""]]}]}],
        [{"t": "Para", "c": [{"t": "Span", "c": [["", "cls", []], []]}]}],
        [{"t": "Header", "c": [1, ATTR]}],
        [{"t": "BulletList", "c": "x"}],
        [{"t": "OrderedList", "c": [1]}],
        [{"t": "Table", "c": [ATTR]}],
        [{"t": "Table", "c": [ATTR, [None, []], [], [ATTR, "x"], [], [ATTR, []]]}],
        [{"t": "Para", "c": [{"t": "Note", "c": "x"}]}],
    ],
)
def test_malformed_ast_is_refused_rather_than_skipped(
    run: Callable[[list[Any]], Any], blocks: list[Any]
) -> None:
    with pytest.raises(RTFExtractionInvalid):
        run(blocks)


def test_structure_beyond_the_depth_bound_is_refused(
    run: Callable[[list[Any]], Any],
) -> None:
    def quoted(depth: int) -> list[Any]:
        blocks: list[Any] = [para("leaf")]
        for _ in range(depth):
            blocks = [{"t": "BlockQuote", "c": blocks}]
        return blocks

    assert view(run(quoted(MAX_AST_DEPTH - 1)))
    with pytest.raises(RTFExtractionInvalid, match="exceeds"):
        run(quoted(MAX_AST_DEPTH + 1))


def test_a_node_budget_bounds_the_walk(
    run: Callable[[list[Any]], Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rtf_extractor, "MAX_AST_NODES", 10)

    with pytest.raises(RTFExtractionInvalid, match="exceeds"):
        run([para("one two three four five six seven eight")])


def test_a_non_object_document_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def reader(arguments: Any, **options: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            arguments, 0, b'{"blocks": {"t": "Para"}}', b""
        )

    monkeypatch.setattr(rtf_extractor.subprocess, "run", reader)

    with pytest.raises(RTFExtractionInvalid, match="block count"):
        extract_rtf(SOURCE, capture_id=CAPTURE, settings=reader_settings())


@pytest.mark.skipif(HOST_PANDOC is None, reason="no host RTF reader to bind")
def test_the_real_reader_maps_a_table_a_note_and_a_hyperlink_field() -> None:
    content = (
        rb"{\rtf1\ansi {\trowd\cellx1000\cellx2000\intbl a\cell b\cell\row}"
        rb"{\trowd\cellx1000\cellx2000\intbl c\cell d\cell\row}\pard "
        rb'See {\field{\*\fldinst HYPERLINK "http://example.invalid/x"}{\fldrslt site}}'
        rb"{\footnote note text} end\par}"
    )

    result = extract_rtf(content, capture_id=CAPTURE, settings=reader_settings())
    segments = view(result)

    cells = [text for role, _, text in segments if role == "table_cell"]
    assert cells == ["a", "b", "c", "d"]
    body = next(text for role, _, text in segments if role == "body")
    assert "site" in body and "example.invalid" not in body
    assert ("note", "note text") in [(role, text) for role, _, text in segments]
    targets = [
        item.structured_value
        for item in result.segments
        if item.semantic_role == "object_inventory"
    ]
    assert [(t["scheme"], t["resolved"]) for t in targets] == [("http", False)]
    again = extract_rtf(content, capture_id=CAPTURE, settings=reader_settings())
    assert again.segments == result.segments
