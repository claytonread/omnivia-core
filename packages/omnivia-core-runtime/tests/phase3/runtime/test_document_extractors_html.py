"""The inert HTML walk: where text is located, in what order, and what it refuses.

Every case here is driven through the public ``extract_document`` entry point, so
what is asserted is the adapter's output and not a parser internal. The bound
cases shrink the module-level bound instead of growing the source, which keeps
the fixtures small enough to read and proves the refusal comes from the bound
rather than from anything the source happens to contain.
"""

from __future__ import annotations

import builtins
import socket
import subprocess
from typing import Any, Final

import pytest
from omnivia_core_runtime.service import document_extractors
from omnivia_core_runtime.service.document_extractors import (
    DocumentExtractionInvalid,
    extract_document,
)
from omnivia_core_runtime.service.document_intake import ExtractionOutcome

CAPTURE: Final = "dcap-" + "b" * 64
#: The checkpoint's case: an outer block whose words are interrupted by a block
#: nested inside a structural container.
NESTED: Final = b"<p>outer<section><p>inner</p></section>after</p>"
REFERENCES: Final = (
    b'<p>Go <a href="http://example.invalid/x">here</a> now <em>fast</em></p>'
    b'<p><img src="file:///etc/passwd" alt="Map"></p>'
    b'<p><a href="javascript:alert(1)">run</a>'
    b'<a href="data:text/plain;base64,QQ==">data</a>'
    b'<a href="#anchor">jump</a><a href="next.html">next</a></p>'
)


def html(source: bytes) -> ExtractionOutcome:
    return extract_document(source, media_type="text/html", capture_id=CAPTURE)


def texts(result: ExtractionOutcome) -> list[tuple[str, str]]:
    return [
        (item.semantic_role, item.text)
        for item in result.segments
        if item.text is not None
    ]


def locators(result: ExtractionOutcome) -> list[dict[str, Any]]:
    return [dict(item.source_locator) for item in result.segments]


def test_html_locates_text_by_dom_path_and_sibling_index() -> None:
    result = html(
        b'<div><p>first</p><br><p id="second">second</p>'
        b"<table><tr><td>cell</td></tr></table></div>"
    )

    # The path names each ancestor by its index among its parent's element
    # children, counting void elements, so the index of a later sibling moves
    # when markup is inserted before it and in no other case.
    assert locators(result) == [
        {
            "captureId": CAPTURE,
            "unit": "element",
            "element": "p",
            "path": "/1:div/1:p",
            "run": 1,
        },
        {
            "captureId": CAPTURE,
            "unit": "element",
            "element": "p",
            "path": "/1:div/3:p",
            "run": 1,
            "elementId": "second",
        },
        {
            "captureId": CAPTURE,
            "unit": "element",
            "element": "td",
            "path": "/1:div/4:table/1:tr/1:td",
            "run": 1,
            # A cell also keeps its place in its table's grid; that place is
            # checkpointed in full by ``test_html_table_structure.py``.
            "table": 1,
            "row": 1,
            "column": 1,
            "section": "direct",
            "cellKind": "td",
            "rowSpan": 1,
            "colSpan": 1,
            "headers": [],
        },
    ]
    # The same bytes locate the same text on every run, so the output digest a
    # revision is identified by does not move.
    repeated = html(
        b'<div><p>first</p><br><p id="second">second</p>'
        b"<table><tr><td>cell</td></tr></table></div>"
    )
    assert locators(repeated) == locators(result)
    assert repeated.output_digest == result.output_digest


def test_nested_block_splits_its_ancestor_into_ordered_runs() -> None:
    result = html(NESTED)

    # Source order, nothing repeated: the outer block's words before the nested
    # block, then the nested block's own words, then the words after it.
    assert texts(result) == [
        ("body", "outer"),
        ("body", "inner"),
        ("body", "after"),
    ]
    # The two runs of the one outer element share its path and are told apart by
    # their run index, which is what makes each run's locator unique.
    assert [(item["path"], item.get("run")) for item in locators(result)] == [
        ("/1:p", 1),
        ("/1:p/1:section/1:p", 1),
        ("/1:p", 2),
    ]


def test_nested_block_text_is_never_repeated_in_an_ancestor() -> None:
    result = html(NESTED)

    joined = "".join(text for _, text in texts(result))
    assert joined == "outerinnerafter"
    for word in ("outer", "inner", "after"):
        assert joined.count(word) == 1


@pytest.mark.parametrize(
    "container",
    [
        "div",
        "section",
        "article",
        "nav",
        "aside",
        "header",
        "footer",
        "main",
        "blockquote",
        "ul",
        "ol",
        "table",
        "tr",
    ],
)
def test_structural_containers_bound_text_attribution(container: str) -> None:
    source = f"<p>before<{container}><p>within</p></{container}>after</p>".encode()

    result = html(source)

    # The descendant block's words stay in their own segment: they neither join
    # the container's text nor reach the ancestor block's runs.
    assert texts(result) == [
        ("body", "before"),
        ("body", "within"),
        ("body", "after"),
    ]


def test_container_holds_its_own_loose_text_in_ordered_runs() -> None:
    result = html(b"<div>loose<ul><li>item</li></ul>tail</div>")

    assert texts(result) == [
        ("body", "loose"),
        ("list_item", "item"),
        ("body", "tail"),
    ]
    assert [(item["path"], item.get("run")) for item in locators(result)] == [
        ("/1:div", 1),
        ("/1:div/1:ul/1:li", 1),
        ("/1:div", 2),
    ]


def test_container_without_own_text_emits_no_segment() -> None:
    result = html(b"<section>\n  <p>only</p>\n</section>")

    # Whitespace between a container's children is not text, so a container that
    # only wraps blocks contributes nothing and numbers no run.
    assert texts(result) == [("body", "only")]


def test_inline_markup_keeps_source_order_within_one_block() -> None:
    result = html(REFERENCES)

    assert texts(result) == [
        ("body", "Go here now fast"),
        ("image_alt", "Map"),
        ("body", "rundatajumpnext"),
    ]


def test_link_and_image_targets_are_inventoried_without_resolution() -> None:
    result = html(REFERENCES)

    assert [
        (
            item.semantic_role,
            item.extraction_method,
            item.visibility,
            item.structured_value["kind"],
            item.structured_value["scheme"],
            item.structured_value["target"],
            item.structured_value["resolved"],
        )
        for item in result.segments
        if item.structured_value is not None
    ] == [
        (
            "object_inventory",
            "inventory",
            "annotation",
            "link",
            "http",
            "http://example.invalid/x",
            False,
        ),
        (
            "object_inventory",
            "inventory",
            "annotation",
            "image",
            "file",
            "file:///etc/passwd",
            False,
        ),
        (
            "object_inventory",
            "inventory",
            "annotation",
            "link",
            "javascript",
            "javascript:alert(1)",
            False,
        ),
        (
            "object_inventory",
            "inventory",
            "annotation",
            "link",
            "data",
            "data:text/plain;base64,QQ==",
            False,
        ),
        (
            "object_inventory",
            "inventory",
            "annotation",
            "link",
            "fragment",
            "#anchor",
            False,
        ),
        (
            "object_inventory",
            "inventory",
            "annotation",
            "link",
            "relative",
            "next.html",
            False,
        ),
    ]


def test_extraction_takes_no_external_action_for_any_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*arguments: object, **keywords: object) -> Any:
        raise AssertionError("the HTML walk must take no external action")

    monkeypatch.setattr(builtins, "open", refuse)
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)

    result = html(REFERENCES)

    # Nothing was opened, connected to, resolved or run: every target is carried
    # out as text only.
    assert all(
        item.structured_value["resolved"] is False
        for item in result.segments
        if item.structured_value is not None
    )


@pytest.mark.parametrize(
    ("bound", "value", "source", "reason"),
    [
        (
            "MAX_HTML_DEPTH",
            2,
            b"<div><div><div>deep</div></div></div>",
            "nesting exceeds",
        ),
        ("MAX_HTML_ELEMENTS", 2, b"<p>a</p><p>b</p><p>c</p>", "too many elements"),
        (
            "MAX_HTML_ATTRIBUTES",
            1,
            b'<p id="a" class="b">x</p>',
            "too many attributes",
        ),
        ("MAX_HTML_SEGMENT_CHARS", 4, b"<p>abcde</p>", "text exceeds"),
        (
            "MAX_HTML_REFERENCE_CHARS",
            4,
            b'<p><a href="/abcde">x</a></p>',
            "reference exceeds",
        ),
        ("MAX_HTML_SEGMENTS", 1, b"<p>a</p><p>b</p>", "too many segments"),
    ],
)
def test_html_bounds_refuse_the_source_rather_than_truncate_it(
    bound: str,
    value: int,
    source: bytes,
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(document_extractors, bound, value)

    with pytest.raises(DocumentExtractionInvalid, match=reason):
        html(source)

    # The same bytes are accepted at the packaged bound, so the refusal is the
    # bound speaking and not a defect in the fixture.
    monkeypatch.undo()
    assert html(source).state == "succeeded"
