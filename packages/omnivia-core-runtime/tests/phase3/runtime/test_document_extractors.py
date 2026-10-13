from __future__ import annotations

import pytest
from omnivia_core_runtime.service.document_extractors import (
    DocumentExtractionInvalid,
    DocumentExtractionUnsupported,
    extract_document,
)

CAPTURE = "dcap-" + "a" * 64


def test_text_preserves_bom_and_line_endings() -> None:
    result = extract_document(
        b"\xef\xbb\xbfline one\r\nline two\n",
        media_type="text/plain",
        capture_id=CAPTURE,
    )

    assert result.state == "succeeded"
    assert result.segments[0].text == "\ufeffline one\r\nline two\n"
    assert result.coverage["roleCoverage"] == {"body": 1}


def test_csv_preserves_empty_fields_quoted_newlines_and_formula_strings() -> None:
    result = extract_document(
        b'name,note,formula,empty\r\nAda,"two\nlines",=1+1,\r\n',
        media_type="text/csv",
        capture_id=CAPTURE,
    )

    values = [segment.structured_value["raw"] for segment in result.segments]
    assert values == [
        "name",
        "note",
        "formula",
        "empty",
        "Ada",
        "two\nlines",
        "=1+1",
        "",
    ]
    assert result.segments[-1].source_locator["physicalLineStart"] == 2
    assert result.segments[-1].source_locator["physicalLineEnd"] == 3


def test_html_extracts_visible_structure_alt_text_and_separates_script() -> None:
    result = extract_document(
        b'<h1 id="title">Title</h1><p>Hello <b>world</b></p><img alt="Diagram"><script>doNotRun()</script>',
        media_type="text/html",
        capture_id=CAPTURE,
    )

    assert [
        (item.semantic_role, item.text, item.visibility) for item in result.segments
    ] == [
        ("heading", "Title", "visible"),
        ("body", "Hello world", "visible"),
        ("image_alt", "Diagram", "visible"),
        ("hidden_content", "doNotRun()", "hidden"),
    ]


def test_invalid_encoding_and_unqualified_format_are_refused() -> None:
    with pytest.raises(DocumentExtractionInvalid):
        extract_document(b"\xff", media_type="text/plain", capture_id=CAPTURE)
    with pytest.raises(DocumentExtractionUnsupported):
        extract_document(b"%PDF", media_type="application/pdf", capture_id=CAPTURE)
