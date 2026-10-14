from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from _trusted_ocr import catalogue_over, host_models, stand_in_models
from omnivia_core_runtime.service.document_extractors import DOCX_MEDIA_TYPE
from omnivia_core_runtime.service.document_intake import (
    capture_document_bytes,
    read_extraction_projection,
)
from omnivia_core_runtime.service.document_processing import (
    NEEDS_OCR_LANGUAGE,
    TEXT_ONLY_PROFILE_REF,
    DocumentProcessingExecutor,
    portable_profile,
    process_capture_outbox,
    resolve_settings,
)
from omnivia_core_runtime.service.ocr_resources import TrustedOcrCatalogue
from omnivia_core_runtime.service.runner import ServiceRunner
from omnivia_core_runtime.storage import document_intake as store
from PIL import Image, ImageDraw, ImageFont

pytest_plugins = ("test_document_intake",)


@pytest.fixture(scope="module")
def host_catalogue(
    tmp_path_factory: pytest.TempPathFactory,
) -> TrustedOcrCatalogue:
    """A trusted catalogue over models this test copied in and digested itself.

    The packaged catalogue a production process starts with names no model
    directory, so nothing recognises anything until an installation configures
    one. A test that needs real recognition configures its own, by the same
    explicit directory-plus-verified-digest route and never by an ambient
    ``TESSDATA_PREFIX``.
    """
    directory = host_models(tmp_path_factory.mktemp("tessdata"), "eng", "osd")
    if directory is None:  # pragma: no cover - host dependent
        pytest.skip("no local OCR engine with the English and orientation models")
    return catalogue_over(directory, "eng", "osd")


def verified_catalogue(directory: Path, *names: str) -> TrustedOcrCatalogue:
    """A trusted catalogue whose models are never meant to reach the engine."""
    return catalogue_over(stand_in_models(directory, *names), *names)


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


def test_service_executor_publishes_local_image_ocr(
    runner: ServiceRunner, host_catalogue: TrustedOcrCatalogue
) -> None:
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

    results = DocumentProcessingExecutor(
        runner, ocr_catalogue=host_catalogue
    ).run_pending(budget=1, force=True)

    assert [result.result_state for result in results] == ["succeeded"]
    projected = read_extraction_projection(
        runner, document_id="doc-processing-image", query="Captured"
    )
    assert "Captured OCR" in " ".join(segment.text or "" for segment in projected)


def test_a_missing_ocr_language_settles_as_needs_ocr_language(
    runner: ServiceRunner, tmp_path: Path
) -> None:
    """DOC-14: the requested profile survives, and recovery is installing it.

    The installation here has a trusted model directory with verified English
    and orientation models, and the configured selection asks for one it does
    not hold. Nothing is downloaded and English is not substituted.
    """
    catalogue = verified_catalogue(tmp_path, "eng", "osd")
    image = Image.new("RGB", (400, 120), "white")
    ImageDraw.Draw(image).text(
        (20, 20), "Ignored", fill="black", font=ImageFont.load_default(size=60)
    )
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    capture_document_bytes(
        runner,
        content=encoded.getvalue(),
        document_id="doc-processing-language",
        expected_base_capture_id=None,
        operation_key="capture-processing-language",
        media_type="image/png",
        intent_kind=store.INTENT_NEW_CAPTURE,
    )

    results = process_capture_outbox(
        runner, ocr_catalogue=catalogue, ocr_languages=["jpn"]
    )

    assert [result.result_state for result in results] == ["failed"]
    limitations = json.loads(results[0].limitations_json)
    assert limitations["code"] == NEEDS_OCR_LANGUAGE
    assert limitations["missingLanguages"] == ["jpn"]
    assert limitations["recovery"] == "install_language"
    # No silent English substitution and no download: nothing was recognised.
    assert (
        read_extraction_projection(runner, document_id="doc-processing-language") == []
    )
    profile = store.read_profile(
        runner.connection,
        workspace_id=runner.workspace_id,
        digest=_profile_digest(runner, results[0]),
    )
    assert profile is not None
    # The requested profile keeps its identity: the mode that was selected and
    # the language that was asked for, pinning no model it never read.
    assert profile["processingMode"] == "text_and_local_ocr"
    assert profile["ocr"]["languages"] == ["jpn"]
    assert profile["ocr"]["modelHashes"] == []
    assert profile["ocr"]["resourceState"] == "unavailable"


def _profile_digest(runner: ServiceRunner, result: store.StoredResult) -> str:
    assert runner.connection is not None and runner.workspace_id is not None
    revision = store.read_revision(
        runner.connection,
        workspace_id=runner.workspace_id,
        extraction_revision_id=result.extraction_revision_id,
    )
    assert revision is not None
    return revision.profile_digest


def test_an_explicit_text_only_profile_reference_pins_no_ocr_resources(
    tmp_path: Path,
) -> None:
    profile = portable_profile(
        media_type="application/pdf",
        profile_ref=TEXT_ONLY_PROFILE_REF,
        revision="1",
    )

    assert profile["processingMode"] == "text_only"
    assert profile["ocr"] == {
        "engine": "none",
        "engineRevision": "none",
        "modelHashes": [],
        "languages": [],
    }
    assert profile["deduplicationVersion"] == "none-1"
    assert "local_ocr" not in profile["parserFeatures"]
    # The default reference keeps Text and local OCR for the same media type,
    # and an installation with no trusted model directory fails closed under it:
    # no recognition, no claimed model hash, and the state said out loud.
    unconfigured = portable_profile(
        media_type="application/pdf", profile_ref="default", revision="1"
    )
    assert unconfigured["processingMode"] == "text_and_local_ocr"
    assert unconfigured["ocr"]["languages"] == ["eng"]
    assert unconfigured["ocr"]["modelHashes"] == []
    assert unconfigured["ocr"]["resourceState"] == "unavailable"
    assert "local_ocr" not in unconfigured["parserFeatures"]
    # Configure that directory with verified digests and the same reference pins
    # the two models it actually read: English and the orientation data.
    catalogue = verified_catalogue(tmp_path, "eng", "osd")
    configured = portable_profile(
        media_type="application/pdf",
        profile_ref="default",
        revision="1",
        settings=resolve_settings(
            media_type="application/pdf",
            profile_ref="default",
            catalogue=catalogue,
        ),
    )
    assert configured["ocr"]["languages"] == ["eng"]
    assert configured["ocr"]["modelHashes"] == sorted(
        f"sha256:{digest}" for digest in catalogue.installed.values()
    )
    assert "resourceState" not in configured["ocr"]
    assert "local_ocr" in configured["parserFeatures"]
    assert "pdfium-" in configured["decoderBuild"]


def test_an_ordered_language_selection_reaches_the_immutable_profile(
    tmp_path: Path,
) -> None:
    catalogue = verified_catalogue(tmp_path, "eng", "deu", "osd")

    profile = portable_profile(
        media_type="application/pdf",
        profile_ref="default",
        revision="1",
        settings=resolve_settings(
            media_type="application/pdf",
            profile_ref="default",
            catalogue=catalogue,
            languages=["deu", "eng"],
        ),
    )

    assert profile["ocr"]["languages"] == ["deu", "eng"]
    # The digests pinned are the ones taken over the files just verified, with
    # the orientation data the profile always loads alongside them.
    assert profile["ocr"]["modelHashes"] == sorted(
        f"sha256:{digest}" for digest in catalogue.installed.values()
    )


def test_service_executor_processes_rtf_through_the_retained_copy(
    runner: ServiceRunner,
) -> None:
    capture_document_bytes(
        runner,
        content=rb"{\rtf1\ansi\deff0 Core RTF content\par}",
        document_id="doc-processing-rtf",
        expected_base_capture_id=None,
        operation_key="capture-processing-rtf",
        media_type="application/rtf",
        intent_kind=store.INTENT_NEW_CAPTURE,
    )

    results = DocumentProcessingExecutor(runner).run_pending(budget=1, force=True)

    assert [result.result_state for result in results] == ["succeeded"]
    projected = read_extraction_projection(
        runner, document_id="doc-processing-rtf", query="RTF"
    )
    assert "Core RTF content" in " ".join(segment.text or "" for segment in projected)
