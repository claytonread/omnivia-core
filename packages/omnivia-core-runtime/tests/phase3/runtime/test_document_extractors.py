from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from _trusted_ocr import catalogue_over, host_models
from omnivia_core_runtime.service.document_extractors import (
    DOCX_MEDIA_TYPE,
    PDF_MEDIA_TYPE,
    PPTX_MEDIA_TYPE,
    XLSX_MEDIA_TYPE,
    DocumentExtractionInvalid,
    DocumentExtractionUnsupported,
    extract_document,
)
from omnivia_core_runtime.service.ocr_resources import (
    ExtractionSettings,
    local_ocr_settings,
)
from PIL import Image, ImageDraw, ImageFont

CAPTURE = "dcap-" + "a" * 64


@pytest.fixture(scope="module")
def local_ocr(tmp_path_factory: pytest.TempPathFactory) -> ExtractionSettings:
    """Local OCR over a trusted model directory this test owns.

    Production names no model directory and installs no model, so recognition
    cannot run until an installation configures one: the default here would fail
    closed. The host's installed models are copied into a directory the test
    owns and pinned by their real SHA-256, which is the explicit trusted path an
    installation takes - never an ambient ``TESSDATA_PREFIX``.
    """
    directory = host_models(tmp_path_factory.mktemp("tessdata"), "eng", "osd")
    if directory is None:  # pragma: no cover - host dependent
        pytest.skip("no local OCR engine with the English and orientation models")
    return local_ocr_settings(catalogue=catalogue_over(directory, "eng", "osd"))


def office_package(parts: dict[str, bytes], *, overrides: bytes = b"") -> bytes:
    content_types = (
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        + overrides
        + b"</Types>"
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
        extract_document(b"legacy", media_type="application/msword", capture_id=CAPTURE)


def test_docx_extracts_body_and_keeps_comments_separate() -> None:
    package = office_package(
        {
            "word/document.xml": b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Body text</w:t></w:r></w:p></w:body></w:document>',
            "word/comments.xml": b'<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:comment><w:p><w:r><w:t>Review note</w:t></w:r></w:p></w:comment></w:comments>',
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
    # The package declares no styles part and no date system, so both records
    # carry the General, 1900-epoch provenance of a cell that authored no style.
    assert result.segments[1].structured_value == {
        "expression": "SUM(B2:B3)",
        "kind": "ordinary",
        "sharedIndex": None,
        "ref": None,
        "cacheState": "present",
        "cacheFreshness": "unknown",
        "styleIndex": None,
        "numberFormatId": 0,
        "numberFormatCode": None,
        "dateSystem": "1900",
    }
    assert result.segments[2].structured_value == {
        "raw": "42",
        "cellType": None,
        "error": False,
        "freshness": "unknown",
        # The saved cache is a stored number, so it is decoded beside the
        # lexical value the file wrote; the formula above is untouched.
        "decoded": 42,
        "styleIndex": None,
        "numberFormatId": 0,
        "numberFormatCode": None,
        "dateSystem": "1900",
    }


def test_pptx_extracts_slide_text_tables_and_related_notes_without_rendering() -> None:
    frame = b'<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="3"/></p:nvGraphicFramePr><a:graphic><a:graphicData><a:tbl><a:tblGrid><a:gridCol w="100"/><a:gridCol w="100"/></a:tblGrid><a:tr><a:tc><a:txBody><a:p><a:r><a:t>Region</a:t></a:r></a:p></a:txBody></a:tc><a:tc><a:txBody><a:p><a:r><a:t>Revenue</a:t></a:r></a:p></a:txBody></a:tc></a:tr></a:tbl></a:graphicData></a:graphic></p:graphicFrame>'
    package = office_package(
        {
            "ppt/presentation.xml": b'<p:presentation xmlns:p="urn:p" xmlns:r="urn:r"><p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst></p:presentation>',
            "ppt/_rels/presentation.xml.rels": b'<Relationships><Relationship Id="rId1" Target="slides/slide1.xml"/></Relationships>',
            "ppt/slides/slide1.xml": b'<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><p:cSld><p:spTree><p:sp><p:nvSpPr><p:cNvPr id="2" name="Title"/></p:nvSpPr><p:txBody><a:p><a:r><a:t>Quarterly plan</a:t></a:r></a:p></p:txBody></p:sp>'
            + frame
            + b"</p:spTree></p:cSld></p:sld>",
            "ppt/slides/_rels/slide1.xml.rels": b'<Relationships><Relationship Id="rId2" Target="../notesSlides/notesSlide1.xml" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide"/></Relationships>',
            "ppt/notesSlides/notesSlide1.xml": b'<p:notes xmlns:p="urn:p" xmlns:a="urn:a"><a:t>Speaker detail</a:t></p:notes>',
        },
        overrides=b'<Override PartName="/ppt/notesSlides/notesSlide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.notesSlide+xml"/>',
    )

    result = extract_document(package, media_type=PPTX_MEDIA_TYPE, capture_id=CAPTURE)

    assert [(item.semantic_role, item.text) for item in result.segments] == [
        ("body", "Quarterly plan"),
        ("table_cell", "Region"),
        ("table_cell", "Revenue"),
        ("note", "Speaker detail"),
    ]


def test_pptx_ignores_a_notes_part_the_slide_does_not_relate_to() -> None:
    """A notes-looking member reached only by name substitution is never read."""
    package = office_package(
        {
            "ppt/presentation.xml": b'<p:presentation xmlns:p="urn:p" xmlns:r="urn:r"><p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst></p:presentation>',
            "ppt/_rels/presentation.xml.rels": b'<Relationships><Relationship Id="rId1" Target="slides/slide1.xml"/></Relationships>',
            "ppt/slides/slide1.xml": b'<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><p:cSld><p:spTree><p:sp><p:nvSpPr><p:cNvPr id="2"/></p:nvSpPr><p:txBody><a:p><a:r><a:t>Quarterly plan</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>',
            "ppt/notesSlides/notesSlide1.xml": b'<p:notes xmlns:p="urn:p" xmlns:a="urn:a"><a:t>Not this slide\'s notes</a:t></p:notes>',
        },
        overrides=b'<Override PartName="/ppt/notesSlides/notesSlide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.notesSlide+xml"/>',
    )

    result = extract_document(package, media_type=PPTX_MEDIA_TYPE, capture_id=CAPTURE)

    assert [(item.semantic_role, item.text) for item in result.segments] == [
        ("body", "Quarterly plan")
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


def test_pdf_extracts_native_page_text_with_page_provenance() -> None:
    repository = Path(__file__).resolve().parents[5]
    content = (
        repository / "services/omnivia-memory/tests/fixtures/sample.pdf"
    ).read_bytes()

    result = extract_document(content, media_type=PDF_MEDIA_TYPE, capture_id=CAPTURE)

    native = [
        item
        for item in result.segments
        if item.source_locator.get("method") == "native_text"
    ]
    assert "Test PDF Document" in (native[0].text or "")
    assert all(item.source_locator["page"] == 1 for item in native)
    assert all(item.extraction_method == "direct" for item in native)
    # Directly decoded text never claims an engine confidence.
    assert all(item.confidence is None for item in native)
    assert all(
        item.source_locator["space"] == "cropbox_points_top_left_rotated_v1"
        and item.source_locator["cropBox"] == [0.0, 0.0, 595.0, 842.0]
        and item.source_locator["rotation"] == 0
        for item in native
    )


def test_png_runs_local_ocr_with_geometry_and_confidence(
    local_ocr: ExtractionSettings,
) -> None:
    image = Image.new("RGB", (1000, 240), "white")
    draw = ImageDraw.Draw(image)
    draw.text(
        (40, 55),
        "OmniVia OCR",
        fill="black",
        font=ImageFont.load_default(size=92),
    )
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")

    result = extract_document(
        encoded.getvalue(),
        media_type="image/png",
        capture_id=CAPTURE,
        settings=local_ocr,
    )

    assert result.state == "succeeded"
    assert "OmniVia OCR" in " ".join(item.text or "" for item in result.segments)
    assert all(item.extraction_method == "ocr" for item in result.segments)
    assert all(item.confidence is not None for item in result.segments)
    assert all("box" in item.source_locator for item in result.segments)


def test_rtf_uses_fixed_local_reader_and_records_ast_locators() -> None:
    content = (
        rb"{\rtf1\ansi\deff0 {\fonttbl {\f0 Arial;}}\fs24 Hello \b OmniVia\b0\par}"
    )

    result = extract_document(content, media_type="application/rtf", capture_id=CAPTURE)

    assert result.state == "succeeded"
    assert "Hello OmniVia" in " ".join(item.text or "" for item in result.segments)
    assert all(item.source_locator["unit"] == "rtf_ast" for item in result.segments)
    assert result.limitations["readerBuild"] == "pandoc-3.12.1-rtf-json"


def test_rtf_refuses_unbalanced_groups_before_reader_execution() -> None:
    with pytest.raises(DocumentExtractionInvalid, match="unbalanced"):
        extract_document(
            rb"{\rtf1\ansi missing-close",
            media_type="application/rtf",
            capture_id=CAPTURE,
        )
