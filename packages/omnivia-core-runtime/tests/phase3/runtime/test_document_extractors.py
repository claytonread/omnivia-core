from __future__ import annotations

import io
import zipfile

import pytest
from omnivia_core_runtime.service.document_extractors import (
    DOCX_MEDIA_TYPE,
    PPTX_MEDIA_TYPE,
    XLSX_MEDIA_TYPE,
    DocumentExtractionInvalid,
    DocumentExtractionUnsupported,
    extract_document,
)

CAPTURE = "dcap-" + "a" * 64


def office_package(parts: dict[str, bytes]) -> bytes:
    content_types = (
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()


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


def test_docx_extracts_body_and_keeps_comments_separate() -> None:
    package = office_package(
        {
            "word/document.xml": b'<w:document xmlns:w="urn:w"><w:body><w:p><w:r><w:t>Body text</w:t></w:r></w:p></w:body></w:document>',
            "word/comments.xml": b'<w:comments xmlns:w="urn:w"><w:comment><w:p><w:r><w:t>Review note</w:t></w:r></w:p></w:comment></w:comments>',
        }
    )

    result = extract_document(package, media_type=DOCX_MEDIA_TYPE, capture_id=CAPTURE)

    assert [
        (item.semantic_role, item.text, item.visibility) for item in result.segments
    ] == [
        ("body", "Body text", "visible"),
        ("comment", "Review note", "annotation"),
    ]


def test_xlsx_preserves_formula_and_saved_cache_as_distinct_values() -> None:
    package = office_package(
        {
            "xl/workbook.xml": b'<workbook xmlns:r="urn:r"><sheets><sheet name="Budget" sheetId="7" r:id="rId1"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": b'<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
            "xl/worksheets/sheet1.xml": b'<worksheet><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Name</t></is></c><c r="B1"><f>SUM(B2:B3)</f><v>42</v></c></row></sheetData></worksheet>',
        }
    )

    result = extract_document(package, media_type=XLSX_MEDIA_TYPE, capture_id=CAPTURE)

    assert [item.semantic_role for item in result.segments] == [
        "table_cell",
        "formula",
        "saved_value",
    ]
    assert result.segments[1].structured_value == {
        "expression": "SUM(B2:B3)",
        "cacheFreshness": "unknown",
    }
    assert result.segments[2].structured_value == {"raw": "42", "freshness": "unknown"}


def test_pptx_extracts_slide_text_and_notes_without_rendering() -> None:
    package = office_package(
        {
            "ppt/presentation.xml": b'<p:presentation xmlns:p="urn:p" xmlns:r="urn:r"><p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst></p:presentation>',
            "ppt/_rels/presentation.xml.rels": b'<Relationships><Relationship Id="rId1" Target="slides/slide1.xml"/></Relationships>',
            "ppt/slides/slide1.xml": b'<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><p:sp><a:p><a:r><a:t>Quarterly plan</a:t></a:r></a:p></p:sp></p:sld>',
            "ppt/notesSlides/notesSlide1.xml": b'<p:notes xmlns:p="urn:p" xmlns:a="urn:a"><a:t>Speaker detail</a:t></p:notes>',
        }
    )

    result = extract_document(package, media_type=PPTX_MEDIA_TYPE, capture_id=CAPTURE)

    assert [(item.semantic_role, item.text) for item in result.segments] == [
        ("body", "Quarterly plan"),
        ("note", "Speaker detail"),
    ]


def test_ooxml_rejects_traversal_and_macro_enabled_content() -> None:
    with pytest.raises(DocumentExtractionInvalid, match="unsafe or ambiguous"):
        extract_document(
            office_package({"../word/document.xml": b"<document/>"}),
            media_type=DOCX_MEDIA_TYPE,
            capture_id=CAPTURE,
        )

    macro_types = b'<Types><Override PartName="/word/vbaProject.bin" ContentType="application/vnd.ms-office.vbaProject"/></Types>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", macro_types)
        archive.writestr("word/document.xml", b"<document/>")
    with pytest.raises(DocumentExtractionInvalid, match="macro-enabled"):
        extract_document(
            buffer.getvalue(), media_type=DOCX_MEDIA_TYPE, capture_id=CAPTURE
        )
