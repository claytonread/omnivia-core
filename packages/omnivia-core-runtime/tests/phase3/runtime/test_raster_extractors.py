"""PDF/raster geometry, spatial deduplication, OCR confidence and frame tests.

The recognition engine is injected as a fake almost everywhere, so the dedup,
confidence, coverage and frame contracts are exercised deterministically rather
than against whatever a host's Tesseract happens to read. One integration smoke
runs the real packaged engine when the environment has it.
"""

from __future__ import annotations

import hashlib
import io
import struct
import zlib
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import pytesseract
import pytest
from _trusted_ocr import catalogue_over, host_tessdata, stand_in_models
from omnivia_core_runtime.service import ocr_resources as ocr
from omnivia_core_runtime.service.document_extractors import (
    _raster_outcome,
    _segments_digest,
)
from omnivia_core_runtime.service.document_intake import ExtractedSegment
from omnivia_core_runtime.service.raster_extractors import (
    DEDUPLICATION_VERSION,
    GEOMETRY_SPACE,
    OCR_DPI,
    OUTCOME_BOTH_DEDUPLICATED,
    OUTCOME_CONFLICTING_READINGS,
    OUTCOME_EMBEDDED_TEXT,
    OUTCOME_EXCLUDED_BY_PROFILE,
    OUTCOME_LOW_CONFIDENCE,
    OUTCOME_NO_RELIABLE_OCR_TEXT,
    OUTCOME_OCR_TEXT,
    OUTCOME_PROCESSING_FAILURE,
    OUTCOME_TRUNCATED_BY_LIMIT,
    ExtractionBudget,
    RasterExtractionInvalid,
    extract_image,
    extract_pdf,
)
from PIL import Image, ImageDraw, ImageFont, TiffImagePlugin

CAPTURE = "dcap-" + "a" * 64
SAMPLE_PDF = "services/omnivia-memory/tests/fixtures/sample.pdf"
AT35_MIXED_PDF = (
    "packages/omnivia-core-runtime/tests/phase3/runtime/fixtures/"
    "at35-mixed-ten-page.pdf"
)
PAGE_WIDTH_POINTS = 595.0
#: The model names the tests install into their own trusted directory.
MODEL_NAMES = ("eng", "deu", "fra", "osd")
#: The words a prior OCR tool left behind in the invisible layer of a scan.
SCAN_LINES = (
    ("Invoice total is 4,210.00", 72.0, 700.0),
    ("Terms thirty days net", 72.0, 660.0),
)


# --------------------------------------------------------------------------- #
# Fixtures and fakes
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def sample_pdf(pytestconfig: pytest.Config) -> bytes:
    return (pytestconfig.rootpath / SAMPLE_PDF).read_bytes()


@pytest.fixture(scope="module")
def at35_mixed_pdf(pytestconfig: pytest.Config) -> bytes:
    return (pytestconfig.rootpath / AT35_MIXED_PDF).read_bytes()


@pytest.fixture(scope="module")
def tessdata(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One trusted model directory holding stand-in bytes for each model name."""
    return stand_in_models(tmp_path_factory.mktemp("tessdata"), *MODEL_NAMES)


@pytest.fixture
def local_catalogue(tessdata: Path) -> ocr.TrustedOcrCatalogue:
    return catalogue_over(tessdata, *MODEL_NAMES)


@pytest.fixture(autouse=True)
def packaged(
    monkeypatch: pytest.MonkeyPatch, tessdata: Path
) -> ocr.TrustedOcrCatalogue:
    """A verified catalogue standing in for the signed bundle WP-14 installs.

    The production default pins nothing and names no model directory, so it
    fails closed; every extraction that relies on the packaged catalogue has to
    be handed a verified one, and these tests hand it one they own.
    """
    installed = catalogue_over(tessdata, "eng", "osd")
    monkeypatch.setattr(ocr, "PACKAGED_OCR_CATALOGUE", installed)
    return installed


def two_page_pdf(content: bytes) -> bytes:
    source = pdfium.PdfDocument(content)
    document = pdfium.PdfDocument.new()
    document.import_pages(source, [0])
    document.import_pages(source, [0])
    buffer = io.BytesIO()
    document.save(buffer)
    document.close()
    source.close()
    return buffer.getvalue()


def rotated_pdf(content: bytes, degrees: int) -> bytes:
    document = pdfium.PdfDocument(content)
    document[0].set_rotation(degrees)
    buffer = io.BytesIO()
    document.save(buffer)
    document.close()
    return buffer.getvalue()


def scanned_pdf(
    lines: Sequence[tuple[str, float, float]], *, render_mode: int = 3
) -> bytes:
    """A page whose visible presentation is a raster, plus a text layer.

    This is the shape a prior OCR tool leaves behind: the scan itself is an
    image drawn over the whole page, and the words it recognised are written as
    real page text in rendering mode 3, which paints no glyphs. ``render_mode``
    is a parameter only so the same page can be built visibly, which is what
    shows the raster path would have drawn the glyphs had they been visible.
    Hand-built because no writer here emits mode 3, and bounded to one 2x2
    greyscale raster and the base-14 font so the bytes stay deterministic.

    ``lines`` are ``(text, x, y)`` in PDF user space, bottom-left origin.
    """
    raster = zlib.compress(bytes([0xFF, 0x40, 0x40, 0xFF]))
    content = ["q 595 0 0 842 0 0 cm /Im0 Do Q"]
    if lines:
        content.append(f"BT /F1 12 Tf {render_mode} Tr")
        content += [f"1 0 0 1 {x} {y} Tm ({text}) Tj" for text, x, y in lines]
        content.append("ET")
    stream = "\n".join(content).encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 5 0 R >> /XObject << /Im0 6 0 R >> >> "
            b"/Contents 4 0 R >>"
        ),
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /XObject /Subtype /Image /Width 2 /Height 2 "
        b"/ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode "
        b"/Length %d >>\nstream\n" % len(raster) + raster + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.7\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        start,
    )
    return bytes(out)


def rendered_page(content: bytes) -> bytes:
    """The first page's rasterised pixels, as the decoder actually paints them."""
    document = pdfium.PdfDocument(content)
    page = document[0]
    image = page.render(scale=0.5).to_pil()
    try:
        return image.tobytes()
    finally:
        image.close()
        page.close()
        document.close()


def native_lines(
    content: bytes,
) -> list[tuple[str, tuple[float, float, float, float]]]:
    """The embedded text lines and their common-space boxes, read text-only."""
    result = extract_pdf(content, capture_id=CAPTURE, settings=ocr.text_only_settings())
    lines: list[tuple[str, tuple[float, float, float, float]]] = []
    for segment in result.segments:
        left, top, width, height = segment.source_locator["box"]
        lines.append((segment.text or "", (left, top, width, height)))
    return lines


class FakeOcr:
    """An injected engine returning fixed word rows, in Tesseract's TSV shape."""

    def __init__(
        self,
        *,
        by_pass: dict[int, list[tuple[str, float, tuple[float, float, float, float]]]]
        | None = None,
        words: list[tuple[str, float, tuple[float, float, float, float]]] | None = None,
        page_width_points: float = PAGE_WIDTH_POINTS,
        raise_after: int | None = None,
    ) -> None:
        self.by_pass = by_pass if by_pass is not None else {3: words or [], 6: []}
        self.page_width_points = page_width_points
        self.raise_after = raise_after
        self.calls: list[int] = []

    def __call__(
        self,
        image: Image.Image,
        *,
        resources: ocr.OcrResources,
        psm: int,
        timeout_seconds: float,
    ) -> dict[str, list[Any]]:
        self.calls.append(psm)
        if self.raise_after is not None and len(self.calls) > self.raise_after:
            raise pytesseract.TesseractError(1, "injected engine failure")
        scale = image.width / self.page_width_points
        # A container row: no text and a sentinel confidence. Never a word.
        data: dict[str, list[Any]] = {
            "level": [1],
            "text": [""],
            "conf": [-1],
            "left": [0],
            "top": [0],
            "width": [image.width],
            "height": [image.height],
            "block_num": [0],
            "par_num": [0],
            "line_num": [0],
        }
        for line_number, (text, confidence, box) in enumerate(
            self.by_pass.get(psm, []), start=1
        ):
            for word_number, word in enumerate(text.split()):
                data["level"].append(5)
                data["text"].append(word)
                data["conf"].append(confidence)
                data["left"].append(round(box[0] * scale) + word_number)
                data["top"].append(round(box[1] * scale))
                data["width"].append(round(box[2] * scale))
                data["height"].append(round(box[3] * scale))
                data["block_num"].append(line_number)
                data["par_num"].append(1)
                data["line_num"].append(line_number)
        return data


def text_image(label: str, size: tuple[int, int] = (900, 220)) -> Image.Image:
    image = Image.new("RGB", size, "white")
    ImageDraw.Draw(image).text(
        (30, 50), label, fill="black", font=ImageFont.load_default(size=80)
    )
    return image


def encoded(image: Image.Image, image_format: str, **options: Any) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=image_format, **options)
    return buffer.getvalue()


def png_header_only(width: int, height: int) -> bytes:
    """A structurally valid PNG header for a huge image, with no pixel data.

    Dimensions have to be refused from the header, before anything is decoded.
    """

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b""))
        + chunk(b"IEND", b"")
    )


def tiff_frames(frames: list[tuple[Image.Image, int]]) -> bytes:
    buffer = io.BytesIO()
    with TiffImagePlugin.AppendingTiffWriter(buffer) as writer:
        for image, subfile_type in frames:
            image.save(writer, format="TIFF", tiffinfo={254: subfile_type})
            writer.newFrame()
    return buffer.getvalue()


def outcomes(result: Any) -> dict[str, int]:
    return result.limitations["unitOutcomes"]


def searchable(result: Any) -> list[str]:
    return [
        segment.text or ""
        for segment in result.segments
        if segment.semantic_role == "body"
    ]


# --------------------------------------------------------------------------- #
# DOC-13: engine, model and language identity
# --------------------------------------------------------------------------- #

def test_english_is_the_default_and_orientation_data_is_pinned_with_it(
    packaged: ocr.TrustedOcrCatalogue,
) -> None:
    resources = packaged.resolve()

    assert resources.available and resources.languages == ("eng",)
    assert resources.engine == "tesseract"
    # English plus the orientation/script model the profile pins alongside it.
    assert len(resources.model_hashes) == 2
    assert resources.model_hashes == tuple(sorted(set(resources.model_hashes)))


def test_the_production_default_serves_nothing_until_an_installation_fills_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DOC-16: no manifest and no trusted directory means no recognition at all."""
    # Drop the verified stand-in the autouse fixture installed, so this reads
    # the catalogue a production process actually starts with.
    monkeypatch.undo()
    resources = ocr.PACKAGED_OCR_CATALOGUE.resolve()

    assert not resources.available
    assert resources.reason == ocr.REASON_UNCONFIGURED
    # No model hash is claimed, because no model was read.
    assert resources.model_hashes == ()
    assert not ocr.local_ocr_settings().runs_ocr
    with pytest.raises(ocr.OcrResourceRefused):
        ocr.run_tesseract(
            Image.new("RGB", (10, 10), "white"), resources=resources, psm=3
        )


def test_an_explicit_language_is_needs_language_even_with_nothing_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DOC-14: the user-facing condition is the language, not the installation.

    Asking for a language an unconfigured installation cannot serve is still a
    missing language, recovered by installing it. The narrower fault is kept as
    the operator's diagnostic rather than shown in place of the recoverable one.
    """
    monkeypatch.undo()
    resources = ocr.PACKAGED_OCR_CATALOGUE.resolve(["jpn"])

    assert not resources.available
    assert resources.reason == ocr.REASON_NEEDS_LANGUAGE
    assert resources.languages == ("jpn",)
    assert resources.model_hashes == ()
    assert resources.to_limitations() == {
        "code": ocr.REASON_NEEDS_LANGUAGE,
        "recovery": "install_language",
        "requestedLanguages": ["jpn"],
        "missingLanguages": ["jpn"],
        "resourceDiagnostic": ocr.REASON_UNCONFIGURED,
    }


def test_up_to_three_languages_keep_their_configured_order(
    local_catalogue: ocr.TrustedOcrCatalogue,
) -> None:
    resources = local_catalogue.resolve(["deu", "eng", "fra"])

    assert resources.languages == ("deu", "eng", "fra")
    assert resources.tesseract_lang == "deu+eng+fra"
    assert len(resources.model_hashes) == 4  # three languages plus orientation data


@pytest.mark.parametrize(
    "requested",
    [
        ["eng", "deu", "fra", "spa"],
        ["eng", "eng"],
        ["../eng"],
        ["eng+deu"],
        ["osd"],
        ["e"],
    ],
)
def test_malformed_or_over_limit_language_selections_are_refused(
    local_catalogue: ocr.TrustedOcrCatalogue, requested: list[str]
) -> None:
    with pytest.raises(ocr.OcrSelectionInvalid):
        local_catalogue.resolve(requested)


def test_absent_model_is_a_needs_language_report_not_an_english_fallback(
    local_catalogue: ocr.TrustedOcrCatalogue,
) -> None:
    resources = local_catalogue.resolve(["deu", "jpn"])

    assert not resources.available
    assert resources.reason == ocr.REASON_NEEDS_LANGUAGE
    # The requested order survives intact and English is not substituted.
    assert resources.languages == ("deu", "jpn")
    assert resources.unavailable_models == ("jpn",)
    assert resources.model_hashes == ()
    assert resources.to_limitations() == {
        "code": ocr.REASON_NEEDS_LANGUAGE,
        "recovery": "install_language",
        "requestedLanguages": ["deu", "jpn"],
        "missingLanguages": ["jpn"],
    }


def test_a_model_whose_bytes_do_not_match_the_manifest_is_never_invoked(
    tessdata: Path,
) -> None:
    """DOC-16: a host model is not served under a packaged digest."""
    tampered = ocr.TrustedOcrCatalogue(
        installed={
            **dict(catalogue_over(tessdata, "osd").installed),
            "eng": "f" * 64,
        },
        tessdata_dir=str(tessdata),
    )

    resources = tampered.resolve(["eng"])

    assert not resources.available
    assert resources.reason == ocr.REASON_UNVERIFIED
    assert resources.unavailable_models == ("eng",)
    assert resources.model_hashes == ()


def test_a_selected_model_that_is_absent_or_a_link_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "tessdata"
    directory.mkdir()
    real = directory / "real.bin"
    real.write_bytes(b"model-eng")
    digest = hashlib.sha256(b"model-eng").hexdigest()
    (directory / "eng.traineddata").symlink_to(real)
    catalogue = ocr.TrustedOcrCatalogue(
        installed={"eng": digest}, tessdata_dir=str(directory)
    )

    # The digest matches the linked bytes, and the link is still refused: the
    # bytes digested have to be the bytes the engine opens.
    assert catalogue.resolve(["eng"]).reason == ocr.REASON_UNVERIFIED
    absent = ocr.TrustedOcrCatalogue(
        installed={"deu": digest}, tessdata_dir=str(directory)
    )
    assert absent.resolve(["deu"]).reason == ocr.REASON_UNVERIFIED


def test_a_host_language_outside_the_manifest_is_never_discovered(
    tessdata: Path,
) -> None:
    """Only the manifest's names are selectable; the directory is not scanned."""
    catalogue = catalogue_over(tessdata, "eng", "osd")

    assert (tessdata / "deu.traineddata").is_file()
    assert catalogue.resolve(["deu"]).reason == ocr.REASON_NEEDS_LANGUAGE


def test_a_verified_model_manifest_is_the_only_language_source(
    tessdata: Path,
) -> None:
    class Artifact:
        def __init__(self, name: str, digest: str) -> None:
            self.name, self.sha256 = name, digest

    installed = catalogue_over(tessdata, "eng", "deu", "osd").installed

    class Manifest:
        artifacts = (
            Artifact("eng.traineddata", installed["eng"]),
            Artifact("deu.traineddata", installed["deu"]),
            Artifact("osd.traineddata", installed["osd"]),
            Artifact("notes.txt", "d" * 64),
        )

    catalogue = ocr.TrustedOcrCatalogue.from_model_manifest(
        Manifest(), tessdata_dir=str(tessdata)
    )

    resolved = catalogue.resolve(["deu"])
    assert resolved.available and resolved.tessdata_dir == str(tessdata)
    assert sorted(catalogue.installed) == ["deu", "eng", "osd"]
    assert catalogue.resolve(["jpn"]).reason == ocr.REASON_NEEDS_LANGUAGE


def test_the_engine_invocation_is_built_only_from_trusted_resources(
    monkeypatch: pytest.MonkeyPatch, local_catalogue: ocr.TrustedOcrCatalogue
) -> None:
    """DOC-16: the model directory and language come from the catalogue only."""
    seen: dict[str, Any] = {}

    def capture(image: Any, **kwargs: Any) -> dict[str, list[Any]]:
        seen.update(kwargs)
        return {"text": [], "conf": []}

    monkeypatch.setattr(pytesseract, "image_to_data", capture)
    resources = local_catalogue.resolve(["deu", "eng"])

    ocr.run_tesseract(
        Image.new("RGB", (10, 10), "white"),
        resources=resources,
        psm=3,
        timeout_seconds=5,
    )

    assert seen["lang"] == "deu+eng"
    assert seen["config"] == (
        f"--oem 1 --psm 3 --tessdata-dir {local_catalogue.tessdata_dir}"
    )
    assert seen["timeout"] == 5
    # And the engine itself is the absolute executable the catalogue names,
    # never a bare word resolved against whatever search path the child holds.
    assert pytesseract.pytesseract.tesseract_cmd == local_catalogue.tesseract_path
    assert str(local_catalogue.tesseract_path).startswith("/")


def test_a_text_only_profile_pins_no_engine_model_or_language(
    packaged: ocr.TrustedOcrCatalogue,
) -> None:
    settings = ocr.text_only_settings()

    assert settings.ocr is None and not settings.runs_ocr
    with pytest.raises(ocr.OcrSelectionInvalid):
        ocr.ExtractionSettings(
            processing_mode=ocr.MODE_TEXT_ONLY,
            ocr=packaged.resolve(),
        )


# --------------------------------------------------------------------------- #
# DOC-10 / DOC-11: mixed pages, deduplication and conflicts
# --------------------------------------------------------------------------- #


def test_text_only_mode_reports_embedded_text_and_names_its_exclusion(
    sample_pdf: bytes,
) -> None:
    result = extract_pdf(
        sample_pdf, capture_id=CAPTURE, settings=ocr.text_only_settings()
    )

    assert outcomes(result) == {OUTCOME_EMBEDDED_TEXT: 1}
    assert result.limitations["processingMode"] == "text_only"
    assert result.limitations["ocrEngine"] == "none"
    assert result.limitations["ocrLanguages"] == []
    assert "Test PDF Document" in searchable(result)[0]


def test_overlapping_agreement_is_one_segment_with_two_method_observations(
    sample_pdf: bytes,
) -> None:
    lines = native_lines(sample_pdf)
    engine = FakeOcr(words=[(text, 92.0, box) for text, box in lines])

    result = extract_pdf(sample_pdf, capture_id=CAPTURE, ocr_engine=engine)

    assert outcomes(result) == {OUTCOME_BOTH_DEDUPLICATED: 1}
    # A page with native text is still rastered and recognised: a native
    # heading can sit above a scanned body, and no heuristic may skip it.
    assert engine.calls == [3]
    assert len(searchable(result)) == len(lines)
    # The same words are observed twice and stored once.
    assert searchable(result).count(lines[0][0]) == 1
    primary = result.segments[0]
    assert primary.extraction_method == "direct"
    assert primary.source_locator["methods"] == ["native_text", "ocr"]
    assert primary.source_locator["ocrConfidence"] == pytest.approx(0.92)
    assert result.limitations["deduplication"] == DEDUPLICATION_VERSION


def test_ocr_only_region_below_a_native_header_is_retained(
    sample_pdf: bytes,
) -> None:
    lines = native_lines(sample_pdf)
    engine = FakeOcr(
        words=[
            (lines[0][0], 91.0, lines[0][1]),
            ("Scanned body paragraph", 88.0, (72.0, 400.0, 300.0, 14.0)),
        ]
    )

    result = extract_pdf(sample_pdf, capture_id=CAPTURE, ocr_engine=engine)

    assert outcomes(result) == {OUTCOME_BOTH_DEDUPLICATED: 1}
    recognised = [
        segment
        for segment in result.segments
        if segment.extraction_method == "ocr" and segment.semantic_role == "body"
    ]
    assert [segment.text for segment in recognised] == ["Scanned body paragraph"]
    assert recognised[0].source_locator["space"] == GEOMETRY_SPACE
    assert recognised[0].source_locator["box"][1] == pytest.approx(400.0, abs=1.0)
    assert recognised[0].language == "eng"


def test_reliable_embedded_text_stays_searchable_when_ocr_is_empty(
    sample_pdf: bytes,
) -> None:
    engine = FakeOcr(words=[])

    result = extract_pdf(sample_pdf, capture_id=CAPTURE, ocr_engine=engine)

    assert outcomes(result) == {OUTCOME_EMBEDDED_TEXT: 1}
    assert result.useful_units == 1 and result.known_empty_units == 0
    assert "Test PDF Document" in searchable(result)[0]
    # An empty OCR observation is not a failure and not a blank page.
    assert result.failed_units == 0
    assert result.limitations["units"][0]["ocr"]["acceptedWords"] == 0
    assert result.limitations["units"][0]["ocr"]["sentinelRows"] == 1


def test_material_disagreement_is_a_linked_alternative_not_a_concatenation(
    sample_pdf: bytes,
) -> None:
    lines = native_lines(sample_pdf)
    engine = FakeOcr(words=[("An entirely different reading of it", 80.0, lines[0][1])])

    result = extract_pdf(sample_pdf, capture_id=CAPTURE, ocr_engine=engine)

    assert outcomes(result)[OUTCOME_CONFLICTING_READINGS] == 1
    assert searchable(result)[0] == lines[0][0]
    alternative = next(
        segment
        for segment in result.segments
        if segment.source_locator.get("observation") == "alternative_reading"
    )
    assert alternative.semantic_role == "hidden_content"
    assert alternative.visibility == "hidden"
    assert alternative.source_locator["conflict"] is True
    assert "alternativeOfRectOrdinal" in alternative.source_locator
    assert alternative.text == "An entirely different reading of it"
    # Neither reading was appended to the other.
    assert all("different reading" not in text for text in searchable(result))


def test_identical_text_at_distinct_positions_is_never_deduplicated(
    sample_pdf: bytes,
) -> None:
    engine = FakeOcr(
        words=[
            ("Confidential footer", 90.0, (72.0, 300.0, 180.0, 10.0)),
            ("Confidential footer", 90.0, (72.0, 500.0, 180.0, 10.0)),
            ("Confidential footer", 90.0, (72.0, 700.0, 180.0, 10.0)),
        ]
    )

    result = extract_pdf(sample_pdf, capture_id=CAPTURE, ocr_engine=engine)

    footers = [text for text in searchable(result) if text == "Confidential footer"]
    assert len(footers) == 3
    boxes = {
        tuple(segment.source_locator["box"])
        for segment in result.segments
        if segment.text == "Confidential footer"
    }
    assert len(boxes) == 3


def test_an_invisible_prior_ocr_layer_is_decoded_with_page_and_spatial_provenance() -> (
    None
):
    content = scanned_pdf(SCAN_LINES)

    # The layer really is invisible: the page paints exactly the raster it would
    # paint with no text at all, while the same page built visibly does not.
    assert rendered_page(content) == rendered_page(scanned_pdf(()))
    assert rendered_page(content) != rendered_page(
        scanned_pdf(SCAN_LINES, render_mode=0)
    )

    result = extract_pdf(content, capture_id=CAPTURE, settings=ocr.text_only_settings())

    assert outcomes(result) == {OUTCOME_EMBEDDED_TEXT: 1}
    assert searchable(result) == [text for text, _, _ in SCAN_LINES]
    assert all(
        segment.source_locator["page"] == 1
        and segment.source_locator["method"] == "native_text"
        and segment.source_locator["space"] == GEOMETRY_SPACE
        and segment.source_locator["rotation"] == 0
        and segment.source_locator["cropBox"] == [0.0, 0.0, 595.0, 842.0]
        for segment in result.segments
    )
    # Top-left origin: the higher baseline is the smaller top, and the two
    # lines keep distinct regions rather than one page-wide claim.
    tops = [segment.source_locator["box"][1] for segment in result.segments]
    assert tops == sorted(tops) and len(set(tops)) == 2
    # The same bytes decode to the same geometry, so a reading stays traceable.
    assert native_lines(content) == native_lines(content)


def test_a_reading_agreeing_with_the_invisible_layer_adds_no_second_sentence() -> None:
    content = scanned_pdf(SCAN_LINES)
    lines = native_lines(content)
    engine = FakeOcr(words=[(text, 93.0, box) for text, box in lines])

    result = extract_pdf(content, capture_id=CAPTURE, ocr_engine=engine)

    assert outcomes(result) == {OUTCOME_BOTH_DEDUPLICATED: 1}
    # The raster is recognised even though the page already carries text.
    assert engine.calls == [3]
    assert searchable(result) == [text for text, _, _ in SCAN_LINES]
    primary = result.segments[0]
    assert primary.extraction_method == "direct"
    assert primary.source_locator["methods"] == ["native_text", "ocr"]
    assert primary.source_locator["ocrConfidence"] == pytest.approx(0.93)
    # Two methods read the same words; they are stored once.
    assert len(result.segments) == len(SCAN_LINES)


def test_a_reading_conflicting_with_the_invisible_layer_keeps_both_apart() -> None:
    content = scanned_pdf(SCAN_LINES)
    lines = native_lines(content)
    engine = FakeOcr(words=[("Invoice total is 9,999.00", 81.0, lines[0][1])])

    result = extract_pdf(content, capture_id=CAPTURE, ocr_engine=engine)

    assert outcomes(result)[OUTCOME_CONFLICTING_READINGS] == 1
    # The prior layer's sentence stays the searchable one, unaltered.
    assert searchable(result) == [text for text, _, _ in SCAN_LINES]
    alternative = next(
        segment
        for segment in result.segments
        if segment.source_locator.get("observation") == "alternative_reading"
    )
    assert alternative.text == "Invoice total is 9,999.00"
    assert alternative.semantic_role == "hidden_content"
    assert alternative.visibility == "hidden"
    assert alternative.source_locator["conflict"] is True
    assert alternative.source_locator["alternativeOfRectOrdinal"] == 0
    # Neither amount was appended to the other.
    assert all("9,999.00" not in text for text in searchable(result))


def test_a_repeated_invisible_footer_pairs_each_position_with_its_own_reading() -> None:
    footer = "Confidential footer"
    content = scanned_pdf([(footer, 72, y) for y in (700, 500, 300)])
    lines = native_lines(content)
    assert [text for text, _ in lines] == [footer] * 3
    engine = FakeOcr(words=[(text, 90.0, box) for text, box in lines])

    result = extract_pdf(content, capture_id=CAPTURE, ocr_engine=engine)

    assert outcomes(result) == {OUTCOME_BOTH_DEDUPLICATED: 1}
    assert searchable(result) == [footer] * 3
    # Identical text at distinct positions is three segments, and each one is
    # paired with the reading over its own region rather than a sibling's.
    assert len({tuple(item.source_locator["box"]) for item in result.segments}) == 3
    assert len({item.source_locator["ocrLine"] for item in result.segments}) == 3
    for segment in result.segments:
        assert segment.source_locator["ocrBox"][1] == pytest.approx(
            segment.source_locator["box"][1], abs=1.0
        )


def test_rotated_page_geometry_maps_both_methods_into_one_space(
    sample_pdf: bytes,
) -> None:
    rotated = rotated_pdf(sample_pdf, 90)
    lines = native_lines(rotated)
    # The 90-degree display page is landscape, so a mapped box must be too.
    assert max(box[1] for _, box in lines) < 595.0
    engine = FakeOcr(
        words=[(text, 90.0, box) for text, box in lines],
        page_width_points=842.0,
    )

    result = extract_pdf(rotated, capture_id=CAPTURE, ocr_engine=engine)

    assert outcomes(result) == {OUTCOME_BOTH_DEDUPLICATED: 1}
    assert result.segments[0].source_locator["rotation"] == 90
    assert result.segments[0].source_locator["cropBox"] == [0.0, 0.0, 595.0, 842.0]
    assert result.segments[0].source_locator["pageWidth"] == 842.0


# --------------------------------------------------------------------------- #
# DOC-12: confidence, zero results and coverage
# --------------------------------------------------------------------------- #


def test_below_threshold_candidates_stay_observations_outside_search(
    sample_pdf: bytes,
) -> None:
    engine = FakeOcr(
        words=[
            ("Faint scan line", 20.0, (72.0, 300.0, 180.0, 10.0)),
            ("Clear scan line", 70.0, (72.0, 500.0, 180.0, 10.0)),
        ]
    )

    result = extract_pdf(sample_pdf, capture_id=CAPTURE, ocr_engine=engine)

    assert "Faint scan line" not in searchable(result)
    assert "Clear scan line" in searchable(result)
    observation = next(
        segment
        for segment in result.segments
        if segment.source_locator.get("observation") == "below_confidence_threshold"
    )
    assert observation.semantic_role == "hidden_content"
    assert observation.text == "Faint scan line"
    assert [word["confidence"] for word in observation.source_locator["words"]] == [
        20.0,
        20.0,
        20.0,
    ]


@pytest.mark.parametrize("confidence", [0.0, -1.0, 34.9])
def test_zero_negative_and_sentinel_confidence_never_becomes_certainty(
    sample_pdf: bytes, confidence: float
) -> None:
    engine = FakeOcr(words=[("Unreadable", confidence, (72.0, 300.0, 90.0, 10.0))])

    result = extract_pdf(sample_pdf, capture_id=CAPTURE, ocr_engine=engine)

    observation = next(
        segment
        for segment in result.segments
        if segment.source_locator.get("observation") == "below_confidence_threshold"
    )
    raw = observation.source_locator["words"][0]["confidence"]
    assert raw == confidence
    assert raw != 100.0
    assert observation.confidence == pytest.approx(max(0.0, confidence / 100.0))
    # Embedded text carried the page, so the page is not empty and not failed.
    assert outcomes(result) == {OUTCOME_EMBEDDED_TEXT: 1}


def test_an_empty_ocr_pass_on_an_image_records_no_reliable_ocr_text() -> None:
    engine = FakeOcr(words=[], page_width_points=900.0)

    result = extract_image(
        encoded(text_image("ignored"), "PNG"),
        capture_id=CAPTURE,
        media_type="image/png",
        ocr_engine=engine,
    )

    assert outcomes(result) == {OUTCOME_NO_RELIABLE_OCR_TEXT: 1}
    assert result.useful_units == 0
    assert result.failed_units == 0
    # Known-empty here means nothing reliable was observed, not a proven blank.
    assert result.known_empty_units == 1
    assert (
        result.limitations["knownEmptyMeaning"]
        == "no_reliable_text_observed_not_proven_blank"
    )


def test_a_source_with_no_useful_text_is_not_reported_as_plain_success() -> None:
    engine = FakeOcr(words=[], page_width_points=900.0)

    outcome = _image_outcome(engine)

    assert outcome.state == "partial"
    assert outcome.limitations["documentOutcome"] == "no_reliable_text_found"
    assert outcome.coverage["units"][0]["usefulText"] == 0


def test_low_confidence_is_flagged_by_median_and_by_share(
    sample_pdf: bytes,
) -> None:
    median_below = FakeOcr(words=[("Murky line", 45.0, (72.0, 300.0, 120.0, 10.0))])
    share_below = FakeOcr(
        words=[
            ("Good line", 95.0, (72.0, 300.0, 120.0, 10.0)),
            ("Weak line", 36.0, (72.0, 350.0, 120.0, 10.0)),
        ]
    )
    confident = FakeOcr(words=[("Good line", 95.0, (72.0, 300.0, 120.0, 10.0))])

    flagged = extract_pdf(sample_pdf, capture_id=CAPTURE, ocr_engine=median_below)
    shared = extract_pdf(sample_pdf, capture_id=CAPTURE, ocr_engine=share_below)
    clean = extract_pdf(sample_pdf, capture_id=CAPTURE, ocr_engine=confident)

    assert outcomes(flagged)[OUTCOME_LOW_CONFIDENCE] == 1
    assert outcomes(shared)[OUTCOME_LOW_CONFIDENCE] == 1
    assert OUTCOME_LOW_CONFIDENCE not in outcomes(clean)


def test_one_documented_alternate_segmentation_pass_after_no_reliable_words(
    sample_pdf: bytes,
) -> None:
    engine = FakeOcr(
        by_pass={3: [], 6: [("Second pass line", 90.0, (72.0, 300.0, 150.0, 10.0))]}
    )
    settled = FakeOcr(words=[("First pass line", 90.0, (72.0, 300.0, 150.0, 10.0))])

    retried = extract_pdf(sample_pdf, capture_id=CAPTURE, ocr_engine=engine)
    direct = extract_pdf(sample_pdf, capture_id=CAPTURE, ocr_engine=settled)

    assert engine.calls == [3, 6]
    assert settled.calls == [3]
    assert retried.limitations["units"][0]["ocr"]["segmentationPasses"] == [3, 6]
    assert direct.limitations["units"][0]["ocr"]["segmentationPasses"] == [3]
    assert "Second pass line" in searchable(retried)


def test_output_identity_separates_method_language_and_confidence() -> None:
    """Two readings of one region are two outputs, not one with a detail changed."""

    def reading(**changes: Any) -> tuple[ExtractedSegment, ...]:
        fields: dict[str, Any] = {
            "extraction_method": "ocr",
            "language": "eng",
            "confidence": 0.9,
            **changes,
        }
        return (
            ExtractedSegment(
                "body",
                {"captureId": CAPTURE, "unit": "pdf_page", "number": 1},
                text="The same words",
                **fields,
            ),
        )

    baseline = _segments_digest(reading())

    assert baseline == _segments_digest(reading())  # deterministic
    assert (
        len(
            {
                baseline,
                _segments_digest(reading(extraction_method="direct")),
                _segments_digest(reading(language="deu")),
                _segments_digest(reading(confidence=0.5)),
            }
        )
        == 4
    )


# --------------------------------------------------------------------------- #
# AT-35: exact partial coverage
# --------------------------------------------------------------------------- #


def test_one_unit_failure_keeps_exact_counts_and_a_partial_outcome(
    sample_pdf: bytes,
) -> None:
    content = two_page_pdf(sample_pdf)
    engine = FakeOcr(
        words=[("Page body", 90.0, (72.0, 300.0, 120.0, 10.0))], raise_after=1
    )

    result = extract_pdf(content, capture_id=CAPTURE, ocr_engine=engine)

    assert result.expected == 2 and result.visited == 2
    assert result.useful_units == 1 and result.failed_units == 1
    assert outcomes(result) == {
        OUTCOME_BOTH_DEDUPLICATED: 1,
        OUTCOME_PROCESSING_FAILURE: 1,
    }
    assert result.limitations["units"][1] == {
        "unit": "pdf_page",
        "number": 2,
        "outcome": OUTCOME_PROCESSING_FAILURE,
    }
    outcome = _raster_outcome(result)
    assert outcome.state == "partial"
    assert outcome.limitations["documentOutcome"] == "partial_coverage"
    assert outcome.coverage["units"][0]["failed"] == 1


def test_text_only_profile_shows_the_exact_pages_it_did_not_examine(
    sample_pdf: bytes,
) -> None:
    blank = pdfium.PdfDocument.new()
    blank.new_page(200, 200)
    buffer = io.BytesIO()
    blank.save(buffer)
    blank.close()

    result = extract_pdf(
        buffer.getvalue(), capture_id=CAPTURE, settings=ocr.text_only_settings()
    )

    assert outcomes(result) == {OUTCOME_EXCLUDED_BY_PROFILE: 1}
    assert result.excluded_units == 1 and result.useful_units == 0


def test_at35_text_only_names_all_five_image_only_pages(
    at35_mixed_pdf: bytes,
) -> None:
    result = extract_pdf(
        at35_mixed_pdf, capture_id=CAPTURE, settings=ocr.text_only_settings()
    )

    assert result.expected == 10 and result.visited == 10
    assert outcomes(result) == {
        OUTCOME_EMBEDDED_TEXT: 5,
        OUTCOME_EXCLUDED_BY_PROFILE: 5,
    }
    assert result.useful_units == 5 and result.excluded_units == 5
    excluded = [
        unit["number"]
        for unit in result.limitations["units"]
        if unit["outcome"] == OUTCOME_EXCLUDED_BY_PROFILE
    ]
    assert excluded == [2, 4, 6, 8, 10]


def test_at35_two_page_ocr_failure_keeps_exact_ten_page_coverage(
    at35_mixed_pdf: bytes,
) -> None:
    engine = FakeOcr(
        words=[("Qualified reading", 90.0, (72.0, 140.0, 250.0, 34.0))],
        raise_after=8,
    )

    result = extract_pdf(at35_mixed_pdf, capture_id=CAPTURE, ocr_engine=engine)

    assert result.expected == 10 and result.visited == 10
    assert result.failed_units == 2
    assert outcomes(result)[OUTCOME_PROCESSING_FAILURE] == 2
    failed = [
        unit["number"]
        for unit in result.limitations["units"]
        if unit["outcome"] == OUTCOME_PROCESSING_FAILURE
    ]
    assert failed == [9, 10]


# --------------------------------------------------------------------------- #
# DOC-09: image decoding, orientation and frames
# --------------------------------------------------------------------------- #


def test_orientation_is_applied_once_and_the_transform_is_recorded() -> None:
    image = text_image("Oriented")
    exif = image.getexif()
    exif[0x0112] = 6
    engine = FakeOcr(
        words=[("Oriented", 90.0, (10.0, 10.0, 100.0, 20.0))], page_width_points=220.0
    )

    result = extract_image(
        encoded(image, "JPEG", exif=exif.tobytes()),
        capture_id=CAPTURE,
        media_type="image/jpeg",
        ocr_engine=engine,
    )

    locator = result.segments[0].source_locator
    assert locator["exifOrientation"] == 6
    assert (locator["sourceWidth"], locator["sourceHeight"]) == (900, 220)
    # Orientation 6 is one quarter turn, applied exactly once.
    assert (locator["orientedWidth"], locator["orientedHeight"]) == (220, 900)
    assert locator["orientationAppliedByDecoder"] is False
    assert locator["space"] == GEOMETRY_SPACE


def test_heif_rotation_is_not_applied_twice() -> None:
    image = text_image("Heif", (80, 40))
    exif = image.getexif()
    exif[0x0112] = 6
    engine = FakeOcr(words=[], page_width_points=40.0)

    result = extract_image(
        encoded(image, "HEIF", exif=exif.tobytes()),
        capture_id=CAPTURE,
        media_type="image/heic",
        ocr_engine=engine,
    )

    unit = result.limitations
    assert unit["format"] == "HEIF"
    # The decoder already applied the container rotation, so the frame arrives
    # rotated with EXIF orientation reset and the transpose is a no-op.
    assert unit["primaryFrames"] == 1


def test_multiple_tiff_frames_are_units_and_reduced_subfiles_are_excluded() -> None:
    content = tiff_frames(
        [
            (text_image("Alpha page"), 0),
            (text_image("Beta page"), 0),
            (text_image("Thumb", (200, 60)), 1),
        ]
    )
    engine = FakeOcr(
        words=[("Frame text", 90.0, (10.0, 10.0, 100.0, 20.0))],
        page_width_points=900.0,
    )

    result = extract_image(
        content, capture_id=CAPTURE, media_type="image/tiff", ocr_engine=engine
    )

    assert result.expected == 3 and result.visited == 3
    assert outcomes(result) == {OUTCOME_OCR_TEXT: 2, OUTCOME_EXCLUDED_BY_PROFILE: 1}
    assert result.limitations["defaultFrame"] == 1
    assert [segment.source_locator["frame"] for segment in result.segments] == [1, 2]


def test_heif_primary_frames_are_distinct_units_with_a_recorded_default() -> None:
    first, second = text_image("One", (120, 60)), text_image("Two", (120, 60))
    buffer = io.BytesIO()
    first.save(buffer, format="HEIF", save_all=True, append_images=[second], quality=80)
    engine = FakeOcr(
        words=[("Frame text", 90.0, (5.0, 5.0, 50.0, 10.0))], page_width_points=120.0
    )

    result = extract_image(
        buffer.getvalue(),
        capture_id=CAPTURE,
        media_type="image/heif",
        ocr_engine=engine,
    )

    assert result.expected == 2 and result.visited == 2
    assert outcomes(result) == {OUTCOME_OCR_TEXT: 2}
    assert result.limitations["defaultFrame"] == 1
    assert result.limitations["auxiliaryFramesExcluded"] == 0


def test_animation_is_refused_while_static_webp_is_accepted() -> None:
    first, second = text_image("One"), text_image("Two")
    animated = io.BytesIO()
    first.save(
        animated, format="WEBP", save_all=True, append_images=[second], duration=100
    )
    engine = FakeOcr(
        words=[("Static", 90.0, (5.0, 5.0, 40.0, 10.0))], page_width_points=900.0
    )

    with pytest.raises(RasterExtractionInvalid, match="animated"):
        extract_image(animated.getvalue(), capture_id=CAPTURE, media_type="image/webp")

    result = extract_image(
        encoded(first, "WEBP"),
        capture_id=CAPTURE,
        media_type="image/webp",
        ocr_engine=engine,
    )
    assert outcomes(result) == {OUTCOME_OCR_TEXT: 1}


def test_alpha_is_composited_against_white_before_recognition() -> None:
    transparent = Image.new("RGBA", (300, 120), (255, 255, 255, 0))
    ImageDraw.Draw(transparent).text(
        (10, 20), "Alpha", fill=(0, 0, 0, 255), font=ImageFont.load_default(size=60)
    )
    seen: list[str] = []

    def engine(
        image: Image.Image,
        *,
        resources: ocr.OcrResources,
        psm: int,
        timeout_seconds: float,
    ) -> dict[str, list[Any]]:
        seen.append(image.mode)
        return FakeOcr(words=[], page_width_points=300.0)(
            image, resources=resources, psm=psm, timeout_seconds=timeout_seconds
        )

    result = extract_image(
        encoded(transparent, "PNG"),
        capture_id=CAPTURE,
        media_type="image/png",
        ocr_engine=engine,
    )

    assert seen == ["RGB", "RGB"]  # the primary pass and its one alternate
    assert result.limitations["alphaBackground"] == "white"


def test_declared_media_type_must_agree_with_the_detected_container() -> None:
    with pytest.raises(RasterExtractionInvalid, match="magic"):
        extract_image(
            encoded(text_image("x", (40, 20)), "PNG"),
            capture_id=CAPTURE,
            media_type="image/jpeg",
        )
    with pytest.raises(RasterExtractionInvalid, match="media type"):
        extract_image(b"GIF89a", capture_id=CAPTURE, media_type="image/gif")


# --------------------------------------------------------------------------- #
# DOC-15: resource bounds
# --------------------------------------------------------------------------- #


def test_an_oversized_frame_is_refused_from_its_header() -> None:
    with pytest.raises(RasterExtractionInvalid, match="pixel bound"):
        extract_image(
            png_header_only(9000, 9000), capture_id=CAPTURE, media_type="image/png"
        )


def test_too_many_primary_frames_is_a_truthful_refusal() -> None:
    content = tiff_frames([(Image.new("RGB", (4, 4), "white"), 0)] * 101)

    with pytest.raises(RasterExtractionInvalid, match="too many primary frames"):
        extract_image(content, capture_id=CAPTURE, media_type="image/tiff")


def test_an_oversized_source_is_refused_before_decoding() -> None:
    with pytest.raises(RasterExtractionInvalid, match="exceeds the accepted bound"):
        extract_pdf(b"%PDF-1.7\n" + b"\0" * (50 * 1024 * 1024), capture_id=CAPTURE)
    with pytest.raises(RasterExtractionInvalid, match="exceeds the accepted bound"):
        extract_image(
            b"\x89PNG\r\n\x1a\n" + b"\0" * (50 * 1024 * 1024), capture_id=CAPTURE
        )


def test_a_downscaled_page_records_its_effective_dpi_and_limitation() -> None:
    oversized = pdfium.PdfDocument.new()
    oversized.new_page(14_000, 14_000)
    buffer = io.BytesIO()
    oversized.save(buffer)
    oversized.close()
    engine = FakeOcr(words=[], page_width_points=14_000.0)

    result = extract_pdf(buffer.getvalue(), capture_id=CAPTURE, ocr_engine=engine)

    limited = result.limitations["resolutionLimitedPages"]
    assert limited[0]["page"] == 1
    assert limited[0]["effectiveDpi"] < OCR_DPI
    assert limited[0]["limitation"] == "working_raster_bounds"


def test_a_corrupt_or_encrypted_pdf_is_a_bounded_refusal() -> None:
    with pytest.raises(RasterExtractionInvalid, match="malformed or encrypted"):
        extract_pdf(b"%PDF-1.7\n", capture_id=CAPTURE)


# --------------------------------------------------------------------------- #
# DOC-14: a requested recognition language whose model is not installed
# --------------------------------------------------------------------------- #


def refusing_engine(
    image: Image.Image,
    *,
    resources: ocr.OcrResources,
    psm: int,
    timeout_seconds: float,
) -> dict[str, list[Any]]:
    raise AssertionError("recognition must not run without verified models")


def missing_language_settings(
    catalogue: ocr.TrustedOcrCatalogue,
) -> ocr.ExtractionSettings:
    settings = ocr.local_ocr_settings(catalogue=catalogue, languages=["jpn", "deu"])
    # No silent mode substitution: the selected profile keeps its identity.
    assert settings.processing_mode == ocr.MODE_TEXT_AND_LOCAL_OCR
    assert not settings.runs_ocr
    return settings


def test_an_unavailable_selection_encodes_an_honest_immutable_profile(
    local_catalogue: ocr.TrustedOcrCatalogue,
) -> None:
    unavailable = local_catalogue.resolve(["jpn"]).to_profile()

    assert unavailable == {
        "engine": "tesseract",
        "engineRevision": ocr.TESSERACT_REVISION,
        "modelHashes": [],
        "languages": ["jpn"],
        "resourceState": ocr.RESOURCE_STATE_UNAVAILABLE,
    }
    # Verified resources keep the exact shape every earlier profile was written
    # with, so no stored profile digest moves because of this encoding.
    assert "resourceState" not in local_catalogue.resolve(["deu"]).to_profile()


def test_a_pdf_keeps_its_requested_profile_and_native_text_without_its_model(
    sample_pdf: bytes, local_catalogue: ocr.TrustedOcrCatalogue
) -> None:
    result = extract_pdf(
        sample_pdf,
        capture_id=CAPTURE,
        settings=missing_language_settings(local_catalogue),
        ocr_engine=refusing_engine,
    )
    outcome = _raster_outcome(result)

    assert result.limitations["processingMode"] == ocr.MODE_TEXT_AND_LOCAL_OCR
    assert result.limitations["ocrResourceState"] == ocr.RESOURCE_STATE_UNAVAILABLE
    assert result.limitations["ocrLanguages"] == ["jpn", "deu"]
    # No model hash is claimed, because no model was read.
    assert result.limitations["ocrModelHashes"] == []
    assert result.limitations["code"] == ocr.REASON_NEEDS_LANGUAGE
    assert result.limitations["requestedLanguages"] == ["jpn", "deu"]
    assert result.limitations["missingLanguages"] == ["jpn"]
    assert result.limitations["recovery"] == "install_language"
    # The embedded text already within reach is published, not discarded.
    assert "Test PDF Document" in searchable(result)[0]
    assert outcome.state == "partial"
    assert outcome.limitations["documentOutcome"] == ocr.REASON_NEEDS_LANGUAGE
    assert outcome.coverage["units"][0]["usefulText"] == 1


def test_an_image_without_its_ocr_model_is_needs_language_not_a_success(
    local_catalogue: ocr.TrustedOcrCatalogue,
) -> None:
    result = extract_image(
        encoded(text_image("Ignored"), "PNG"),
        capture_id=CAPTURE,
        media_type="image/png",
        settings=missing_language_settings(local_catalogue),
        ocr_engine=refusing_engine,
    )
    outcome = _raster_outcome(result)

    # An image holds no embedded text, so nothing at all was read.
    assert outcomes(result) == {OUTCOME_EXCLUDED_BY_PROFILE: 1}
    assert result.useful_units == 0 and result.known_empty_units == 0
    assert outcome.state == "failed"
    assert outcome.segments == () and outcome.output_digest is None
    assert outcome.limitations["documentOutcome"] == ocr.REASON_NEEDS_LANGUAGE
    assert outcome.limitations["missingLanguages"] == ["jpn"]


# --------------------------------------------------------------------------- #
# DOC-15: a limit breach on the final unit is never rounded up to success
# --------------------------------------------------------------------------- #


def test_a_final_page_whose_raster_the_pixel_budget_refused_is_not_a_success(
    sample_pdf: bytes,
) -> None:
    engine = FakeOcr(words=[])

    result = extract_pdf(
        sample_pdf,
        capture_id=CAPTURE,
        ocr_engine=engine,
        budget=ExtractionBudget(ocr_pixels=1_000),
    )
    outcome = _raster_outcome(result)

    assert engine.calls == []  # the raster was never paid for
    assert result.limitations["limitBreach"] == "ocr_pixel_budget_exhausted"
    # Not a complete embedded-text page: its OCR half never ran.
    assert outcomes(result) == {OUTCOME_TRUNCATED_BY_LIMIT: 1}
    assert "Test PDF Document" in searchable(result)[0]
    assert result.useful_units == 1 and result.known_empty_units == 0
    assert outcome.state == "partial"
    assert outcome.limitations["documentOutcome"] == "coverage_truncated_by_limit"


def test_a_final_frame_whose_ocr_time_ran_out_is_unknown_not_a_proven_blank() -> None:
    engine = FakeOcr(words=[], page_width_points=900.0)

    result = extract_image(
        encoded(text_image("Unread"), "PNG"),
        capture_id=CAPTURE,
        media_type="image/png",
        ocr_engine=engine,
        budget=ExtractionBudget(ocr_seconds=0.0),
    )
    outcome = _raster_outcome(result)

    assert engine.calls == []
    assert result.limitations["limitBreach"] == "ocr_time_budget_exhausted"
    assert outcomes(result) == {OUTCOME_TRUNCATED_BY_LIMIT: 1}
    assert result.limitations["units"][0]["publishedText"] is False
    # Nothing was read, so the frame is neither useful nor a proven blank.
    assert result.useful_units == 0 and result.known_empty_units == 0
    assert result.visited == 0 and result.unknown_remaining == 1
    assert outcome.state == "partial"
    assert outcome.coverage["units"][0]["unknownRemaining"] == 1
    assert outcome.coverage["units"][0]["knownEmpty"] == 0


def test_the_decoded_pixel_budget_is_the_documents_and_not_one_frames() -> None:
    """Source pixels are charged per admitted frame against the one shared
    ceiling, so a frame the document can no longer pay for is cut short before
    it is worked on instead of being counted from zero. The frames already read
    keep their readings; the frame never decoded is unknown, not a blank."""
    frame = text_image("Page", (200, 60))
    content = tiff_frames([(frame, 0)] * 3)
    engine = FakeOcr(
        words=[("Frame text", 90.0, (10.0, 10.0, 100.0, 20.0))],
        page_width_points=200.0,
    )
    budget = ExtractionBudget(decoded_pixels=2 * 200 * 60)

    result = extract_image(
        content,
        capture_id=CAPTURE,
        media_type="image/tiff",
        ocr_engine=engine,
        budget=budget,
    )

    assert outcomes(result) == {OUTCOME_OCR_TEXT: 2, OUTCOME_TRUNCATED_BY_LIMIT: 1}
    assert [segment.source_locator["frame"] for segment in result.segments] == [1, 2]
    assert result.limitations["limitBreach"] == "decoded_pixel_budget_exhausted"
    # Charged exactly once for each frame admitted, and not at all for the one
    # refused: the ceiling is spent, not overspent.
    assert budget.decoded_pixels == 0
    assert result.useful_units == 2 and result.unknown_remaining == 1
    assert result.known_empty_units == 0


def test_a_final_page_whose_output_the_text_budget_cut_short_stays_partial(
    sample_pdf: bytes,
) -> None:
    # Exactly the first native line fits, so the page publishes some of its text
    # and loses the rest: useful coverage, but not a complete page.
    result = extract_pdf(
        sample_pdf,
        capture_id=CAPTURE,
        settings=ocr.text_only_settings(),
        budget=ExtractionBudget(text_bytes=len(b"Test PDF Document")),
    )
    outcome = _raster_outcome(result)

    assert result.limitations["limitBreach"] == "output_text_budget_exhausted"
    assert outcomes(result) == {OUTCOME_TRUNCATED_BY_LIMIT: 1}
    assert result.limitations["units"][0]["publishedText"] is True
    assert searchable(result) == ["Test PDF Document"]
    assert result.useful_units == 1 and result.visited == 1
    assert outcome.state == "partial"
    assert outcome.limitations["documentOutcome"] == "coverage_truncated_by_limit"


# --------------------------------------------------------------------------- #
# Local integration smoke, when the host has the packaged engine
# --------------------------------------------------------------------------- #


#: The host's own model directory, as the one real-engine smoke test trusts it.
#: A test may trust its host explicitly. Production may not, which is exactly
#: why the packaged catalogue is built from a signed manifest and verified
#: against the directory it names before the engine is invoked.
_HOST_TESSDATA = host_tessdata("eng", "osd")


@pytest.mark.skipif(
    _HOST_TESSDATA is None, reason="no local OCR engine and model directory"
)
def test_local_tesseract_deduplicates_the_sample_pdf_once(sample_pdf: bytes) -> None:
    assert _HOST_TESSDATA is not None
    host = catalogue_over(_HOST_TESSDATA, "eng", "osd")

    result = extract_pdf(
        sample_pdf,
        capture_id=CAPTURE,
        settings=ocr.local_ocr_settings(catalogue=host),
    )

    assert outcomes(result) == {OUTCOME_BOTH_DEDUPLICATED: 1}
    body = " ".join(searchable(result))
    assert body.count("Test PDF Document") == 1
    assert result.limitations["ocrEngine"].startswith("tesseract-")
    assert result.limitations["ocrLanguages"] == ["eng"]
    # The digests reported are the ones taken over the files just verified.
    assert sorted(result.limitations["ocrModelHashes"]) == sorted(
        f"sha256:{digest}" for digest in host.installed.values()
    )


@pytest.mark.skipif(
    _HOST_TESSDATA is None, reason="no local OCR engine and model directory"
)
def test_at35_real_tesseract_reads_all_ten_mixed_pages_exactly_once(
    at35_mixed_pdf: bytes,
) -> None:
    assert _HOST_TESSDATA is not None
    host = catalogue_over(_HOST_TESSDATA, "eng", "osd")

    result = extract_pdf(
        at35_mixed_pdf,
        capture_id=CAPTURE,
        settings=ocr.local_ocr_settings(catalogue=host),
    )

    assert result.expected == 10 and result.visited == 10
    assert result.failed_units == 0 and result.useful_units == 10
    text = "\n".join(searchable(result)).casefold()
    for number in range(1, 11):
        label = (
            f"Native page {number} qualified"
            if number % 2
            else f"Scanned page {number}"
        )
        assert text.count(label.casefold()) == 1
    assert result.limitations["ocrEngine"].startswith("tesseract-")
    assert result.limitations["ocrLanguages"] == ["eng"]


def _image_outcome(engine: ocr.OcrEngine) -> Any:
    """One image extraction mapped onto its document-level outcome."""
    return _raster_outcome(
        extract_image(
            encoded(text_image("ignored"), "PNG"),
            capture_id=CAPTURE,
            media_type="image/png",
            ocr_engine=engine,
        )
    )


def test_an_engine_that_is_not_an_absolute_packaged_path_is_never_invoked(
    monkeypatch: pytest.MonkeyPatch, tessdata: Path
) -> None:
    """DOC-13/DOC-16: there is no bare-name or search-path route to the engine.

    Resources whose model bytes verify but which name no packaged executable --
    or name one by bare word -- are refused before any process is started. An
    installation with no verified payload therefore runs no recognition at all,
    rather than running whatever binary a host happens to have.
    """
    invoked: list[object] = []
    monkeypatch.setattr(
        pytesseract, "image_to_data", lambda *a, **k: invoked.append(a) or {}
    )
    verified = catalogue_over(tessdata, "eng").resolve(("eng",))

    for engine in (None, "", "tesseract", "../../usr/bin/tesseract"):
        resources = replace(verified, tesseract_path=engine)
        with pytest.raises(ocr.OcrResourceRefused):
            ocr.run_tesseract(
                Image.new("RGB", (10, 10), "white"), resources=resources, psm=3
            )
    assert invoked == []
