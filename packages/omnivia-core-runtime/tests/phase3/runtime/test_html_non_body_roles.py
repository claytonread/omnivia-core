"""The HTML roles that are not document body: comments and hidden content.

Every case here is driven through the public ``extract_document`` entry point,
so what is asserted is the adapter's published segment and not a parser
internal. What each case has to tell apart is a source the document presents as
prose from one it does not: a comment, a script or style body, template content,
text under the ``hidden`` attribute and text under an inline ``display:none`` or
``visibility:hidden`` declaration are all located and all kept out of the roles
a default retrieval reads, rather than being dropped unread or published as
body.
"""

from __future__ import annotations

import builtins
import socket
import subprocess
from typing import Any, Final

import pytest
from omnivia_core_runtime.service import document_extractors
from omnivia_core_runtime.service.document_extractors import (
    MAX_HTML_DEPTH,
    MAX_HTML_SEGMENT_CHARS,
    XHTML_MEDIA_TYPE,
    DocumentExtractionInvalid,
    extract_document,
)
from omnivia_core_runtime.service.document_intake import ExtractionOutcome
from omnivia_core_runtime.storage.document_intake import DEFAULT_RETRIEVAL_ROLES

CAPTURE: Final = "dcap-" + "d" * 64
#: One source carrying every non-body role beside the body prose that must stay
#: body, in source order: the fixture the role, order and equivalence cases read.
EVERY_ROLE: Final = (
    b"<p>prose</p>"
    b"<!-- note -->"
    b"<script>fetch('http://example.invalid/x')</script>"
    b"<style>p{color:red}</style>"
    b"<template><p>fragment</p></template>"
    b"<p hidden>attribute</p>"
    b'<p style="display:none">declaration</p>'
)


def html(source: bytes, *, media_type: str = "text/html") -> ExtractionOutcome:
    return extract_document(source, media_type=media_type, capture_id=CAPTURE)


def roles(result: ExtractionOutcome) -> list[tuple[str, str, str | None]]:
    """Each located segment as its role, its visibility and its text."""
    return [
        (item.semantic_role, item.visibility, item.text)
        for item in result.segments
        if item.text is not None
    ]


def paths(result: ExtractionOutcome) -> list[tuple[str, Any]]:
    return [
        (str(item.source_locator["path"]), item.source_locator.get("run"))
        for item in result.segments
    ]


def test_every_non_body_source_is_located_in_its_own_role() -> None:
    result = html(EVERY_ROLE)

    # Each one is published with the text it really holds, in the source order
    # it was written in, so nothing is dropped unread and nothing moves.
    assert roles(result) == [
        ("body", "visible", "prose"),
        ("comment", "annotation", " note "),
        ("hidden_content", "hidden", "fetch('http://example.invalid/x')"),
        ("hidden_content", "hidden", "p{color:red}"),
        ("hidden_content", "hidden", "fragment"),
        ("hidden_content", "hidden", "attribute"),
        ("hidden_content", "hidden", "declaration"),
    ]


def test_no_non_body_role_reaches_the_default_retrieval_roles() -> None:
    result = html(EVERY_ROLE)

    # ``body`` is the only role in this source a default retrieval reads, so
    # none of the hidden text or the comment can surface as document prose.
    assert [
        (role, text)
        for role, _, text in roles(result)
        if role in DEFAULT_RETRIEVAL_ROLES
    ] == [("body", "prose")]


def test_a_comment_keeps_its_place_among_the_words_around_it() -> None:
    result = html(b"<p>before<!-- aside -->after</p>")

    # The comment ends the block's pending run exactly as a nested block does,
    # which is what keeps the words written before it ahead of it.
    assert roles(result) == [
        ("body", "visible", "before"),
        ("comment", "annotation", " aside "),
        ("body", "visible", "after"),
    ]
    assert paths(result) == [("/1:p", 1), ("/1:p/1:#comment", None), ("/1:p", 2)]


def test_a_comment_takes_no_element_sibling_index() -> None:
    plain = html(b"<div><p>first</p><p>second</p></div>")
    commented = html(b"<div><!--a--><p>first</p><!--b--><p>second</p></div>")

    # Writing a comment beside an element must not move that element's locator,
    # so the comments are counted in a namespace of their own.
    assert paths(plain) == [("/1:div/1:p", 1), ("/1:div/2:p", 1)]
    assert paths(commented) == [
        ("/1:div/1:#comment", None),
        ("/1:div/1:p", 1),
        ("/1:div/2:#comment", None),
        ("/1:div/2:p", 1),
    ]


def test_a_comment_path_cannot_collide_with_an_element_named_comment() -> None:
    result = html(b"<div><comment><p>element</p></comment><!--node--></div>")

    # ``#comment`` is the DOM's own name for the node and no element can be
    # spelled with a ``#``, so the two namespaces cannot meet.
    assert paths(result) == [
        ("/1:div/1:comment/1:p", 1),
        ("/1:div/1:#comment", None),
    ]


@pytest.mark.parametrize(
    "source",
    [
        # A hidden container holding a block, a script and a comment.
        b'<div style="visibility:hidden"><p>deep</p><script>s()</script><!--c--></div>',
        # A template inside a hidden element, and a hidden element inside it.
        (
            b"<div hidden><template><p hidden>deep</p><script>s()</script>"
            b"<!--c--></template></div>"
        ),
        # A style element inside a template inside a hidden container.
        (
            b'<section hidden><template><style>s()</style><p>deep</p><!--c-->'
            b"</template></section>"
        ),
    ],
)
def test_nested_non_body_roles_neither_leak_nor_double_count(source: bytes) -> None:
    result = html(source)

    # A hidden ancestor hides every descendant once; no descendant's words reach
    # the ancestor's segment, and no segment is published twice.
    assert sorted(roles(result)) == sorted(
        [
            ("hidden_content", "hidden", "deep"),
            ("hidden_content", "hidden", "s()"),
            ("comment", "annotation", "c"),
        ]
    )


def test_an_inline_hidden_claim_is_published_beside_the_hidden_role() -> None:
    result = html(
        b'<p style="display:none;visibility:hidden">declared</p>'
        b"<p hidden>attribute</p>"
    )

    # The two readings are told apart by ``hiddenStyle``: present, and this walk
    # read the element's own declaration; absent, and the ``hidden`` attribute
    # hid it. Nothing here claims a computed visibility for either.
    assert [
        item.source_locator.get("hiddenStyle") for item in result.segments
    ] == [["display:none", "visibility:hidden"], None]


def test_an_inline_hidden_claim_rides_with_an_image_alt() -> None:
    result = html(b'<img style="display:none" alt="Map" src="map.png">')

    assert roles(result) == [("hidden_content", "hidden", "Map")]
    assert result.segments[0].source_locator["hiddenStyle"] == ["display:none"]


@pytest.mark.parametrize(
    ("style", "claims"),
    [
        (b'style="display:none"', ["display:none"]),
        (b'style="DISPLAY : NONE"', ["display:none"]),
        (b'style="display:none !important"', ["display:none"]),
        (b'style="color:red;display:none;"', ["display:none"]),
        (b'style="visibility:hidden"', ["visibility:hidden"]),
        # A property declared twice keeps the declaration that would win among
        # the element's own, which is the last one written.
        (b'style="display:none;display:block"', None),
        (b'style="display:block;display:none"', ["display:none"]),
        # Neither a longer property name nor a value this walk does not claim is
        # read as one of the two it does.
        (b'style="-webkit-display:none"', None),
        (b'style="visibility:collapse"', None),
        (b'style="display:nonesuch"', None),
        (b'style="background:url(none)"', None),
        # An attribute that declares nothing makes no claim.
        (b"style", None),
        (b'style=""', None),
    ],
)
def test_an_inline_claim_is_read_from_the_elements_own_declarations_only(
    style: bytes, claims: list[str] | None
) -> None:
    result = html(b"<p " + style + b">x</p>")

    assert roles(result) == [
        ("hidden_content" if claims else "body", "hidden" if claims else "visible", "x")
    ]
    assert result.segments[0].source_locator.get("hiddenStyle") == claims


def test_a_stylesheet_hides_nothing_this_walk_publishes() -> None:
    result = html(b"<style>p{display:none}</style><p>visible</p>")

    # The rule is located as the style body it is; resolving its selector would
    # be computing a visibility, which this walk never claims.
    assert roles(result) == [
        ("hidden_content", "hidden", "p{display:none}"),
        ("body", "visible", "visible"),
    ]


def test_a_repeated_attribute_keeps_the_value_html_keeps() -> None:
    first_wins = html(b'<p style="color:red" style="display:none">x</p>')
    still_hidden = html(b'<p style="display:none" style="color:red">x</p>')

    # HTML drops a duplicate attribute rather than overriding with it, so the
    # first declaration is the element's and a later one changes nothing.
    assert roles(first_wins) == [("body", "visible", "x")]
    assert roles(still_hidden) == [("hidden_content", "hidden", "x")]


@pytest.mark.parametrize(
    ("source", "located"),
    [
        # A comment the source never terminates is still the comment it opened.
        (b"<p>a</p><!-- unterminated", [("comment", "annotation", " unterminated")]),
        # A bogus comment is a comment, which is how HTML reads one.
        (b"<p>a</p><!bogus>", [("comment", "annotation", "bogus")]),
        # An empty comment holds no text, so it is no segment.
        (b"<p>a</p><!---->", []),
        (b"<p>a</p><!-->", []),
        # The dashes inside a comment belong to the comment.
        (b"<p>a</p><!-- x -- y -->", [("comment", "annotation", " x -- y ")]),
        # Markup inside a script is script text, never markup.
        (
            b"<p>a</p><script><p>b</p><!--c--></script>",
            [("hidden_content", "hidden", "<p>b</p><!--c-->")],
        ),
        # A script the source never closes still carries its own text.
        (b"<p>a</p><script>var x = 1", [("hidden_content", "hidden", "var x = 1")]),
        # An end tag that closes the wrong element does not unhide what follows
        # it inside the hidden one.
        (b"<p>a</p><div hidden><p>b</div>", [("hidden_content", "hidden", "b")]),
    ],
)
def test_malformed_markup_still_locates_its_non_body_text(
    source: bytes, located: list[tuple[str, str, str]]
) -> None:
    assert roles(html(source)) == [("body", "visible", "a"), *located]


def test_xhtml_and_html_locate_the_same_non_body_roles() -> None:
    # XHTML has no boolean attribute, so the one the HTML fixture writes bare is
    # written with its own name as its value; nothing else about it changes.
    body = EVERY_ROLE.replace(b"<p hidden>", b'<p hidden="hidden">')
    strict = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<html xmlns="http://www.w3.org/1999/xhtml"><body>' + body + b"</body></html>"
    )

    # The admission parse differs between the two; what each role is and where
    # it sits does not, once the document XHTML must declare is unwrapped.
    assert roles(html(strict, media_type=XHTML_MEDIA_TYPE)) == roles(html(body))
    assert [
        (path.removeprefix("/1:html/1:body"), run)
        for path, run in paths(html(strict, media_type=XHTML_MEDIA_TYPE))
    ] == paths(html(body))


def test_a_script_body_and_an_event_handler_are_inert_text(
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

    result = html(
        b'<p onclick="fetch(\'http://example.invalid/x\')">clicky</p>'
        b'<img src="http://example.invalid/i" onerror="alert(1)" alt="Map">'
        b"<script src=\"http://example.invalid/s\">open('/etc/passwd')</script>"
    )

    # Nothing was opened, connected to, resolved or run. A handler attribute is
    # not prose and is published as no segment at all; a script body is text in
    # a hidden role, and the only target carried out is the inert inventory one.
    assert roles(result) == [
        ("body", "visible", "clicky"),
        ("image_alt", "visible", "Map"),
        ("hidden_content", "hidden", "open('/etc/passwd')"),
    ]
    assert [
        item.structured_value["target"]
        for item in result.segments
        if item.structured_value is not None
    ] == ["http://example.invalid/i"]
    assert all(
        item.structured_value["resolved"] is False
        for item in result.segments
        if item.structured_value is not None
    )


def test_a_comment_is_bounded_by_the_segment_character_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = b"<p>a</p><!--" + b"x" * 8 + b"-->"
    monkeypatch.setattr(document_extractors, "MAX_HTML_SEGMENT_CHARS", 4)

    with pytest.raises(DocumentExtractionInvalid, match="comment exceeds"):
        html(source)

    monkeypatch.undo()
    assert html(source).state == "succeeded"


def test_comments_are_charged_against_the_output_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = b"<p>a</p><!--one--><!--two-->"
    monkeypatch.setattr(document_extractors, "MAX_HTML_SEGMENTS", 2)

    # A comment is a published segment, so it counts towards the output bound
    # exactly as prose does rather than slipping past it.
    with pytest.raises(DocumentExtractionInvalid, match="too many segments"):
        html(source)

    monkeypatch.undo()
    assert len(html(source).segments) == 3


def test_hidden_nesting_is_bounded_by_the_depth_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = b"<div hidden>" * 3 + b"<p>deep</p>" + b"</div>" * 3
    monkeypatch.setattr(document_extractors, "MAX_HTML_DEPTH", 2)

    # A hidden subtree is walked under the same node bounds as a visible one, so
    # nesting cannot be driven deeper by hiding it.
    with pytest.raises(DocumentExtractionInvalid, match="nesting exceeds"):
        html(source)

    monkeypatch.undo()
    assert MAX_HTML_DEPTH > 3
    assert roles(html(source)) == [("hidden_content", "hidden", "deep")]


def test_a_comment_at_the_packaged_bound_is_accepted() -> None:
    source = b"<!--" + b"x" * MAX_HTML_SEGMENT_CHARS + b"-->"

    # The bound is a refusal above it and nothing below it: a comment of exactly
    # the accepted length is published whole rather than trimmed to fit.
    assert roles(html(source)) == [
        ("comment", "annotation", "x" * MAX_HTML_SEGMENT_CHARS)
    ]
