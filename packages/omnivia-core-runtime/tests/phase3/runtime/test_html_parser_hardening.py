"""The hardened lxml admission parse in front of the inert HTML walk.

What is asserted here is admission and what the adapter reports about it: which
parser read the source, what it had to recover from, and what it refuses
outright. The located segments are the inert walk's and are asserted to be
exactly what they were before this seam existed, because the admission parse
decides whether a source is read at all and never what its text is.
"""

from __future__ import annotations

import builtins
import socket
import subprocess
from typing import Any, Final

import pytest
from omnivia_core_runtime.service import document_extractors
from omnivia_core_runtime.service.document_extractors import (
    MAX_HTML_SEGMENT_CHARS,
    MAX_HTML_SOURCE_BYTES,
    MAX_TEXT_BYTES,
    XHTML_MEDIA_TYPE,
    DocumentExtractionInvalid,
    extract_document,
)
from omnivia_core_runtime.service.document_intake import ExtractionOutcome
from omnivia_core_runtime.service.document_processing import portable_profile

CAPTURE: Final = "dcap-" + "c" * 64
XHTML: Final = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<html xmlns="http://www.w3.org/1999/xhtml"><body>'
    b"<h1>Title</h1><p>Body</p></body></html>"
)
#: The same document with no XML declaration, so only the media type can say
#: that it is XML.
UNDECLARED_XHTML: Final = (
    b'<html xmlns="http://www.w3.org/1999/xhtml"><body>'
    b"<h1>Title</h1><p>Body</p></body></html>"
)
#: A document type whose internal subset declares the entity it then expands.
ENTITY_BOMB: Final = (
    b'<?xml version="1.0"?>'
    b'<!DOCTYPE html [<!ENTITY blow "boom">]>'
    b"<html><body><p>&blow;</p></body></html>"
)
EXTERNAL_DTD: Final = (
    b'<?xml version="1.0"?>'
    b'<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN"'
    b' "http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd">'
    b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>x</p></body></html>'
)


def html(source: bytes) -> ExtractionOutcome:
    return extract_document(source, media_type="text/html", capture_id=CAPTURE)


def xhtml(source: bytes) -> ExtractionOutcome:
    """Extract one source through the public path as an XHTML media type."""
    return extract_document(source, media_type=XHTML_MEDIA_TYPE, capture_id=CAPTURE)


def texts(result: ExtractionOutcome) -> list[tuple[str, str]]:
    return [
        (item.semantic_role, item.text)
        for item in result.segments
        if item.text is not None
    ]


def locators(result: ExtractionOutcome) -> list[dict[str, Any]]:
    return [dict(item.source_locator) for item in result.segments]


def test_ordinary_html_names_the_html_parser_and_reports_a_clean_read() -> None:
    result = html(b"<div><p>first</p><p>second</p></div>")

    assert result.limitations == {
        "parser": "lxml.html.HTMLParser",
        "parserRecovered": False,
        "parserWarnings": [],
    }


def test_malformed_html_retains_its_recovery_warnings() -> None:
    source = b"<p>a<div>b</p></span><p>c"

    result = html(source)

    # The source is still read, and what libxml2 had to recover from is carried
    # out rather than dropped on the floor.
    assert result.state == "succeeded"
    assert result.limitations["parser"] == "lxml.html.HTMLParser"
    assert result.limitations["parserRecovered"] is True
    assert "ERR_TAG_NAME_MISMATCH" in result.limitations["parserWarnings"]
    # The same bytes report the same warnings every time, so the limitations a
    # revision is published with do not move between runs.
    assert html(source).limitations == result.limitations


def test_parser_warnings_are_sorted_distinct_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(document_extractors, "MAX_HTML_PARSER_WARNINGS", 0)

    warnings = html(b"<p>a<div>b</p></span>").limitations["parserWarnings"]

    # A warning list is diagnostics, not prose, so the bound caps it instead of
    # refusing the source the way every bound on located text does.
    assert warnings == []
    monkeypatch.undo()
    warnings = html(b"<p>a<div>b</p></span>").limitations["parserWarnings"]
    assert warnings and warnings == sorted(set(warnings))


def test_xml_declared_xhtml_is_read_by_the_xml_parser() -> None:
    result = html(XHTML)

    assert result.limitations == {
        "parser": "lxml.etree.XMLParser",
        "parserRecovered": False,
        "parserWarnings": [],
    }
    # Admission chose the stricter parser; the text is still the inert walk's.
    assert texts(result) == [("heading", "Title"), ("body", "Body")]


def test_the_xhtml_media_type_is_extracted_through_the_public_path() -> None:
    # No XML declaration, so the media type alone selects the strict parser,
    # and it does so through ``extract_document`` rather than the seam beneath.
    result = xhtml(UNDECLARED_XHTML)

    assert result.state == "succeeded"
    assert result.limitations == {
        "parser": "lxml.etree.XMLParser",
        "parserRecovered": False,
        "parserWarnings": [],
    }
    # The same bytes as the same media type locate the same segments as
    # ``text/html`` does, because the walk beneath admission is the one walk.
    assert texts(result) == [("heading", "Title"), ("body", "Body")]
    assert texts(html(UNDECLARED_XHTML)) == texts(result)


def test_the_xhtml_media_type_routes_to_the_markup_profile_and_its_limits() -> None:
    profile = portable_profile(
        media_type=XHTML_MEDIA_TYPE, profile_ref="default", revision="1"
    )

    # Routing is end to end: the published profile names the same adapter as
    # ``text/html`` and declares the bound the markup adapter actually applies.
    assert profile["adapter"] == "inert-html"
    assert profile["limits"]["sourceBytes"] == MAX_HTML_SOURCE_BYTES
    assert (
        profile["decoderBuild"]
        == portable_profile(
            media_type="text/html", profile_ref="default", revision="1"
        )["decoderBuild"]
    )


@pytest.mark.parametrize("source", [ENTITY_BOMB, EXTERNAL_DTD])
@pytest.mark.parametrize("extract", [html, xhtml])
def test_declared_document_types_and_entities_are_refused(
    source: bytes, extract: Any
) -> None:
    # Neither declaration is read with its effect quietly ignored: the source
    # is refused, so no expansion and no external fetch can follow from it.
    with pytest.raises(DocumentExtractionInvalid, match="document type or an entity"):
        extract(source)


def test_an_xhtml_document_type_with_no_declaration_is_refused() -> None:
    # The media type is the only thing saying this is XML, and the DTD refusal
    # has to hold on that reading too: a stripped declaration is not a bypass.
    with pytest.raises(DocumentExtractionInvalid, match="document type or an entity"):
        xhtml(
            b'<!DOCTYPE html [<!ENTITY blow "boom">]>'
            b"<html><body><p>&blow;</p></body></html>"
        )


@pytest.mark.parametrize("extract", [html, xhtml])
def test_undeclared_entity_and_malformed_xhtml_are_refused(extract: Any) -> None:
    for source in (
        b'<?xml version="1.0"?><html><body><p>&nope;</p></body></html>',
        b'<?xml version="1.0"?><html><body><p>open</body></html>',
    ):
        with pytest.raises(DocumentExtractionInvalid, match="not well formed"):
            extract(source)


def test_malformed_xhtml_is_refused_where_the_same_bytes_as_html_recover() -> None:
    source = b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>open</body></html>'

    with pytest.raises(DocumentExtractionInvalid, match="not well formed"):
        xhtml(source)

    # The strictness is the media type's, not a new refusal for everyone:
    # ordinary HTML still recovers from exactly these bytes.
    assert html(source).state == "succeeded"


def test_admission_takes_no_external_action_for_any_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*arguments: object, **keywords: object) -> Any:
        raise AssertionError("the admission parse must take no external action")

    monkeypatch.setattr(builtins, "open", refuse)
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)

    # Bytes that read as a path and bytes that read as a URL are parsed as the
    # markup they are: the admission parse is never handed a filename.
    assert html(b"<p>/etc/passwd</p>").state == "succeeded"
    assert html(b"<p>https://example.invalid/x</p>").state == "succeeded"
    assert html(XHTML).state == "succeeded"
    assert xhtml(UNDECLARED_XHTML).state == "succeeded"
    for extract in (html, xhtml):
        with pytest.raises(DocumentExtractionInvalid):
            extract(EXTERNAL_DTD)


@pytest.mark.parametrize(
    ("bound", "value", "source", "reason"),
    [
        ("MAX_HTML_DEPTH", 2, b"<div><div><div>deep</div></div></div>", "nesting"),
        ("MAX_HTML_ELEMENTS", 2, b"<p>a</p><p>b</p><p>c</p>", "too many elements"),
        (
            "MAX_HTML_SOURCE_BYTES",
            8,
            b"<p>a longer source</p>",
            "exceeds the accepted bound",
        ),
    ],
)
def test_depth_node_and_source_bounds_still_refuse_with_admission_in_the_path(
    bound: str,
    value: int,
    source: bytes,
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(document_extractors, bound, value)

    with pytest.raises(DocumentExtractionInvalid, match=reason):
        html(source)

    monkeypatch.undo()
    assert html(source).state == "succeeded"


def padded_markup(size: int) -> bytes:
    """One readable markup source of exactly ``size`` bytes.

    The padding is comments, so the source reaches the byte bound without
    reaching the per-element text bound that would refuse it for another reason.
    It is spread over several of them because a comment is a located segment in
    its own right and so carries the same per-segment character bound as an
    element's text, and because libxml2 refuses any single node over 10 MB while
    ``huge_tree`` is off; both are ceilings under the source bound rather than
    the source bound itself.
    """
    head = b"<p>body</p>"
    remaining, parts = size - len(head), []
    while remaining:
        take = min(remaining, MAX_HTML_SEGMENT_CHARS)
        parts.append(b"<!--" + b"x" * (take - 7) + b"-->")
        remaining -= take
    return head + b"".join(parts)


@pytest.mark.parametrize("extract", [html, xhtml])
def test_markup_enforces_the_published_twenty_mebibyte_source_bound(
    extract: Any,
) -> None:
    assert MAX_HTML_SOURCE_BYTES == 20 * 1024 * 1024

    at_bound = padded_markup(MAX_HTML_SOURCE_BYTES)
    assert len(at_bound) == MAX_HTML_SOURCE_BYTES
    # A source the published profile says is accepted is accepted, so the
    # declared ceiling is the one a caller actually gets.
    assert extract(at_bound).state == "succeeded"
    with pytest.raises(DocumentExtractionInvalid, match="exceeds the accepted bound"):
        extract(padded_markup(MAX_HTML_SOURCE_BYTES + 1))


def test_plain_text_keeps_its_own_one_mebibyte_source_bound() -> None:
    # Raising the markup bound raised nothing else: the text bound is separate,
    # and the profile for these media types still publishes 1 MiB.
    assert MAX_TEXT_BYTES == 1024 * 1024
    for media_type in ("text/plain", "text/markdown"):
        assert (
            extract_document(
                b"x" * MAX_TEXT_BYTES, media_type=media_type, capture_id=CAPTURE
            ).state
            == "succeeded"
        )
        with pytest.raises(
            DocumentExtractionInvalid, match="exceeds the accepted bound"
        ):
            extract_document(
                b"x" * (MAX_TEXT_BYTES + 1), media_type=media_type, capture_id=CAPTURE
            )
        assert (
            portable_profile(
                media_type=media_type, profile_ref="default", revision="1"
            )["limits"]["sourceBytes"]
            == MAX_TEXT_BYTES
        )


def test_located_segments_are_unchanged_by_the_admission_parse() -> None:
    result = html(
        b'<div><p>first</p><br><p id="second">second</p>'
        b"<table><tr><td>cell</td></tr></table></div>"
        b'<p>Go <a href="http://example.invalid/x">here</a></p>'
    )

    # The checkpointed locator shape and source order, unchanged: admission
    # decides readability and contributes nothing to where text is located.
    assert texts(result) == [
        ("body", "first"),
        ("body", "second"),
        ("table_cell", "cell"),
        ("body", "Go here"),
    ]
    assert [(item.get("path"), item.get("run")) for item in locators(result)] == [
        ("/1:div/1:p", 1),
        ("/1:div/3:p", 1),
        ("/1:div/4:table/1:tr/1:td", 1),
        ("/2:p/1:a", None),
        ("/2:p", 1),
    ]
    assert (
        result.output_digest
        == html(
            b'<div><p>first</p><br><p id="second">second</p>'
            b"<table><tr><td>cell</td></tr></table></div>"
            b'<p>Go <a href="http://example.invalid/x">here</a></p>'
        ).output_digest
    )
