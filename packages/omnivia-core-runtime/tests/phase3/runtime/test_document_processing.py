from __future__ import annotations

import io
import zipfile

from omnivia_core_runtime.service.document_extractors import DOCX_MEDIA_TYPE
from omnivia_core_runtime.service.document_intake import (
    capture_document_bytes,
    read_extraction_projection,
)
from omnivia_core_runtime.service.document_processing import (
    DocumentProcessingExecutor,
    process_capture_outbox,
)
from omnivia_core_runtime.service.runner import ServiceRunner
from omnivia_core_runtime.storage import document_intake as store
from PIL import Image, ImageDraw, ImageFont

pytest_plugins = ("test_document_intake",)


def test_pending_text_capture_becomes_a_searchable_projection(
    runner: ServiceRunner,
) -> None:
    capture = capture_document_bytes(
        runner,
        content=b"One searchable sentence.\r\nSecond line.",
        document_id="doc-processing-1",
        expected_base_capture_id=None,
        operation_key="capture-processing-1",
        media_type="text/plain",
        intent_kind=store.INTENT_NEW_CAPTURE,
    )

    results = process_capture_outbox(runner)

    assert [result.result_state for result in results] == ["succeeded"]
    assert results[0].publication_disposition == "promoted"
    projected = read_extraction_projection(
        runner, document_id="doc-processing-1", query="Second"
    )
    assert [segment.text for segment in projected] == [
        "One searchable sentence.\r\nSecond line."
    ]
    assert runner.connection is not None and runner.workspace_id is not None
    outbox = store.read_outbox(
        runner.connection,
        workspace_id=runner.workspace_id,
        capture_id=capture.capture.capture_id,
    )
    assert outbox is not None and outbox.outbox_state == store.OUTBOX_SETTLED


def test_corrupt_pdf_is_preserved_and_settled_without_projection(
    runner: ServiceRunner,
) -> None:
    capture = capture_document_bytes(
        runner,
        content=b"%PDF-1.7\n",
        document_id="doc-processing-pdf",
        expected_base_capture_id=None,
        operation_key="capture-processing-pdf",
        media_type="application/pdf",
        intent_kind=store.INTENT_NEW_CAPTURE,
    )

    results = process_capture_outbox(runner)

    assert [result.result_state for result in results] == ["failed"]
    assert read_extraction_projection(runner, document_id="doc-processing-pdf") == []
    assert runner.connection is not None and runner.workspace_id is not None
    outbox = store.read_outbox(
        runner.connection,
        workspace_id=runner.workspace_id,
        capture_id=capture.capture.capture_id,
    )
    assert outbox is not None and outbox.outbox_state == store.OUTBOX_SETTLED


def test_service_executor_processes_a_bounded_capture_pass(
    runner: ServiceRunner,
) -> None:
    capture_document_bytes(
        runner,
        content=b"Processed by the service-owned executor.",
        document_id="doc-processing-executor",
        expected_base_capture_id=None,
        operation_key="capture-processing-executor",
        media_type="text/plain",
        intent_kind=store.INTENT_NEW_CAPTURE,
    )

    results = DocumentProcessingExecutor(runner).run_pending(budget=1, force=True)

    assert [result.result_state for result in results] == ["succeeded"]
    projected = read_extraction_projection(
        runner, document_id="doc-processing-executor", query="service-owned"
    )
    assert [segment.text for segment in projected] == [
        "Processed by the service-owned executor."
    ]


def test_service_executor_processes_a_retained_docx_package(
    runner: ServiceRunner,
) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="urn:w"><w:body><w:p><w:r><w:t>DOCX in Core</w:t></w:r></w:p></w:body></w:document>',
        )
    capture_document_bytes(
        runner,
        content=buffer.getvalue(),
        document_id="doc-processing-docx",
        expected_base_capture_id=None,
        operation_key="capture-processing-docx",
        media_type=DOCX_MEDIA_TYPE,
        intent_kind=store.INTENT_NEW_CAPTURE,
    )

    results = DocumentProcessingExecutor(runner).run_pending(budget=1, force=True)

    assert [result.result_state for result in results] == ["succeeded"]
    projected = read_extraction_projection(
        runner, document_id="doc-processing-docx", query="DOCX"
    )
    assert [segment.text for segment in projected] == ["DOCX in Core"]


def test_service_executor_publishes_local_image_ocr(runner: ServiceRunner) -> None:
    image = Image.new("RGB", (1000, 240), "white")
    ImageDraw.Draw(image).text(
        (40, 55),
        "Captured OCR",
        fill="black",
        font=ImageFont.load_default(size=92),
    )
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    capture_document_bytes(
        runner,
        content=encoded.getvalue(),
        document_id="doc-processing-image",
        expected_base_capture_id=None,
        operation_key="capture-processing-image",
        media_type="image/png",
        intent_kind=store.INTENT_NEW_CAPTURE,
    )

    results = DocumentProcessingExecutor(runner).run_pending(budget=1, force=True)

    assert [result.result_state for result in results] == ["succeeded"]
    projected = read_extraction_projection(
        runner, document_id="doc-processing-image", query="Captured"
    )
    assert "Captured OCR" in " ".join(segment.text or "" for segment in projected)
