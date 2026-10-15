from __future__ import annotations

import base64
import io
import subprocess
import zipfile
from pathlib import Path
from typing import Any, Final

import pytest
from _trusted_ocr import catalogue_over, host_models
from omnivia_core_runtime.service import rtf_extractor
from omnivia_core_runtime.service.document_extractors import (
    DOCX_MEDIA_TYPE,
    PDF_MEDIA_TYPE,
    PPTX_MEDIA_TYPE,
    XLSX_MEDIA_TYPE,
    DocumentExtractionInvalid,
    DocumentExtractionUnsupported,
    extract_document,
)
from omnivia_core_runtime.service.document_intake import ExtractionOutcome
from omnivia_core_runtime.service.ocr_resources import (
    ExtractionSettings,
    local_ocr_settings,
)
from PIL import Image, ImageDraw, ImageFont

from omnivia_core.contracts.v1 import to_canonical_json

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


DOCX_IMAGE_BODY: Final = (
    b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    b' xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"'
    b' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    b' xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"'
    b' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    b"<w:body><w:p><w:r><w:t>Body text</w:t></w:r></w:p>"
    b"<w:p><w:r><w:drawing><wp:inline><a:graphic><a:graphicData><pic:pic>"
    b'<pic:blipFill><a:blip r:embed="rId9"/></pic:blipFill>'
    b"</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"
    b"</w:body></w:document>"
)
DOCX_IMAGE_RELS: Final = (
    b'<Relationships><Relationship Id="rId9" Target="media/image1.png" Type='
    b'"http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"/>'
    b"</Relationships>"
)
PNG_DEFAULT: Final = b'<Default Extension="png" ContentType="image/png"/>'


def docx_with_image(member: bytes) -> bytes:
    return office_package(
        {
            "word/document.xml": DOCX_IMAGE_BODY,
            "word/_rels/document.xml.rels": DOCX_IMAGE_RELS,
            "word/media/image1.png": member,
        },
        overrides=PNG_DEFAULT,
    )


def test_a_docx_embedded_image_is_examined_only_under_settings_that_say_so(
    local_ocr: ExtractionSettings,
) -> None:
    """The trusted settings reach the Word path, and nothing else decides it: a
    profile that examines no embedded image publishes the structural extraction
    alone, and the same bytes under a profile that does are read with the
    engine the profile pinned."""
    image = io.BytesIO()
    Image.new("RGB", (240, 60), "white").save(image, format="PNG")
    package = docx_with_image(image.getvalue())

    structural = extract_document(
        package, media_type=DOCX_MEDIA_TYPE, capture_id=CAPTURE
    )
    assert structural.state == "succeeded"
    assert [item.semantic_role for item in structural.segments] == [
        "body",
        "object_inventory",
    ]
    record = structural.segments[1].structured_value
    assert (record["imageDecoded"], record["ocrPerformed"]) == (False, False)

    examined = extract_document(
        package,
        media_type=DOCX_MEDIA_TYPE,
        capture_id=CAPTURE,
        settings=ExtractionSettings(
            processing_mode=local_ocr.processing_mode,
            ocr=local_ocr.ocr,
            embedded_image_ocr=True,
        ),
    )
    assert examined.state == "succeeded"
    record = examined.segments[1].structured_value
    # A blank raster yields no reliable text, and that is a reading of it: the
    # bytes were decoded and the engine the profile pinned ran over them.
    assert (record["imageDecoded"], record["ocrPerformed"]) == (True, True)
    assert examined.limitations["embeddedImages"] == 1


def test_a_docx_whose_embedded_image_cannot_be_read_is_partial_not_a_success(
    local_ocr: ExtractionSettings,
) -> None:
    """The structural extraction stands and the source is not refused, but a
    raster the decoder could not read is coverage this source never reached, so
    the document is partial and says which coverage was lost."""
    package = docx_with_image(b"\x89PNG\r\n\x1a\nnot really an image")

    result = extract_document(
        package,
        media_type=DOCX_MEDIA_TYPE,
        capture_id=CAPTURE,
        settings=ExtractionSettings(
            processing_mode=local_ocr.processing_mode,
            ocr=local_ocr.ocr,
            embedded_image_ocr=True,
        ),
    )

    assert result.state == "partial"
    assert result.limitations["documentOutcome"] == "partial_coverage"
    assert result.limitations["embeddedImageOutcomes"] == {
        "image_could_not_be_decoded": 1
    }
    # No *block* failed, and the block coverage does not claim one did: the
    # image's own outcome is reported where its own unit is counted.
    assert result.coverage["units"][0]["failed"] == 0
    assert [item.semantic_role for item in result.segments] == [
        "body",
        "object_inventory",
    ]


SPREADSHEETML: Final = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RELNS: Final = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
SHEET_DRAWING: Final = (
    "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
)
DRAWINGML: Final = "http://schemas.openxmlformats.org/drawingml/2006/main"
XLSX_IMAGE_PARTS: Final = {
    "xl/workbook.xml": (
        f'<workbook xmlns="{SPREADSHEETML}" xmlns:r="{RELNS}"><sheets>'
        '<sheet name="Budget" sheetId="7" r:id="rId1"/></sheets></workbook>'
    ).encode(),
    "xl/_rels/workbook.xml.rels": (
        f'<Relationships><Relationship Id="rId1" Type="{RELNS}/worksheet"'
        ' Target="worksheets/sheet1.xml"/></Relationships>'
    ).encode(),
    "xl/worksheets/sheet1.xml": (
        f'<worksheet xmlns="{SPREADSHEETML}" xmlns:r="{RELNS}"><sheetData>'
        '<row r="1"><c r="A1" t="inlineStr"><is><t>Name</t></is></c></row>'
        '</sheetData><drawing r:id="rIdD"/></worksheet>'
    ).encode(),
    "xl/worksheets/_rels/sheet1.xml.rels": (
        f'<Relationships><Relationship Id="rIdD" Type="{RELNS}/drawing"'
        ' Target="../drawings/drawing1.xml"/></Relationships>'
    ).encode(),
    "xl/drawings/drawing1.xml": (
        f'<xdr:wsDr xmlns:xdr="{SHEET_DRAWING}" xmlns:a="{DRAWINGML}"'
        f' xmlns:r="{RELNS}"><xdr:oneCellAnchor>'
        "<xdr:from><xdr:col>0</xdr:col><xdr:colOff>0</xdr:colOff>"
        "<xdr:row>2</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>"
        '<xdr:ext cx="914400" cy="457200"/>'
        '<xdr:pic><xdr:nvPicPr><xdr:cNvPr id="2" name="Picture 2"/>'
        "<xdr:cNvPicPr/></xdr:nvPicPr>"
        '<xdr:blipFill><a:blip r:embed="rIdI"/><a:stretch/></xdr:blipFill>'
        "<xdr:spPr/></xdr:pic>"
        "<xdr:clientData/></xdr:oneCellAnchor></xdr:wsDr>"
    ).encode(),
    "xl/drawings/_rels/drawing1.xml.rels": (
        f'<Relationships><Relationship Id="rIdI" Type="{RELNS}/image"'
        ' Target="../media/image1.png"/></Relationships>'
    ).encode(),
}
DRAWING_CT: Final = b"application/vnd.openxmlformats-officedocument.drawing+xml"
XLSX_IMAGE_TYPES: Final = (
    PNG_DEFAULT
    + b'<Override PartName="/xl/drawings/drawing1.xml" ContentType="'
    + DRAWING_CT
    + b'"/>'
)


def xlsx_with_image(member: bytes) -> bytes:
    return office_package(
        XLSX_IMAGE_PARTS | {"xl/media/image1.png": member},
        overrides=XLSX_IMAGE_TYPES,
    )


def embedded_image_settings(local_ocr: ExtractionSettings) -> ExtractionSettings:
    return ExtractionSettings(
        processing_mode=local_ocr.processing_mode,
        ocr=local_ocr.ocr,
        embedded_image_ocr=True,
    )


def test_an_xlsx_embedded_image_is_examined_only_under_settings_that_say_so(
    local_ocr: ExtractionSettings,
) -> None:
    """The trusted settings reach the Excel path too, and nothing else decides
    it: a profile that examines no embedded image publishes the structural
    extraction alone, and the same bytes under a profile that does are read with
    the engine the profile pinned. The cell beside the picture is untouched
    either way."""
    image = io.BytesIO()
    Image.new("RGB", (240, 60), "white").save(image, format="PNG")
    package = xlsx_with_image(image.getvalue())

    structural = extract_document(
        package, media_type=XLSX_MEDIA_TYPE, capture_id=CAPTURE
    )
    assert structural.state == "succeeded"
    assert [item.semantic_role for item in structural.segments] == [
        "table_cell",
        "object_inventory",
    ]
    record = structural.segments[1].structured_value
    assert (record["imageDecoded"], record["ocrPerformed"]) == (False, False)
    assert record["ocrOutcome"] == "embedded_image_ocr_disabled"

    examined = extract_document(
        package,
        media_type=XLSX_MEDIA_TYPE,
        capture_id=CAPTURE,
        settings=embedded_image_settings(local_ocr),
    )
    assert examined.state == "succeeded"
    assert [item.semantic_role for item in examined.segments] == [
        "table_cell",
        "object_inventory",
    ]
    record = examined.segments[1].structured_value
    # A blank raster yields no reliable text, and that is a reading of it: the
    # bytes were decoded and the engine the profile pinned ran over them.
    assert (record["imageDecoded"], record["ocrPerformed"]) == (True, True)
    assert examined.limitations["embeddedImages"] == 1


def test_an_xlsx_whose_embedded_image_cannot_be_read_is_partial_not_a_success(
    local_ocr: ExtractionSettings,
) -> None:
    """The structural extraction stands and the source is not refused, but a
    raster the decoder could not read is coverage this workbook never reached,
    so it is partial and says which coverage was lost. The cell is published."""
    package = xlsx_with_image(b"\x89PNG\r\n\x1a\nnot really an image")

    result = extract_document(
        package,
        media_type=XLSX_MEDIA_TYPE,
        capture_id=CAPTURE,
        settings=embedded_image_settings(local_ocr),
    )

    assert result.state == "partial"
    assert result.limitations["documentOutcome"] == "partial_coverage"
    assert result.limitations["embeddedImageOutcomes"] == {
        "image_could_not_be_decoded": 1
    }
    # No *cell* failed, and the cell coverage does not claim one did: the
    # image's own outcome is reported where its own unit is counted.
    assert result.coverage["units"][0]["failed"] == 0
    assert [item.semantic_role for item in result.segments] == [
        "table_cell",
        "object_inventory",
    ]


PRESENTATIONML: Final = "http://schemas.openxmlformats.org/presentationml/2006/main"
PPTX_IMAGE_PARTS: Final = {
    "ppt/presentation.xml": (
        f'<p:presentation xmlns:p="{PRESENTATIONML}" xmlns:r="{RELNS}">'
        '<p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst></p:presentation>'
    ).encode(),
    "ppt/_rels/presentation.xml.rels": (
        b'<Relationships><Relationship Id="rId1" Target="slides/slide1.xml"/>'
        b"</Relationships>"
    ),
    "ppt/slides/slide1.xml": (
        f'<p:sld xmlns:p="{PRESENTATIONML}" xmlns:a="{DRAWINGML}" xmlns:r="{RELNS}">'
        "<p:cSld><p:spTree>"
        '<p:sp><p:nvSpPr><p:cNvPr id="3" name="Title 3"/><p:cNvSpPr/><p:nvPr/>'
        "</p:nvSpPr><p:spPr/><p:txBody><a:bodyPr/><a:p><a:r><a:t>Review</a:t>"
        "</a:r></a:p></p:txBody></p:sp>"
        '<p:pic><p:nvPicPr><p:cNvPr id="2" name="Picture 2"/><p:cNvPicPr/>'
        "<p:nvPr/></p:nvPicPr>"
        '<p:blipFill><a:blip r:embed="rIdI"/><a:stretch/></p:blipFill>'
        "<p:spPr/></p:pic>"
        "</p:spTree></p:cSld></p:sld>"
    ).encode(),
    "ppt/slides/_rels/slide1.xml.rels": (
        f'<Relationships><Relationship Id="rIdI" Type="{RELNS}/image"'
        ' Target="../media/image1.png"/></Relationships>'
    ).encode(),
}


def pptx_with_image(member: bytes) -> bytes:
    return office_package(
        PPTX_IMAGE_PARTS | {"ppt/media/image1.png": member}, overrides=PNG_DEFAULT
    )


def test_a_pptx_embedded_image_is_examined_only_under_settings_that_say_so(
    local_ocr: ExtractionSettings,
) -> None:
    """The trusted settings reach the slide path too, and nothing else decides
    it: a profile that examines no embedded image publishes the structural
    extraction alone, and the same bytes under a profile that does are read with
    the engine the profile pinned. The slide's own text is untouched either
    way."""
    image = io.BytesIO()
    Image.new("RGB", (240, 60), "white").save(image, format="PNG")
    package = pptx_with_image(image.getvalue())

    structural = extract_document(
        package, media_type=PPTX_MEDIA_TYPE, capture_id=CAPTURE
    )
    assert structural.state == "succeeded"
    assert [item.semantic_role for item in structural.segments] == [
        "body",
        "object_inventory",
    ]
    record = structural.segments[1].structured_value
    assert (record["imageDecoded"], record["ocrPerformed"]) == (False, False)
    assert record["ocrOutcome"] == "embedded_image_ocr_disabled"

    examined = extract_document(
        package,
        media_type=PPTX_MEDIA_TYPE,
        capture_id=CAPTURE,
        settings=embedded_image_settings(local_ocr),
    )
    assert examined.state == "succeeded"
    assert [item.semantic_role for item in examined.segments] == [
        "body",
        "object_inventory",
    ]
    record = examined.segments[1].structured_value
    # A blank raster yields no reliable text, and that is a reading of it: the
    # bytes were decoded and the engine the profile pinned ran over them.
    assert (record["imageDecoded"], record["ocrPerformed"]) == (True, True)
    assert examined.limitations["embeddedImages"] == 1


def test_a_pptx_whose_embedded_image_cannot_be_read_is_partial_not_a_success(
    local_ocr: ExtractionSettings,
) -> None:
    """The structural extraction stands and the source is not refused, but a
    raster the decoder could not read is coverage this presentation never
    reached, so it is partial and says which coverage was lost. The slide's own
    text is published."""
    package = pptx_with_image(b"\x89PNG\r\n\x1a\nnot really an image")

    result = extract_document(
        package,
        media_type=PPTX_MEDIA_TYPE,
        capture_id=CAPTURE,
        settings=embedded_image_settings(local_ocr),
    )

    assert result.state == "partial"
    assert result.limitations["documentOutcome"] == "partial_coverage"
    assert result.limitations["embeddedImageOutcomes"] == {
        "image_could_not_be_decoded": 1
    }
    # No *slide* failed, and the slide coverage does not claim one did: the
    # image's own outcome is reported where its own unit is counted.
    assert result.coverage["units"][0]["failed"] == 0
    assert [item.semantic_role for item in result.segments] == [
        "body",
        "object_inventory",
    ]


def rtf_with_picture(member: bytes, *, declared: bytes = b"\\pngblip") -> bytes:
    """One RTF source whose body holds prose and one picture group."""
    return (
        b"{\\rtf1\\ansi\\ansicpg1252 Invoice total\\par {\\pict"
        + declared
        + b"\\picw120\\pich40 "
        + member.hex().encode("ascii")
        + b"}}"
    )


def test_an_rtf_embedded_image_is_examined_only_under_settings_that_say_so(
    local_ocr: ExtractionSettings,
) -> None:
    """The trusted settings reach the RTF path too, and nothing else decides it:
    a profile that examines no embedded image publishes the structural
    extraction and the scan's inventory alone, and the same bytes under a
    profile that does are read with the engine the profile pinned. The body text
    the pinned reader extracted is untouched either way."""
    image = io.BytesIO()
    draw = Image.new("RGB", (360, 90), "white")
    ImageDraw.Draw(draw).text(
        (12, 24), "INVOICE", fill="black", font=ImageFont.load_default(size=40)
    )
    draw.save(image, format="PNG")
    content = rtf_with_picture(image.getvalue())

    structural = extract_document(
        content, media_type="application/rtf", capture_id=CAPTURE
    )
    assert structural.state == "succeeded"
    assert structural.limitations["embeddedImages"] == 1
    assert structural.limitations["embeddedImageOutcomes"] == {
        "embedded_image_ocr_disabled": 1
    }
    [record] = structural.limitations["pictureGroups"]
    assert (record["imageDecoded"], record["ocrPerformed"]) == (False, False)
    assert record["declaredType"] == "pngblip"
    assert all(item.extraction_method != "ocr" for item in structural.segments)

    examined = extract_document(
        content,
        media_type="application/rtf",
        capture_id=CAPTURE,
        settings=embedded_image_settings(local_ocr),
    )
    assert examined.state == "succeeded"
    [reading] = [item for item in examined.segments if item.extraction_method == "ocr"]
    assert "INVOICE" in (reading.text or "")
    # The reading is anchored to the group the scanner delimited, not to the
    # reader's derived AST path.
    locator = dict(reading.source_locator)
    assert locator["story"] == "body"
    assert content[int(locator["byteStart"])] == 0x7B
    assert examined.limitations["embeddedImageOutcomes"] == {"ocr_text": 1}


def test_an_rtf_whose_embedded_image_cannot_be_read_is_partial_not_a_success(
    local_ocr: ExtractionSettings,
) -> None:
    """The scan and the pinned reader stand and the source is not refused, but a
    payload the decoder could not read is coverage this source never reached, so
    it is partial and says which coverage was lost."""
    result = extract_document(
        rtf_with_picture(b"\x89PNG\r\n\x1a\nnot really an image"),
        media_type="application/rtf",
        capture_id=CAPTURE,
        settings=embedded_image_settings(local_ocr),
    )

    assert result.state == "partial"
    assert result.limitations["documentOutcome"] == "partial_coverage"
    assert result.limitations["embeddedImageOutcomes"] == {
        "image_could_not_be_decoded": 1
    }
    # No *block* failed, and the block coverage does not claim one did.
    assert result.coverage["units"][0]["failed"] == 0
    assert "Invoice total" in " ".join(item.text or "" for item in result.segments)


def test_an_html_inline_image_is_examined_only_under_settings_that_say_so(
    local_ocr: ExtractionSettings,
) -> None:
    """The trusted settings reach the markup path too. A reference naming
    anything outside the supplied bytes stays unresolved under either profile:
    only the bytes the page carries in the attribute are ever read."""
    image = io.BytesIO()
    draw = Image.new("RGB", (360, 90), "white")
    ImageDraw.Draw(draw).text(
        (12, 24), "INVOICE", fill="black", font=ImageFont.load_default(size=40)
    )
    draw.save(image, format="PNG")
    inline = base64.b64encode(image.getvalue()).decode("ascii")
    content = (
        b'<p>Invoice total</p><img alt="Scanned" src="data:image/png;base64,'
        + inline.encode("ascii")
        + b'"><img alt="Logo" src="file:///etc/logo.png">'
    )

    structural = extract_document(content, media_type="text/html", capture_id=CAPTURE)
    assert structural.state == "succeeded"
    assert structural.limitations["embeddedImageOutcomes"] == {
        "embedded_image_ocr_disabled": 1
    }

    examined = extract_document(
        content,
        media_type="text/html",
        capture_id=CAPTURE,
        settings=embedded_image_settings(local_ocr),
    )
    assert examined.state == "succeeded"
    assert examined.limitations["embeddedImageOutcomes"] == {"ocr_text": 1}
    roles = [(item.semantic_role, item.extraction_method) for item in examined.segments]
    assert roles == [
        ("body", "direct"),
        ("image_alt", "direct"),
        ("object_inventory", "inventory"),
        ("body", "ocr"),
        ("image_alt", "direct"),
        ("object_inventory", "inventory"),
    ]
    assert "INVOICE" in (examined.segments[3].text or "")
    # The ``file:`` reference is inventoried exactly as it was, unresolved, and
    # the payload is never published.
    assert examined.segments[5].structured_value == {
        "kind": "image",
        "scheme": "file",
        "target": "file:///etc/logo.png",
        "resolved": False,
    }
    assert examined.segments[2].structured_value["target"] == "data:image/png;base64,"
    assert inline[:64] not in to_canonical_json(
        {
            "segments": [
                [dict(item.source_locator), item.text, item.structured_value]
                for item in examined.segments
            ]
        }
    )


def test_an_html_inline_image_that_cannot_be_read_is_partial_not_a_success(
    local_ocr: ExtractionSettings,
) -> None:
    result = extract_document(
        b'<p>Invoice total</p><img src="data:image/png;base64,QUJD">',
        media_type="text/html",
        capture_id=CAPTURE,
        settings=embedded_image_settings(local_ocr),
    )

    assert result.state == "partial"
    assert result.limitations["documentOutcome"] == "partial_coverage"
    assert result.limitations["embeddedImageOutcomes"] == {
        "image_could_not_be_decoded": 1
    }
    assert result.segments[0].text == "Invoice total"


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


# --------------------------------------------------------------------------
# DOC-06: the bounded RTF lexical scan and its coverage inventory
# --------------------------------------------------------------------------
#
# The scan is adversarial input handling: every fixture below is a construct
# that a naive byte loop reads as structure when it is data, or as data when it
# is structure. Each one also pins the honest coverage claim, because the pinned
# reader extracts body text only and the inventory is the only place a source's
# headers, notes, revisions, fields and objects are stated at all.


def rtf(content: bytes) -> ExtractionOutcome:
    return extract_document(content, media_type="application/rtf", capture_id=CAPTURE)


#: A ``{\object}`` whose ``\bin`` payload is nothing but structural bytes:
#: braces and backslashes that a scanner reading them as syntax would follow
#: into a wrong group depth and an unbalanced refusal.
BINARY_OBJECT: Final = (
    b"{\\rtf1\\ansi{\\object\\objemb{\\*\\objclass Excel.Sheet}"
    b"{\\*\\objdata \\bin12 {\\}\\\\{{{\\\\ab}}body\\par}"
)
#: Headers, footers, a footnote, an annotation, revision and deleted runs, one
#: ignorable and one substantive unknown destination, and a body paragraph.
FEATURE_RICH: Final = (
    rb"{\rtf1\ansi{\header hdr}{\footer ftr}{\footnote note}{\*\annotation cmt}"
    rb"\revised rev\deleted gone{\*\mydest ignorable}{\myother substantive}body\par}"
)
#: Two fields: an external hyperlink and an INCLUDETEXT of a local path, each
#: with a cached result the reader does extract.
EXTERNAL_FIELDS: Final = (
    rb"{\rtf1\ansi{\field\flddirty{\*\fldinst HYPERLINK "
    rb"\"http://example.invalid/leak\"}{\fldrslt link}}"
    rb"{\field{\*\fldinst INCLUDETEXT c:\\secret\\payload.doc}{\fldrslt cached}}\par}"
)


def test_rtf_scan_reads_escaped_syntax_unicode_and_declared_code_pages() -> None:
    escaped = rtf(
        rb"{\rtf1\ansi\ansicpg1252 A \{brace\} a \\ backslash"
        rb" \'41\u8364?\uc2\u8364??\par}"
    )

    # Escaped braces and backslashes are text, not group structure, and the
    # signed Unicode control words consume exactly their declared fallbacks.
    assert escaped.state == "succeeded"
    assert escaped.segments[0].text == "A {brace} a \\ backslash A\u20ac\u20ac"
    assert escaped.limitations["groups"] == 1
    assert escaped.limitations["escapedLiterals"] == 3
    assert escaped.limitations["hexEscapes"] == 1
    assert escaped.limitations["unicodeEscapes"] == 2
    assert escaped.limitations["declaredCharsets"] == ["ansi"]
    assert escaped.limitations["declaredCodePages"] == [1252]

    legacy = rtf(rb"{\rtf1\mac\ansicpg10000\deff0 caf\'e9\par}")

    assert legacy.limitations["declaredCharsets"] == ["mac"]
    assert legacy.limitations["declaredCodePages"] == [10000]


def test_rtf_scan_skips_binary_payloads_holding_structural_bytes() -> None:
    result = rtf(BINARY_OBJECT)

    # The payload's braces and backslashes were skipped as a block, so the
    # group accounting is the document's and not the object data's.
    assert result.limitations["groups"] == 4
    assert result.limitations["maximumGroupDepth"] == 3
    assert result.limitations["binarySections"] == 1
    assert result.limitations["binaryBytes"] == 12
    assert result.limitations["inventory"] == {
        "object": 1,
        "object_class": 1,
        "object_data": 1,
        "object_embedded": 1,
    }
    # The embedded object is inventoried and never executed; only the body text
    # outside it is extracted.
    assert [item.text for item in result.segments] == ["body"]
    assert result.limitations["objectAndFieldHandling"] == (
        "inventoried_without_execution"
    )


def test_rtf_scan_inventories_notes_revisions_and_unknown_destinations() -> None:
    result = rtf(FEATURE_RICH)

    assert result.limitations["inventory"] == {
        "annotation": 1,
        "deleted_content": 1,
        "footer": 1,
        "footnote": 1,
        "header": 1,
        "revision_marked": 1,
    }
    # A ``\*``-marked unknown destination is separable from an unmarked one,
    # which is the substantive case: its content reaches the body.
    assert result.limitations["unknownDestinations"] == {"myother": 1}
    assert result.limitations["ignorableUnknownDestinations"] == {"mydest": 1}
    assert result.limitations["ignorableDestinationGroups"] == 2
    # The three coverage classes are distinct, and nothing claims completeness:
    # the reader exposes body text and notes only, which is why the header,
    # footer and annotation text below is absent from the segments.
    assert result.limitations["extractedScope"] == "pandoc_body_text_only"
    assert result.limitations["coverage"] == {
        "supported": ["body_text"],
        "inventoriedDisabled": [
            "annotation",
            "deleted_content",
            "footer",
            "footnote",
            "header",
            "revision_marked",
        ],
        "unsupported": ["mydest", "myother"],
    }
    extracted = " ".join(item.text or "" for item in result.segments)
    assert "hdr" not in extracted
    assert "ftr" not in extracted
    # A footnote is the one inventoried feature the reader does expose, as an
    # annotation-visibility note segment ahead of the paragraph that holds it.
    assert [
        (item.semantic_role, item.visibility, item.text)
        for item in result.segments
        if item.semantic_role == "note"
    ] == [("note", "annotation", "note")]
    assert "note" not in " ".join(
        item.text or "" for item in result.segments if item.semantic_role == "body"
    )
    assert "cmt" not in extracted


def test_rtf_scan_inventories_nested_table_markers() -> None:
    result = rtf(rb"{\rtf1\ansi\trowd\itap2 inner\nestcell{\*\nesttableprops\nestrow}}")

    assert result.limitations["inventory"] == {"nested_table": 4}


def test_rtf_scan_names_field_risks_without_resolving_or_executing_them() -> None:
    result = rtf(EXTERNAL_FIELDS)

    # Only the instruction keyword is classified. The URL and the local path are
    # never read out of the bytes, so they cannot appear in the inventory, and
    # nothing resolves or fetches either one.
    assert result.limitations["fieldRisks"] == {"HYPERLINK": 1, "INCLUDETEXT": 1}
    assert result.limitations["inventory"] == {
        "field": 2,
        "field_dirty": 1,
        "field_instruction": 2,
        "field_result": 2,
    }
    assert result.limitations["pathAndUrlHandling"] == (
        "inventoried_without_resolution"
    )
    recorded = to_canonical_json(dict(result.limitations))
    assert "example.invalid" not in recorded
    assert "payload.doc" not in recorded
    # The cached results the source already carried are extracted as text; the
    # instructions that would produce them are not run.
    assert [item.text for item in result.segments if item.text] == ["linkcached"]
    # Pandoc preserves the cached link object but does not expose the stripped
    # field operand as its target. The resulting empty target remains inert and
    # is classified as relative; the INCLUDETEXT path has no entry at all.
    assert [
        (
            item.semantic_role,
            item.structured_value["scheme"],
            item.structured_value["resolved"],
        )
        for item in result.segments
        if item.structured_value
    ] == [("object_inventory", "relative", False)]


def test_rtf_scan_inventory_is_deterministic_and_canonical() -> None:
    first = rtf(FEATURE_RICH).limitations
    second = rtf(FEATURE_RICH).limitations

    assert dict(first) == dict(second)
    assert to_canonical_json(dict(first)) == to_canonical_json(dict(second))
    # Every name-keyed part is emitted in sorted order, so the serialized
    # inventory does not depend on the order the document happened to use.
    for part in ("inventory", "unknownDestinations", "ignorableUnknownDestinations"):
        names = list(first[part])
        assert names == sorted(names)


def test_rtf_scan_output_stays_bounded_when_the_source_names_more() -> None:
    names = rtf_extractor.MAX_INVENTORY_NAMES
    # A control word carries no digits in its name, so the destinations are
    # distinguished by a two-letter suffix.
    content = (
        rb"{\rtf1\ansi"
        + b"".join(
            b"{\\cpg%d x}{\\dst%c%c x}"
            % (3000 + ordinal, 97 + ordinal // 26, 97 + ordinal % 26)
            for ordinal in range(names + 8)
        )
        + rb"body\par}"
    )

    result = rtf(content)

    # Both name-keyed parts stop at the bound and say how many went unnamed, so
    # a source cannot grow the inventory by declaring more of either.
    assert len(result.limitations["declaredCodePages"]) == names
    assert result.limitations["unnamedCodePages"] == 8
    assert len(result.limitations["unknownDestinations"]) == names
    assert result.limitations["unnamedUnknownDestinations"] == 8


def test_rtf_reader_arguments_stay_fixed_and_carry_no_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], bytes]] = []

    def record(arguments: list[str], **keywords: object) -> Any:
        calls.append((arguments, keywords["input"]))  # type: ignore[arg-type]
        return subprocess.CompletedProcess(arguments, 0, b'{"blocks":[]}', b"")

    monkeypatch.setattr(rtf_extractor.subprocess, "run", record)

    rtf(rb"{\rtf1\ansi body\par}")

    # The reader is invoked with the pinned argument vector and the bytes on
    # stdin: no document-derived argument, no path on the command line, no
    # shell, and the sandbox flag retained.
    assert calls == [
        (["pandoc", "--from=rtf", "--to=json", "--sandbox"], rb"{\rtf1\ansi body\par}")
    ]


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"not an rtf file", "signature"),
        (rb"{\rtf1\ansi text\\" + b"\\", "ends in an escape"),
        (rb"{\rtf1\ansi {unclosed}", "unbalanced"),
        (rb"{\rtf1\ansi }}", "unbalanced"),
        (rb"{\rtf1\ansi \'zz}", "hex escape is invalid"),
        (rb"{\rtf1\ansi \'4", "hex escape is truncated"),
        (rb"{\rtf1\ansi \bin900 short}", "binary section is malformed"),
        (rb"{\rtf1\ansi \bin-4 x}", "binary section is malformed"),
        (rb"{\rtf1\ansi \u ?}", "no code point"),
        (b"{\\rtf1\\ansi \\fs" + b"9" * 11 + b" x}", "parameter exceeds"),
        (b"{\\rtf1\\ansi \\" + b"a" * 33 + b" x}", "control word exceeds"),
        (b"{\\rtf1" + b"{" * 200 + b"}" * 200 + b"}", "nesting exceeds"),
        (b"{\\rtf1" + b"a" * rtf_extractor.MAX_SOURCE_BYTES, "source exceeds"),
    ],
)
def test_rtf_malformed_and_out_of_bound_sources_are_refused_before_the_reader(
    content: bytes, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*arguments: object, **keywords: object) -> Any:
        raise AssertionError("the reader must not run on a refused source")

    monkeypatch.setattr(rtf_extractor.subprocess, "run", refuse)

    with pytest.raises(DocumentExtractionInvalid, match=reason):
        rtf(content)
