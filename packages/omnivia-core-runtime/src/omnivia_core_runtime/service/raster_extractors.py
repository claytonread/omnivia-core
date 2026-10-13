"""Bounded PDF, raster-image and local OCR adapters over admitted bytes."""

from __future__ import annotations

import io
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Final

import pillow_heif
import pypdfium2 as pdfium  # type: ignore[import-untyped]
import pytesseract  # type: ignore[import-untyped]
from PIL import Image, ImageOps, UnidentifiedImageError
from pytesseract import Output

from omnivia_core_runtime.service.document_intake import ExtractedSegment

MAX_SOURCE_BYTES: Final = 50 * 1024 * 1024
MAX_PDF_PAGES: Final = 500
MAX_FRAME_PIXELS: Final = 64_000_000
MAX_AGGREGATE_PIXELS: Final = 512_000_000
MAX_FRAMES: Final = 100
MAX_WORKING_PIXELS: Final = 32_000_000
MAX_EDGE: Final = 16_384
OCR_DPI: Final = 300
OCR_CONFIDENCE_THRESHOLD: Final = 35.0
SUPPORTED_IMAGE_FORMATS: Final = frozenset({"PNG", "JPEG", "TIFF", "HEIF", "WEBP"})

pillow_heif.register_heif_opener()


class RasterExtractionInvalid(ValueError):
    """The PDF/image is malformed, unsupported, or beyond a processing bound."""


@dataclass(frozen=True, slots=True)
class RasterExtraction:
    segments: tuple[ExtractedSegment, ...]
    unit_type: str
    expected: int
    useful_units: int
    failed_units: int
    limitations: dict[str, object]


def extract_pdf(content: bytes, *, capture_id: str) -> RasterExtraction:
    if len(content) > MAX_SOURCE_BYTES:
        raise RasterExtractionInvalid("PDF source exceeds the accepted bound")
    try:
        document = pdfium.PdfDocument(content)
    except Exception as error:  # PDFium exposes native exceptions by build.
        raise RasterExtractionInvalid("PDF is malformed or encrypted") from error
    segments: list[ExtractedSegment] = []
    failed = 0
    useful = 0
    try:
        page_count = len(document)
        if page_count > MAX_PDF_PAGES:
            raise RasterExtractionInvalid("PDF has too many pages")
        for page_index in range(page_count):
            try:
                page = document[page_index]
                width, height = page.get_size()
                text_page = page.get_textpage()
                native = text_page.get_text_range()
                page_useful = bool(native.strip())
                if native:
                    segments.append(
                        ExtractedSegment(
                            "body",
                            _locator(
                                capture_id,
                                unit="pdf_page",
                                page=page_index + 1,
                                method="native_text",
                                width=width,
                                height=height,
                            ),
                            text=native,
                        )
                    )
                image, effective_dpi = _render_pdf_page(
                    page, width=width, height=height
                )
                ocr = _ocr_lines(
                    image,
                    capture_id=capture_id,
                    unit="pdf_page",
                    unit_number=page_index + 1,
                    effective_dpi=effective_dpi,
                )
                native_key = _normalise_text(native)
                retained_ocr = [
                    item
                    for item in ocr
                    if _normalise_text(item.text or "") not in native_key
                ]
                segments.extend(retained_ocr)
                page_useful = page_useful or bool(retained_ocr)
                useful += int(page_useful)
                image.close()
                text_page.close()
                page.close()
            except (MemoryError, RuntimeError, OSError, pytesseract.TesseractError):
                failed += 1
    finally:
        document.close()
    return RasterExtraction(
        tuple(segments),
        "pdf_page",
        page_count,
        useful,
        failed,
        {
            "ocrEngine": "tesseract-5.5.3",
            "ocrLanguages": ["eng"],
            "deduplication": "normalised-line-contained-in-native-page-1",
        },
    )


def extract_image(content: bytes, *, capture_id: str) -> RasterExtraction:
    if len(content) > MAX_SOURCE_BYTES:
        raise RasterExtractionInvalid("image source exceeds the accepted bound")
    try:
        source = Image.open(io.BytesIO(content))
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise RasterExtractionInvalid("image cannot be safely decoded") from error
    segments: list[ExtractedSegment] = []
    failed = 0
    useful = 0
    aggregate_pixels = 0
    try:
        image_format = (source.format or "").upper()
        frame_count = int(getattr(source, "n_frames", 1))
        if image_format not in SUPPORTED_IMAGE_FORMATS:
            raise RasterExtractionInvalid("image format is outside the accepted set")
        if frame_count > MAX_FRAMES:
            raise RasterExtractionInvalid("image has too many primary frames")
        if frame_count > 1 and image_format in {"PNG", "WEBP"}:
            raise RasterExtractionInvalid("animated images are not supported")
        for frame_index in range(frame_count):
            try:
                source.seek(frame_index)
                frame = ImageOps.exif_transpose(source.copy())
                pixels = frame.width * frame.height
                aggregate_pixels += pixels
                if (
                    pixels > MAX_FRAME_PIXELS
                    or aggregate_pixels > MAX_AGGREGATE_PIXELS
                    or max(frame.size) > MAX_EDGE
                ):
                    raise RasterExtractionInvalid(
                        "decoded image exceeds the accepted pixel bound"
                    )
                prepared = _ocr_working_image(frame)
                ocr = _ocr_lines(
                    prepared,
                    capture_id=capture_id,
                    unit="image_frame",
                    unit_number=frame_index + 1,
                    effective_dpi=None,
                )
                segments.extend(ocr)
                useful += int(bool(ocr))
                prepared.close()
                frame.close()
            except (MemoryError, RuntimeError, OSError, pytesseract.TesseractError):
                failed += 1
    finally:
        source.close()
    return RasterExtraction(
        tuple(segments),
        "image_frame",
        frame_count,
        useful,
        failed,
        {
            "format": image_format,
            "ocrEngine": "tesseract-5.5.3",
            "ocrLanguages": ["eng"],
        },
    )


def _render_pdf_page(
    page: Any, *, width: float, height: float
) -> tuple[Image.Image, int]:
    target_dpi = OCR_DPI
    scale = target_dpi / 72.0
    if width * height * scale * scale > MAX_WORKING_PIXELS:
        scale = (MAX_WORKING_PIXELS / max(width * height, 1.0)) ** 0.5
        target_dpi = max(1, int(scale * 72))
    if max(width * scale, height * scale) > MAX_EDGE:
        scale = MAX_EDGE / max(width, height)
        target_dpi = max(1, int(scale * 72))
    try:
        image = page.render(scale=scale).to_pil()
    except Exception as error:
        raise RasterExtractionInvalid("PDF page rasterisation failed") from error
    return _ocr_working_image(image), target_dpi


def _ocr_working_image(image: Image.Image) -> Image.Image:
    if image.mode in {"RGBA", "LA"}:
        background = Image.new("RGB", image.size, "white")
        alpha = image.getchannel("A")
        background.paste(image.convert("RGB"), mask=alpha)
        return background
    if image.mode not in {"RGB", "L"}:
        return image.convert("RGB")
    return image.copy()


def _ocr_lines(
    image: Image.Image,
    *,
    capture_id: str,
    unit: str,
    unit_number: int,
    effective_dpi: int | None,
) -> list[ExtractedSegment]:
    data = pytesseract.image_to_data(
        image,
        lang="eng",
        config="--oem 1 --psm 3",
        output_type=Output.DICT,
    )
    lines: dict[tuple[int, int, int], list[tuple[str, float, int, int, int, int]]] = (
        defaultdict(list)
    )
    for index, raw_text in enumerate(data["text"]):
        text = str(raw_text).strip()
        try:
            confidence = float(data["conf"][index])
        except (TypeError, ValueError):
            continue
        if not text or confidence < OCR_CONFIDENCE_THRESHOLD:
            continue
        key = (
            int(data["block_num"][index]),
            int(data["par_num"][index]),
            int(data["line_num"][index]),
        )
        lines[key].append(
            (
                text,
                confidence,
                int(data["left"][index]),
                int(data["top"][index]),
                int(data["width"][index]),
                int(data["height"][index]),
            )
        )
    segments: list[ExtractedSegment] = []
    for line_ordinal, (_key, words) in enumerate(sorted(lines.items()), start=1):
        left = min(word[2] for word in words)
        top = min(word[3] for word in words)
        right = max(word[2] + word[4] for word in words)
        bottom = max(word[3] + word[5] for word in words)
        confidence = statistics.median(word[1] for word in words) / 100.0
        segments.append(
            ExtractedSegment(
                "body",
                _locator(
                    capture_id,
                    unit=unit,
                    number=unit_number,
                    line=line_ordinal,
                    box=[left, top, right - left, bottom - top],
                    effectiveDpi=effective_dpi,
                ),
                text=" ".join(word[0] for word in words),
                extraction_method="ocr",
                language="eng",
                confidence=max(0.0, min(confidence, 1.0)),
            )
        )
    return segments


def _normalise_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _locator(capture_id: str, **values: object) -> dict[str, object]:
    return {
        "captureId": capture_id,
        **{key: value for key, value in values.items() if value is not None},
    }
