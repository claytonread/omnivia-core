"""Bounded PDF, raster-image and local OCR adapters over admitted bytes.

These adapters receive the admitted immutable source bytes and the trusted
:class:`~omnivia_core_runtime.service.ocr_resources.ExtractionSettings` for the
job. They open no path, resolve no URL or bookmark, reach no network, and take
no engine, model, language or directory decision from the document: PDF
attachments and actions are never followed and no form environment is
initialised. Every page, frame and embedded image is decoded, recognised and
closed one at a time, and every bound is checked before the allocation it
protects.

Geometry from both methods is normalised into one page/frame space
(:data:`GEOMETRY_SPACE`) so a reading can be traced back to source pixels:
cropbox-relative points with a top-left origin, after page rotation. Reliable
embedded text stays the primary decoded source; OCR adds a second observation
where the two agree over the same region, a linked alternative where they
disagree materially, and its own segment where only OCR saw the region.
"""

from __future__ import annotations

import io
import re
import statistics
import time
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from typing import Any, Final

import PIL
import pillow_heif
import pypdfium2 as pdfium  # type: ignore[import-untyped]
import pytesseract  # type: ignore[import-untyped]
from PIL import Image, ImageOps, UnidentifiedImageError

from omnivia_core_runtime.service.document_intake import ExtractedSegment
from omnivia_core_runtime.service.ocr_resources import (
    ALTERNATE_PSM,
    CONFIDENCE_SCALE,
    LOW_CONFIDENCE_SHARE,
    LOW_CONFIDENCE_THRESHOLD,
    OCR_PIXELS_PER_DOCUMENT,
    OCR_SECONDS_PER_DOCUMENT,
    OCR_SECONDS_PER_UNIT,
    PRIMARY_PSM,
    RESOURCE_STATE_UNAVAILABLE,
    WORD_CONFIDENCE_THRESHOLD,
    ExtractionSettings,
    OcrEngine,
    local_ocr_settings,
    run_tesseract,
)

MAX_SOURCE_BYTES: Final = 50 * 1024 * 1024
MAX_PDF_PAGES: Final = 500
MAX_FRAME_PIXELS: Final = 64_000_000
MAX_AGGREGATE_PIXELS: Final = 512_000_000
MAX_FRAMES: Final = 100
MAX_WORKING_PIXELS: Final = 32_000_000
MAX_EDGE: Final = 16_384
MAX_TEXT_BYTES: Final = 32 * 1024 * 1024
#: Non-OCR extraction execution budget for one source, in seconds.
MAX_DECODE_SECONDS: Final = 120
OCR_DPI: Final = 300

SUPPORTED_IMAGE_FORMATS: Final = frozenset({"PNG", "JPEG", "TIFF", "HEIF", "WEBP"})
#: Accepted image magic, by the media type the capture was admitted under. A
#: declared type that disagrees with the detected container is refused; the
#: filename hint never wins over the actual container.
IMAGE_FORMAT_BY_MEDIA_TYPE: Final[Mapping[str, str]] = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/tiff": "TIFF",
    "image/heic": "HEIF",
    "image/heif": "HEIF",
    "image/webp": "WEBP",
}
#: Formats whose multi-frame form is animation, refused rather than silently
#: reduced to a first frame. Multiple TIFF/HEIF images are primary frames.
ANIMATION_FORMATS: Final = frozenset({"PNG", "WEBP"})

UNIT_PDF_PAGE: Final = "pdf_page"
UNIT_IMAGE_FRAME: Final = "image_frame"

#: The common coordinate space both methods are normalised into.
GEOMETRY_SPACE: Final = "cropbox_points_top_left_rotated_v1"
READING_ORDER_VERSION: Final = "display-top-left-1"
DEDUPLICATION_VERSION: Final = "geometry-overlap-70-text-similarity-90-1"
#: Proposed, profile-versioned qualification settings, not measured guarantees.
OVERLAP_RATIO: Final = 0.70
TEXT_SIMILARITY_RATIO: Final = 0.90

OUTCOME_EMBEDDED_TEXT: Final = "embedded_text"
OUTCOME_OCR_TEXT: Final = "ocr_text"
OUTCOME_BOTH_DEDUPLICATED: Final = "both_deduplicated"
OUTCOME_CONFLICTING_READINGS: Final = "conflicting_readings"
OUTCOME_NO_RELIABLE_OCR_TEXT: Final = "no_reliable_ocr_text"
OUTCOME_LOW_CONFIDENCE: Final = "low_confidence"
OUTCOME_PROCESSING_FAILURE: Final = "processing_failure"
OUTCOME_UNSUPPORTED_CONTENT: Final = "unsupported_content"
OUTCOME_EXCLUDED_BY_PROFILE: Final = "excluded_by_profile"
#: A unit whose recognition, raster or output was cut short by a document budget.
#: It is neither a proven blank nor a complete embedded-text page, so it is never
#: reported as one: whatever it did publish stays, and what it did not reach is
#: unknown rather than examined.
OUTCOME_TRUNCATED_BY_LIMIT: Final = "truncated_by_limit"
UNIT_OUTCOMES: Final = (
    OUTCOME_EMBEDDED_TEXT,
    OUTCOME_OCR_TEXT,
    OUTCOME_BOTH_DEDUPLICATED,
    OUTCOME_CONFLICTING_READINGS,
    OUTCOME_NO_RELIABLE_OCR_TEXT,
    OUTCOME_LOW_CONFIDENCE,
    OUTCOME_PROCESSING_FAILURE,
    OUTCOME_UNSUPPORTED_CONTENT,
    OUTCOME_EXCLUDED_BY_PROFILE,
    OUTCOME_TRUNCATED_BY_LIMIT,
)
#: Outcomes whose unit contributed searchable text.
_USEFUL_OUTCOMES: Final = frozenset(
    {
        OUTCOME_EMBEDDED_TEXT,
        OUTCOME_OCR_TEXT,
        OUTCOME_BOTH_DEDUPLICATED,
        OUTCOME_CONFLICTING_READINGS,
    }
)

WORKER_BUILD: Final = "core-raster-ocr-1"
#: The exact decoder identities the immutable extraction profile pins, read from
#: the installed pinned builds rather than restated as a literal that could drift.
PDF_DECODER_BUILD: Final = (
    f"pypdfium2-{pdfium.PYPDFIUM_INFO}+pdfium-{pdfium.PDFIUM_INFO}"
)
IMAGE_DECODER_BUILD: Final = (
    f"pillow-{PIL.__version__}"
    f"+pillow_heif-{pillow_heif.__version__}"
    f"+libheif-{pillow_heif.libheif_version()}"
)

pillow_heif.register_heif_opener()


class RasterExtractionInvalid(ValueError):
    """The PDF/image is malformed, unsupported, or beyond a processing bound."""


@dataclass(frozen=True, slots=True)
class RasterExtraction:
    """One source's segments plus the exact unit counts behind them."""

    segments: tuple[ExtractedSegment, ...]
    unit_type: str
    expected: int
    visited: int
    useful_units: int
    known_empty_units: int
    excluded_units: int
    unsupported_units: int
    failed_units: int
    unknown_remaining: int
    limitations: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _UnitRecord:
    """One unit's exact outcome and locator, as retained in the limitations."""

    unit: str
    number: int
    outcome: str
    flags: tuple[str, ...] = ()
    stats: Mapping[str, Any] | None = None
    #: Whether this unit contributed searchable text. It decides whether a
    #: truncated unit counts as useful coverage or as coverage never reached.
    published: bool = False

    def to_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "unit": self.unit,
            "number": self.number,
            "outcome": self.outcome,
        }
        if self.outcome == OUTCOME_TRUNCATED_BY_LIMIT:
            record["publishedText"] = self.published
        if self.flags:
            record["flags"] = list(self.flags)
        if self.stats:
            record["ocr"] = dict(self.stats)
        return record


def _unit_record(
    unit: str,
    number: int,
    outcome: str,
    *,
    budget: ExtractionBudget,
    published: bool,
    flags: tuple[str, ...] = (),
    stats: Mapping[str, Any] | None = None,
) -> _UnitRecord:
    """One unit's record, demoted when a budget breach cut this unit short.

    A breach is only ever set by work this unit asked for, and the loops stop
    before starting another unit, so a breach standing here means *this* unit's
    recognition, raster or output was skipped or truncated. Such a unit cannot
    be called a proven blank or a complete embedded-text page.
    """
    if budget.breach is not None:
        outcome = OUTCOME_TRUNCATED_BY_LIMIT
    return _UnitRecord(unit, number, outcome, flags, stats, published)


@dataclass
class ExtractionBudget:
    """Aggregate document budgets. A breach stops work; it never truncates silently.

    The ceilings are fields rather than constants so a breach can be exercised
    with a small budget instead of a real allocation.
    """

    ocr_pixels: int = OCR_PIXELS_PER_DOCUMENT
    ocr_seconds: float = OCR_SECONDS_PER_DOCUMENT
    decode_seconds: float = MAX_DECODE_SECONDS
    text_bytes: int = MAX_TEXT_BYTES
    breach: str | None = None
    ocr_spent_seconds: float = 0.0
    _started: float = field(default_factory=time.monotonic)

    def may_ocr(self, pixels: int) -> bool:
        """Check the aggregate OCR budgets before the raster they would pay for."""
        if pixels > self.ocr_pixels:
            self.breach = "ocr_pixel_budget_exhausted"
            return False
        if self.ocr_seconds <= 0.0:
            self.breach = "ocr_time_budget_exhausted"
            return False
        return True

    def spend_ocr(self, pixels: int, seconds: float) -> None:
        self.ocr_pixels -= pixels
        self.ocr_seconds -= seconds
        self.ocr_spent_seconds += seconds

    def unit_timeout(self) -> float:
        return max(1.0, min(float(OCR_SECONDS_PER_UNIT), self.ocr_seconds))

    def may_decode(self) -> bool:
        """Whether the non-OCR execution budget for this source still holds."""
        if (
            time.monotonic() - self._started - self.ocr_spent_seconds
            > self.decode_seconds
        ):
            self.breach = "extraction_time_budget_exhausted"
            return False
        return True

    def take_text(self, text: str) -> bool:
        """Reserve a segment's decoded text against the output text ceiling."""
        size = len(text.encode("utf-8"))
        if size > self.text_bytes:
            self.breach = "output_text_budget_exhausted"
            return False
        self.text_bytes -= size
        return True


@dataclass(frozen=True, slots=True)
class _PageGeometry:
    """One PDF page's cropbox, rotation and the transform both methods map through."""

    crop: tuple[float, float, float, float]
    rotation: int
    display_width: float
    display_height: float

    @classmethod
    def of(cls, page: Any) -> _PageGeometry:
        width, height = page.get_size()
        left, bottom, right, top = page.get_cropbox()
        return cls(
            crop=(float(left), float(bottom), float(right), float(top)),
            rotation=int(page.get_rotation()) % 360,
            display_width=float(width),
            display_height=float(height),
        )

    def native_box(
        self, rect: tuple[float, float, float, float]
    ) -> tuple[float, float, float, float]:
        """Map one PDFium text rect from unrotated user space into display space.

        PDFium reports text rects in unrotated, cropbox-relative user space with
        a bottom-left origin, while it renders the page with ``/Rotate`` applied.
        The quarter-turn cases below are that one transform, written out.
        """
        left, bottom, right, top = rect
        crop_left, crop_bottom, crop_right, crop_top = self.crop
        unrotated_width = crop_right - crop_left
        unrotated_height = crop_top - crop_bottom
        x = left - crop_left
        y = crop_top - top
        width = max(0.0, right - left)
        height = max(0.0, top - bottom)
        if self.rotation == 90:
            return (unrotated_height - y - height, x, height, width)
        if self.rotation == 180:
            return (
                unrotated_width - x - width,
                unrotated_height - y - height,
                width,
                height,
            )
        if self.rotation == 270:
            return (y, unrotated_width - x - width, height, width)
        return (x, y, width, height)

    def to_dict(self) -> dict[str, Any]:
        return {
            "space": GEOMETRY_SPACE,
            "cropBox": [round(value, 3) for value in self.crop],
            "rotation": self.rotation,
            "pageWidth": round(self.display_width, 3),
            "pageHeight": round(self.display_height, 3),
        }


@dataclass(frozen=True, slots=True)
class _NativeLine:
    text: str
    box: tuple[float, float, float, float]
    rect_ordinal: int
    mappable: bool = True


@dataclass(frozen=True, slots=True)
class _OcrWord:
    text: str
    confidence: float
    box: tuple[float, float, float, float]
    accepted: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "confidence": self.confidence,
            "box": [round(value, 3) for value in self.box],
            "accepted": self.accepted,
        }


@dataclass(frozen=True, slots=True)
class _OcrLine:
    words: tuple[_OcrWord, ...]
    box: tuple[float, float, float, float]
    line_ordinal: int

    @property
    def accepted_words(self) -> tuple[_OcrWord, ...]:
        return tuple(word for word in self.words if word.accepted)

    @property
    def text(self) -> str:
        return " ".join(word.text for word in (self.accepted_words or self.words))

    @property
    def confidence(self) -> float | None:
        words = self.accepted_words or self.words
        if not words:
            return None
        median = statistics.median(word.confidence for word in words)
        # Raw engine values stay in the locator; the segment field is the
        # contract's bounded zero-through-one representation of the same median.
        return max(0.0, min(median / 100.0, 1.0))


@dataclass(frozen=True, slots=True)
class _UnitOcr:
    """One unit's recognition result, including what was observed but not accepted."""

    lines: tuple[_OcrLine, ...]
    observations: tuple[_OcrLine, ...]
    candidate_words: int
    sentinel_rows: int
    low_confidence: bool
    passes: tuple[int, ...]

    def stats(self) -> dict[str, Any]:
        return {
            "candidateWords": self.candidate_words,
            "acceptedWords": sum(len(line.accepted_words) for line in self.lines),
            "sentinelRows": self.sentinel_rows,
            "segmentationPasses": list(self.passes),
            "confidenceScale": CONFIDENCE_SCALE,
            "acceptThreshold": WORD_CONFIDENCE_THRESHOLD,
        }


_EMPTY_OCR: Final = _UnitOcr((), (), 0, 0, False, ())


def extract_pdf(
    content: bytes,
    *,
    capture_id: str,
    settings: ExtractionSettings | None = None,
    ocr_engine: OcrEngine = run_tesseract,
    budget: ExtractionBudget | None = None,
) -> RasterExtraction:
    """Extract one admitted PDF under ``settings``.

    Under the complete Text-and-local-OCR profile every admitted page gets both
    its native text with geometry and a bounded full-page raster/OCR candidate,
    even where native text already exists: a native heading can sit above a
    scanned body, and no heuristic is allowed to skip that body. Text-only is
    the explicit faster profile and says so in its coverage. A Text-and-local-OCR
    profile whose models turned out to be unavailable still runs this bounded
    native text extraction - the embedded text it already reaches is not thrown
    away - while its coverage names the missing resources rather than claiming
    the source held no recognisable text.

    ``budget`` is the document's aggregate ceiling, injectable so a breach can be
    exercised with a small budget instead of a real allocation.
    """
    settings = settings or local_ocr_settings()
    if len(content) > MAX_SOURCE_BYTES:
        raise RasterExtractionInvalid("PDF source exceeds the accepted bound")
    try:
        document = pdfium.PdfDocument(content)
    except Exception as error:  # PDFium exposes native exceptions by build.
        raise RasterExtractionInvalid("PDF is malformed or encrypted") from error
    segments: list[ExtractedSegment] = []
    records: list[_UnitRecord] = []
    budget = budget if budget is not None else ExtractionBudget()
    downscaled: list[dict[str, Any]] = []
    unmappable = 0
    try:
        page_count = len(document)
        if page_count > MAX_PDF_PAGES:
            raise RasterExtractionInvalid("PDF has too many pages")
        for page_index in range(page_count):
            if budget.breach is not None or not budget.may_decode():
                break
            try:
                record, page_unmappable = _extract_pdf_page(
                    document,
                    page_index,
                    capture_id=capture_id,
                    settings=settings,
                    ocr_engine=ocr_engine,
                    budget=budget,
                    segments=segments,
                    downscaled=downscaled,
                )
                records.append(record)
                unmappable += page_unmappable
            except (
                MemoryError,
                RuntimeError,
                OSError,
                ValueError,
                pytesseract.TesseractError,
            ):
                # One unit's failure is that unit's outcome; the remaining pages
                # of the same immutable source are still processed.
                records.append(
                    _UnitRecord(
                        UNIT_PDF_PAGE, page_index + 1, OUTCOME_PROCESSING_FAILURE
                    )
                )
    finally:
        document.close()
    limitations: dict[str, Any] = {
        "deduplication": DEDUPLICATION_VERSION,
        "readingOrder": READING_ORDER_VERSION,
        "geometrySpace": GEOMETRY_SPACE,
        "overlapRatio": OVERLAP_RATIO,
    }
    if downscaled:
        limitations["resolutionLimitedPages"] = downscaled
    if unmappable:
        limitations["unmappableEmbeddedTextLines"] = unmappable
    return _raster_extraction(
        segments,
        records,
        unit_type=UNIT_PDF_PAGE,
        expected=page_count,
        settings=settings,
        budget=budget,
        limitations=limitations,
    )


def extract_image(
    content: bytes,
    *,
    capture_id: str,
    media_type: str | None = None,
    settings: ExtractionSettings | None = None,
    ocr_engine: OcrEngine = run_tesseract,
    budget: ExtractionBudget | None = None,
) -> RasterExtraction:
    """Extract one admitted image, one primary frame at a time.

    An image carries no embedded text, so a profile that cannot recognise -
    text-only, or local OCR whose models are unavailable - reaches no text at
    all and says so instead of reporting a searchable success.
    """
    settings = settings or local_ocr_settings()
    if len(content) > MAX_SOURCE_BYTES:
        raise RasterExtractionInvalid("image source exceeds the accepted bound")
    expected_format = (
        None if media_type is None else IMAGE_FORMAT_BY_MEDIA_TYPE.get(media_type)
    )
    if media_type is not None and expected_format is None:
        raise RasterExtractionInvalid("image media type is outside the accepted set")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            source = Image.open(io.BytesIO(content))
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise RasterExtractionInvalid("image cannot be safely decoded") from error
    segments: list[ExtractedSegment] = []
    records: list[_UnitRecord] = []
    budget = budget if budget is not None else ExtractionBudget()
    aggregate_pixels = 0
    auxiliary = 0
    default_frame = 1
    try:
        image_format = (source.format or "").upper()
        if image_format not in SUPPORTED_IMAGE_FORMATS:
            raise RasterExtractionInvalid("image format is outside the accepted set")
        if expected_format is not None and image_format != expected_format:
            raise RasterExtractionInvalid(
                "image magic does not match the admitted media type"
            )
        frame_count = max(1, int(getattr(source, "n_frames", 1)))
        # Multiple TIFF/HEIF images are primary frames, and Pillow reports those
        # as "animated" too. Only the formats whose multi-frame form really is
        # animation are refused -- and refused, never reduced to a first frame.
        if image_format in ANIMATION_FORMATS and (
            frame_count > 1 or bool(getattr(source, "is_animated", False))
        ):
            raise RasterExtractionInvalid("animated images are not supported")
        if frame_count > MAX_FRAMES:
            raise RasterExtractionInvalid("image has too many primary frames")
        for frame_index in range(frame_count):
            if budget.breach is not None or not budget.may_decode():
                break
            source.seek(frame_index)
            auxiliary += _auxiliary_frame_count(source)
            if source.info.get("primary") is True:
                default_frame = frame_index + 1
            if not _is_primary_frame(source):
                # A reduced-resolution subfile is a thumbnail of another frame,
                # not an independent page.
                records.append(
                    _UnitRecord(
                        UNIT_IMAGE_FRAME, frame_index + 1, OUTCOME_EXCLUDED_BY_PROFILE
                    )
                )
                continue
            try:
                record, frame_pixels = _extract_image_frame(
                    source,
                    frame_index,
                    capture_id=capture_id,
                    image_format=image_format,
                    settings=settings,
                    ocr_engine=ocr_engine,
                    budget=budget,
                    segments=segments,
                    aggregate_pixels=aggregate_pixels,
                )
            except RasterExtractionInvalid:
                raise
            except (
                MemoryError,
                RuntimeError,
                OSError,
                ValueError,
                pytesseract.TesseractError,
            ):
                records.append(
                    _UnitRecord(
                        UNIT_IMAGE_FRAME, frame_index + 1, OUTCOME_PROCESSING_FAILURE
                    )
                )
                continue
            aggregate_pixels += frame_pixels
            records.append(record)
    finally:
        source.close()
    records.extend(
        _UnitRecord(UNIT_IMAGE_FRAME, frame_count + offset, OUTCOME_EXCLUDED_BY_PROFILE)
        for offset in range(1, auxiliary + 1)
    )
    limitations: dict[str, Any] = {
        "format": image_format,
        "geometrySpace": GEOMETRY_SPACE,
        "defaultFrame": default_frame,
        "primaryFrames": frame_count,
        "auxiliaryFramesExcluded": auxiliary,
        "alphaBackground": "white",
    }
    return _raster_extraction(
        segments,
        records,
        unit_type=UNIT_IMAGE_FRAME,
        expected=frame_count + auxiliary,
        settings=settings,
        budget=budget,
        limitations=limitations,
    )


#: What became of one embedded raster a container declared. ``ocr_text`` and
#: ``no_reliable_ocr_text`` are the two readings of bytes that were actually
#: recognised; every other value names the exact reason nothing was, so a
#: container's record never reports an image as examined when it was not.
EMBEDDED_RECOGNISED: Final = OUTCOME_OCR_TEXT
EMBEDDED_NO_TEXT: Final = OUTCOME_NO_RELIABLE_OCR_TEXT
#: The selected profile does not examine embedded images at all.
EMBEDDED_DISABLED: Final = "embedded_image_ocr_disabled"
#: The profile asked for recognition and its pinned models were not served.
EMBEDDED_RESOURCES_UNAVAILABLE: Final = "ocr_resources_unavailable"
#: The container named a reference out of the package, which is never opened.
EMBEDDED_NOT_EMBEDDED: Final = "no_embedded_image_part"
#: The part's declared type is outside the accepted set, settled before a decode.
EMBEDDED_UNSUPPORTED: Final = "image_type_not_supported"
#: The bytes were opened and the decoder refused them: bad magic, magic that
#: disagrees with the declared type, animation, or a source/frame/pixel bound.
EMBEDDED_MALFORMED: Final = "image_could_not_be_decoded"
EMBEDDED_TRUNCATED: Final = OUTCOME_TRUNCATED_BY_LIMIT
EMBEDDED_OUTCOMES: Final = (
    EMBEDDED_RECOGNISED,
    EMBEDDED_NO_TEXT,
    EMBEDDED_DISABLED,
    EMBEDDED_RESOURCES_UNAVAILABLE,
    EMBEDDED_NOT_EMBEDDED,
    EMBEDDED_UNSUPPORTED,
    EMBEDDED_MALFORMED,
    EMBEDDED_TRUNCATED,
)


@dataclass(frozen=True, slots=True)
class EmbeddedImageCandidate:
    """One raster a container embeds, carried out of its validated reader.

    ``content`` is the member's already-validated bytes and exists only for the
    duration of one recognition: it is never placed in a segment value, a
    locator or a limitation, so nothing persisted ever holds image bytes.
    ``locator`` is the container object's own stable locator, which every
    reading of this image is anchored to, and ``part`` is the image part inside
    the package the bytes came from.
    """

    content: bytes
    media_type: str
    part: str
    locator: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class EmbeddedImageResult:
    """One embedded raster's readings and the exact truth about reaching them."""

    outcome: str
    segments: tuple[ExtractedSegment, ...] = ()
    #: Whether the bytes were opened and decoded at all.
    decoded: bool = False
    #: Whether the recognition engine was actually invoked over them.
    performed: bool = False


def embedded_image_gate(settings: ExtractionSettings) -> str | None:
    """Why no embedded raster may be opened under ``settings``, or ``None``.

    Checked once per extraction and before any candidate is built, so a profile
    that examines no embedded image never reads an image member: the refusal is
    a property of the trusted settings and never of the document.
    """
    if not settings.embedded_image_ocr or settings.ocr is None:
        return EMBEDDED_DISABLED
    if not settings.ocr.available:
        return EMBEDDED_RESOURCES_UNAVAILABLE
    return None


def extract_embedded_image(
    candidate: EmbeddedImageCandidate,
    *,
    settings: ExtractionSettings,
    budget: ExtractionBudget,
    ocr_engine: OcrEngine = run_tesseract,
) -> EmbeddedImageResult:
    """Recognise one embedded raster under the container's aggregate budget.

    The declared type is checked against the accepted set before anything is
    opened, and the bytes then go through the same bounded image path an
    admitted image source does -- the same magic check, the same refusal of
    animation, the same frame/pixel/working-raster bounds, the same recognition
    and the same OCR line and frame geometry. ``budget`` is the container's one
    aggregate ceiling, shared by every candidate in the document, so a source of
    many images is bounded in aggregate rather than one image at a time.

    Every reading is anchored to the container object's own locator and to the
    image part the bytes came from, so a reading can be traced back to the
    object that displayed it. Nothing is merged into the container's prose and no
    page layout is invented: the geometry is the frame's own.

    A refusal is this image's outcome and never the document's: the caller keeps
    its structural extraction and reports the reason here.
    """
    refused = embedded_image_gate(settings)
    if refused is not None:
        return EmbeddedImageResult(refused)
    if candidate.media_type not in IMAGE_FORMAT_BY_MEDIA_TYPE:
        return EmbeddedImageResult(EMBEDDED_UNSUPPORTED)
    try:
        extraction = extract_image(
            candidate.content,
            capture_id=str(candidate.locator.get("captureId", "")),
            media_type=candidate.media_type,
            settings=settings,
            ocr_engine=ocr_engine,
            budget=budget,
        )
    except RasterExtractionInvalid:
        return EmbeddedImageResult(EMBEDDED_MALFORMED)
    units = extraction.limitations.get("units") or ()
    segments = tuple(
        replace(
            segment,
            source_locator={
                **candidate.locator,
                **segment.source_locator,
                "imagePart": candidate.part,
            },
        )
        for segment in extraction.segments
    )
    if extraction.failed_units:
        outcome = EMBEDDED_MALFORMED
    elif budget.breach is not None:
        outcome = EMBEDDED_TRUNCATED
    elif extraction.useful_units:
        # The frame's own reconciled coverage, not merely whether anything was
        # emitted: a frame that published only below-threshold observations
        # reached no reliable reading and is not reported as having read one.
        outcome = EMBEDDED_RECOGNISED
    else:
        outcome = EMBEDDED_NO_TEXT
    return EmbeddedImageResult(
        outcome,
        segments=segments,
        decoded=True,
        # A segmentation pass is recorded for each invocation and for nothing
        # else, so this is the engine that actually ran rather than the one the
        # profile asked for: a frame whose raster the budget refused carries its
        # empty statistics and no pass.
        performed=any(
            unit.get("ocr", {}).get("segmentationPasses")
            for unit in units
            if isinstance(unit, Mapping)
        ),
    )


def _extract_pdf_page(
    document: Any,
    page_index: int,
    *,
    capture_id: str,
    settings: ExtractionSettings,
    ocr_engine: OcrEngine,
    budget: ExtractionBudget,
    segments: list[ExtractedSegment],
    downscaled: list[dict[str, Any]],
) -> tuple[_UnitRecord, int]:
    """Process and close exactly one page, appending its segments."""
    number = page_index + 1
    page = document[page_index]
    text_page = None
    image = None
    try:
        geometry = _PageGeometry.of(page)
        text_page = page.get_textpage()
        native, unmappable = _native_lines(text_page, geometry)
        if not settings.runs_ocr:
            emitted = _emit_native(
                segments,
                native,
                capture_id=capture_id,
                number=number,
                geometry=geometry,
                deduplicated={},
                budget=budget,
            )
            return (
                _unit_record(
                    UNIT_PDF_PAGE,
                    number,
                    OUTCOME_EMBEDDED_TEXT if emitted else OUTCOME_EXCLUDED_BY_PROFILE,
                    budget=budget,
                    published=bool(emitted),
                ),
                unmappable,
            )
        image, effective_dpi, scale = _render_pdf_page(page, geometry, budget=budget)
        if image is None:
            emitted = _emit_native(
                segments,
                native,
                capture_id=capture_id,
                number=number,
                geometry=geometry,
                deduplicated={},
                budget=budget,
            )
            # The raster this page's OCR needed was refused by the document
            # budget, so the breach standing in ``budget`` demotes the record.
            return (
                _unit_record(
                    UNIT_PDF_PAGE,
                    number,
                    OUTCOME_EMBEDDED_TEXT if emitted else OUTCOME_NO_RELIABLE_OCR_TEXT,
                    budget=budget,
                    published=bool(emitted),
                ),
                unmappable,
            )
        if effective_dpi < OCR_DPI:
            downscaled.append(
                {
                    "page": number,
                    "effectiveDpi": effective_dpi,
                    "limitation": "working_raster_bounds",
                }
            )
        ocr = _recognise(
            image, settings=settings, ocr_engine=ocr_engine, budget=budget, scale=scale
        )
        record = _merge_page(
            segments,
            native,
            ocr,
            capture_id=capture_id,
            number=number,
            geometry=geometry,
            settings=settings,
            budget=budget,
            effective_dpi=effective_dpi,
        )
        return record, unmappable
    finally:
        if image is not None:
            image.close()
        if text_page is not None:
            text_page.close()
        page.close()


def _extract_image_frame(
    source: Image.Image,
    frame_index: int,
    *,
    capture_id: str,
    image_format: str,
    settings: ExtractionSettings,
    ocr_engine: OcrEngine,
    budget: ExtractionBudget,
    segments: list[ExtractedSegment],
    aggregate_pixels: int,
) -> tuple[_UnitRecord, int]:
    """Decode, orient and recognise exactly one primary frame, then close it."""
    number = frame_index + 1
    source_width, source_height = source.size
    pixels = source_width * source_height
    # Header dimensions are checked before any decode, so an oversized image is
    # refused rather than allocated and only then downsampled.
    if pixels > MAX_FRAME_PIXELS:
        raise RasterExtractionInvalid("decoded image exceeds the accepted pixel bound")
    if aggregate_pixels + pixels > MAX_AGGREGATE_PIXELS:
        raise RasterExtractionInvalid(
            "decoded image source exceeds the aggregate pixel bound"
        )
    if not settings.runs_ocr:
        return (
            _UnitRecord(UNIT_IMAGE_FRAME, number, OUTCOME_EXCLUDED_BY_PROFILE),
            pixels,
        )
    declared_orientation = int(source.getexif().get(0x0112, 1) or 1)
    frame = None
    prepared = None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            # Orientation is applied exactly once, to a derived working copy.
            frame = ImageOps.exif_transpose(source.copy())
        assert frame is not None
        prepared, scale = _working_image(frame)
        transform: dict[str, Any] = {
            "space": GEOMETRY_SPACE,
            "sourceWidth": source_width,
            "sourceHeight": source_height,
            "orientedWidth": frame.width,
            "orientedHeight": frame.height,
            "exifOrientation": declared_orientation,
            # HEIF decoding applies the container's rotation itself and leaves
            # EXIF orientation at 1, so this single transpose never doubles it.
            "orientationAppliedByDecoder": image_format == "HEIF",
            "workingScale": round(scale, 6),
        }
        if scale != 1.0:
            transform["resolutionLimitation"] = "working_raster_bounds"
        ocr = _recognise(
            prepared,
            settings=settings,
            ocr_engine=ocr_engine,
            budget=budget,
            scale=scale,
        )
    finally:
        if prepared is not None:
            prepared.close()
        if frame is not None:
            frame.close()
    emitted = _emit_ocr(
        segments,
        ocr.lines,
        capture_id=capture_id,
        unit=UNIT_IMAGE_FRAME,
        number=number,
        settings=settings,
        geometry=transform,
        budget=budget,
    )
    _emit_observations(
        segments,
        ocr.observations,
        capture_id=capture_id,
        unit=UNIT_IMAGE_FRAME,
        number=number,
        settings=settings,
        geometry=transform,
        budget=budget,
    )
    return (
        _unit_record(
            UNIT_IMAGE_FRAME,
            number,
            OUTCOME_OCR_TEXT if emitted else OUTCOME_NO_RELIABLE_OCR_TEXT,
            budget=budget,
            published=bool(emitted),
            flags=(OUTCOME_LOW_CONFIDENCE,) if ocr.low_confidence else (),
            stats=ocr.stats(),
        ),
        pixels,
    )


def _is_primary_frame(source: Image.Image) -> bool:
    """Whether the current frame is a primary image rather than a reduced subfile.

    TIFF marks a reduced-resolution subfile through NewSubfileType bit 0, or the
    legacy SubfileType value 2. Those are thumbnails of another frame, not pages.
    """
    tags = getattr(source, "tag_v2", None)
    if tags is None:
        return True
    new_subfile_type = tags.get(254)
    if isinstance(new_subfile_type, int) and new_subfile_type & 0x1:
        return False
    return bool(tags.get(255) != 2)


def _auxiliary_frame_count(source: Image.Image) -> int:
    """Thumbnail, depth and HDR/auxiliary images attached to the current frame.

    They are inventoried and excluded by profile; they are never OCR'd as pages.
    """
    info = source.info
    return (
        len(info.get("thumbnails") or ())
        + len(info.get("depth_images") or ())
        + len(info.get("aux") or ())
    )


def _render_pdf_page(
    page: Any, geometry: _PageGeometry, *, budget: ExtractionBudget
) -> tuple[Image.Image | None, int, float]:
    """Rasterise one page within the working raster bounds.

    Returns the prepared working image, the effective DPI it was produced at and
    the pixels-per-point scale OCR geometry maps back through. A page the
    document's aggregate OCR budget can no longer pay for returns ``None``
    rather than a partial raster.
    """
    width = max(geometry.display_width, 1.0)
    height = max(geometry.display_height, 1.0)
    scale = OCR_DPI / 72.0
    if width * height * scale * scale > MAX_WORKING_PIXELS:
        scale = (MAX_WORKING_PIXELS / (width * height)) ** 0.5
    if max(width * scale, height * scale) > MAX_EDGE:
        scale = MAX_EDGE / max(width, height)
    if not budget.may_ocr(int(width * scale) * int(height * scale)):
        return None, max(1, round(scale * 72)), scale
    try:
        rendered = page.render(scale=scale).to_pil()
    except Exception as error:
        raise RasterExtractionInvalid("PDF page rasterisation failed") from error
    try:
        prepared, _ = _working_image(rendered)
    finally:
        rendered.close()
    return prepared, max(1, round(scale * 72)), scale


def _working_image(image: Image.Image) -> tuple[Image.Image, float]:
    """A bounded RGB/greyscale working copy; the original is never written to.

    Alpha is composited against a documented white background. Returns the
    working image and the scale applied to reach the working raster bounds, so
    recognised coordinates map back to source pixels.
    """
    scale = 1.0
    working = image
    pixels = image.width * image.height
    if pixels > MAX_WORKING_PIXELS or max(image.size) > MAX_EDGE:
        scale = min(
            (MAX_WORKING_PIXELS / max(pixels, 1)) ** 0.5,
            MAX_EDGE / max(max(image.size), 1),
        )
        working = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.Resampling.LANCZOS,
        )
    resized = working is not image
    if working.mode in {"RGBA", "LA", "PA"}:
        opaque = working.convert("RGBA")
        background = Image.new("RGB", opaque.size, "white")
        background.paste(opaque, mask=opaque.getchannel("A"))
        opaque.close()
        if resized:
            working.close()
        return background, scale
    if working.mode not in {"RGB", "L"}:
        converted = working.convert("RGB")
        if resized:
            working.close()
        return converted, scale
    return (working if resized else image.copy()), scale


def _native_lines(
    text_page: Any, geometry: _PageGeometry
) -> tuple[tuple[_NativeLine, ...], int]:
    """Embedded text spans with geometry, in a deterministic reading order.

    PDFium's rect order is not reading order, so the source rect ordinal is kept
    in the locator while the emitted order is the documented top-left heuristic.
    Spans the decoder could not map are recorded rather than presented as text.
    """
    count = text_page.count_rects()
    lines: list[_NativeLine] = []
    unmappable = 0
    for index in range(count):
        rect = text_page.get_rect(index)
        text = text_page.get_text_bounded(*rect)
        if not text or not text.strip():
            continue
        if "�" in text:
            unmappable += 1
            continue
        lines.append(_NativeLine(text, geometry.native_box(rect), index))
    lines.sort(key=lambda line: (round(line.box[1], 2), round(line.box[0], 2)))
    return tuple(lines), unmappable


def _recognise(
    image: Image.Image,
    *,
    settings: ExtractionSettings,
    ocr_engine: OcrEngine,
    budget: ExtractionBudget,
    scale: float,
) -> _UnitOcr:
    """One bounded recognition of one unit, with at most one alternate pass.

    The first pass uses the deterministic segmentation profile. A single
    documented alternate pass is allowed when it produces no reliable words; it
    counts against the same unit and document budget and is recorded. No loop
    over models or language combinations is permitted.
    """
    resources = settings.ocr
    assert resources is not None
    pixels = image.width * image.height
    if not budget.may_ocr(pixels):
        return _EMPTY_OCR
    passes: list[int] = []
    result = _EMPTY_OCR
    for psm in (PRIMARY_PSM, ALTERNATE_PSM):
        started = time.monotonic()
        data = ocr_engine(
            image,
            resources=resources,
            psm=psm,
            timeout_seconds=budget.unit_timeout(),
        )
        budget.spend_ocr(pixels, time.monotonic() - started)
        passes.append(psm)
        current = _parse_ocr(data, scale=scale, passes=tuple(passes))
        if current.lines or not result.observations:
            result = current
        else:
            # The alternate pass found nothing reliable either, so the first
            # pass's observations stay the retained record of what was seen.
            result = replace(result, passes=tuple(passes))
        if result.lines or budget.breach is not None or budget.ocr_seconds <= 0.0:
            break
    return result


def _parse_ocr(
    data: Mapping[str, Sequence[Any]], *, scale: float, passes: tuple[int, ...]
) -> _UnitOcr:
    """Group raw TSV rows into lines, retaining every raw confidence observation.

    Container rows carry a sentinel confidence and no text; they are counted but
    are not words. A zero or negative confidence on a real word is kept exactly
    as the engine reported it, and is never converted into a certainty.
    """
    texts = data.get("text") or ()
    grouped: dict[tuple[int, int, int], list[_OcrWord]] = {}
    candidates: list[float] = []
    sentinel_rows = 0
    for index, raw_text in enumerate(texts):
        text = str(raw_text).strip()
        confidence = _row_confidence(data, index)
        if not text or confidence is None:
            sentinel_rows += 1
            continue
        candidates.append(confidence)
        key = (
            _row_int(data, "block_num", index),
            _row_int(data, "par_num", index),
            _row_int(data, "line_num", index),
        )
        grouped.setdefault(key, []).append(
            _OcrWord(
                text=text,
                confidence=confidence,
                box=_scaled_box(data, index, scale),
                accepted=confidence >= WORD_CONFIDENCE_THRESHOLD,
            )
        )
    lines: list[_OcrLine] = []
    observations: list[_OcrLine] = []
    for ordinal, key in enumerate(sorted(grouped), start=1):
        words = tuple(grouped[key])
        line = _OcrLine(words, _bounding_box(words), ordinal)
        (lines if line.accepted_words else observations).append(line)
    accepted = [word.confidence for line in lines for word in line.accepted_words]
    below = sum(1 for value in candidates if value < LOW_CONFIDENCE_THRESHOLD)
    low = bool(accepted) and (
        statistics.median(accepted) < LOW_CONFIDENCE_THRESHOLD
        or below > LOW_CONFIDENCE_SHARE * len(candidates)
    )
    return _UnitOcr(
        tuple(lines), tuple(observations), len(candidates), sentinel_rows, low, passes
    )


def _row_confidence(data: Mapping[str, Sequence[Any]], index: int) -> float | None:
    try:
        return float(data["conf"][index])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _row_int(data: Mapping[str, Sequence[Any]], key: str, index: int) -> int:
    try:
        return int(data[key][index])
    except (KeyError, IndexError, TypeError, ValueError):
        return 0


def _scaled_box(
    data: Mapping[str, Sequence[Any]], index: int, scale: float
) -> tuple[float, float, float, float]:
    divisor = scale if scale > 0.0 else 1.0
    return (
        _row_int(data, "left", index) / divisor,
        _row_int(data, "top", index) / divisor,
        _row_int(data, "width", index) / divisor,
        _row_int(data, "height", index) / divisor,
    )


def _bounding_box(words: Sequence[_OcrWord]) -> tuple[float, float, float, float]:
    if not words:
        return (0.0, 0.0, 0.0, 0.0)
    left = min(word.box[0] for word in words)
    top = min(word.box[1] for word in words)
    right = max(word.box[0] + word.box[2] for word in words)
    bottom = max(word.box[1] + word.box[3] for word in words)
    return (left, top, right - left, bottom - top)


def _merge_page(
    segments: list[ExtractedSegment],
    native: tuple[_NativeLine, ...],
    ocr: _UnitOcr,
    *,
    capture_id: str,
    number: int,
    geometry: _PageGeometry,
    settings: ExtractionSettings,
    budget: ExtractionBudget,
    effective_dpi: int,
) -> _UnitRecord:
    """Deduplicate one page's two methods geometrically, then emit it.

    Identical text at different positions, or on another page, is never merged:
    the test is the overlap of the two regions first and the text second.
    """
    duplicates: dict[int, _OcrLine] = {}
    conflicts: list[tuple[int, _OcrLine]] = []
    ocr_only: list[_OcrLine] = []
    for line in ocr.lines:
        best_index: int | None = None
        best_ratio = OVERLAP_RATIO
        for index, candidate in enumerate(native):
            ratio = _overlap_ratio(candidate.box, line.box)
            if ratio >= best_ratio:
                best_index, best_ratio = index, ratio
        if best_index is None:
            ocr_only.append(line)
        elif _text_agrees(native[best_index].text, line.text):
            # One searchable segment with two method observations. A further
            # agreeing reading of the same span adds no words to append.
            duplicates.setdefault(best_index, line)
        else:
            conflicts.append((best_index, line))
    geometry_fields = {**geometry.to_dict(), "effectiveDpi": effective_dpi}
    emitted_native = _emit_native(
        segments,
        native,
        capture_id=capture_id,
        number=number,
        geometry=geometry,
        deduplicated=duplicates,
        budget=budget,
    )
    emitted_ocr = _emit_ocr(
        segments,
        tuple(ocr_only),
        capture_id=capture_id,
        unit=UNIT_PDF_PAGE,
        number=number,
        settings=settings,
        geometry=geometry_fields,
        budget=budget,
    )
    for native_index, line in conflicts:
        _emit_alternative(
            segments,
            line,
            capture_id=capture_id,
            number=number,
            settings=settings,
            geometry=geometry_fields,
            primary_rect_ordinal=native[native_index].rect_ordinal,
            budget=budget,
        )
    _emit_observations(
        segments,
        ocr.observations,
        capture_id=capture_id,
        unit=UNIT_PDF_PAGE,
        number=number,
        settings=settings,
        geometry=geometry_fields,
        budget=budget,
    )
    if conflicts:
        outcome = OUTCOME_CONFLICTING_READINGS
    elif duplicates or (emitted_native and emitted_ocr):
        outcome = OUTCOME_BOTH_DEDUPLICATED
    elif emitted_native:
        # Reliable embedded text stays searchable when OCR accepted no words.
        outcome = OUTCOME_EMBEDDED_TEXT
    elif emitted_ocr:
        outcome = OUTCOME_OCR_TEXT
    else:
        outcome = OUTCOME_NO_RELIABLE_OCR_TEXT
    return _unit_record(
        UNIT_PDF_PAGE,
        number,
        outcome,
        budget=budget,
        published=bool(emitted_native or emitted_ocr),
        flags=(OUTCOME_LOW_CONFIDENCE,) if ocr.low_confidence else (),
        stats=ocr.stats(),
    )


def _overlap_ratio(
    first: tuple[float, float, float, float], second: tuple[float, float, float, float]
) -> float:
    """Intersection as a share of the smaller region: the versioned grouping rule."""
    width = max(
        0.0, min(first[0] + first[2], second[0] + second[2]) - max(first[0], second[0])
    )
    height = max(
        0.0, min(first[1] + first[3], second[1] + second[3]) - max(first[1], second[1])
    )
    smaller = min(first[2] * first[3], second[2] * second[3])
    return 0.0 if smaller <= 0.0 else (width * height) / smaller


def _text_agrees(native: str, recognised: str) -> bool:
    """Whether two overlapping readings are one reading rather than a conflict."""
    left, right = _normalise_text(native), _normalise_text(recognised)
    if not left or not right:
        return False
    if left == right:
        return True
    return SequenceMatcher(None, left, right).ratio() >= TEXT_SIMILARITY_RATIO


def _emit_native(
    segments: list[ExtractedSegment],
    native: Sequence[_NativeLine],
    *,
    capture_id: str,
    number: int,
    geometry: _PageGeometry,
    deduplicated: Mapping[int, _OcrLine],
    budget: ExtractionBudget,
) -> int:
    """Emit embedded-text spans as the primary decoded source.

    A span an overlapping OCR reading agreed with becomes one searchable segment
    carrying two method observations; the same words are never appended twice.
    """
    emitted = 0
    for index, line in enumerate(native):
        if not budget.take_text(line.text):
            break
        duplicate = deduplicated.get(index)
        locator: dict[str, Any] = {
            "captureId": capture_id,
            "unit": UNIT_PDF_PAGE,
            "page": number,
            "method": "native_text",
            "methods": ["native_text", "ocr"] if duplicate else ["native_text"],
            "rectOrdinal": line.rect_ordinal,
            "box": [round(value, 3) for value in line.box],
            **geometry.to_dict(),
        }
        if duplicate is not None:
            locator["ocrConfidence"] = duplicate.confidence
            locator["ocrLine"] = duplicate.line_ordinal
            locator["ocrBox"] = [round(value, 3) for value in duplicate.box]
        # Directly decoded text carries no engine confidence: a fabricated
        # certainty would be a claim the decoder never made.
        segments.append(ExtractedSegment("body", locator, text=line.text))
        emitted += 1
    return emitted


def _emit_ocr(
    segments: list[ExtractedSegment],
    lines: Sequence[_OcrLine],
    *,
    capture_id: str,
    unit: str,
    number: int,
    settings: ExtractionSettings,
    geometry: Mapping[str, Any],
    budget: ExtractionBudget,
) -> int:
    """Emit recognised regions that no reliable embedded text already covers."""
    emitted = 0
    for line in lines:
        text = line.text
        if not text or not budget.take_text(text):
            break
        segments.append(
            ExtractedSegment(
                "body",
                _ocr_locator(
                    line,
                    capture_id=capture_id,
                    unit=unit,
                    number=number,
                    settings=settings,
                    geometry=geometry,
                ),
                text=text,
                extraction_method="ocr",
                language=_language_claim(settings),
                confidence=line.confidence,
            )
        )
        emitted += 1
    return emitted


def _emit_alternative(
    segments: list[ExtractedSegment],
    line: _OcrLine,
    *,
    capture_id: str,
    number: int,
    settings: ExtractionSettings,
    geometry: Mapping[str, Any],
    primary_rect_ordinal: int,
    budget: ExtractionBudget,
) -> None:
    """Retain a materially disagreeing reading as an explicitly linked alternative.

    It is kept outside ordinary retrieval so two contradictory sentences are
    never concatenated into one searchable body segment.
    """
    text = line.text
    if not text or not budget.take_text(text):
        return
    locator = _ocr_locator(
        line,
        capture_id=capture_id,
        unit=UNIT_PDF_PAGE,
        number=number,
        settings=settings,
        geometry=geometry,
    )
    locator["observation"] = "alternative_reading"
    locator["conflict"] = True
    locator["alternativeOfRectOrdinal"] = primary_rect_ordinal
    segments.append(
        ExtractedSegment(
            "hidden_content",
            locator,
            text=text,
            extraction_method="ocr",
            visibility="hidden",
            language=_language_claim(settings),
            confidence=line.confidence,
        )
    )


def _emit_observations(
    segments: list[ExtractedSegment],
    observations: Sequence[_OcrLine],
    *,
    capture_id: str,
    unit: str,
    number: int,
    settings: ExtractionSettings,
    geometry: Mapping[str, Any],
    budget: ExtractionBudget,
) -> None:
    """Retain below-threshold candidates so a reviewer can trace the selection.

    They carry a non-retrieval role, so they stay out of ordinary text search
    while their raw geometry and raw engine confidence remain available.
    """
    for line in observations:
        text = line.text
        if not text or not budget.take_text(text):
            return
        locator = _ocr_locator(
            line,
            capture_id=capture_id,
            unit=unit,
            number=number,
            settings=settings,
            geometry=geometry,
        )
        locator["observation"] = "below_confidence_threshold"
        segments.append(
            ExtractedSegment(
                "hidden_content",
                locator,
                text=text,
                extraction_method="ocr",
                visibility="hidden",
                language=_language_claim(settings),
                confidence=line.confidence,
            )
        )


def _ocr_locator(
    line: _OcrLine,
    *,
    capture_id: str,
    unit: str,
    number: int,
    settings: ExtractionSettings,
    geometry: Mapping[str, Any],
) -> dict[str, Any]:
    resources = settings.ocr
    assert resources is not None
    return {
        "captureId": capture_id,
        "unit": unit,
        ("page" if unit == UNIT_PDF_PAGE else "frame"): number,
        "method": "ocr",
        "line": line.line_ordinal,
        "box": [round(value, 3) for value in line.box],
        "words": [word.to_dict() for word in line.words],
        "confidenceScale": CONFIDENCE_SCALE,
        "acceptThreshold": WORD_CONFIDENCE_THRESHOLD,
        "ocrEngine": f"{resources.engine}-{resources.engine_revision}",
        "ocrLanguages": list(resources.languages),
        **dict(geometry),
    }


def _language_claim(settings: ExtractionSettings) -> str | None:
    """The language claim for a recognised segment, or none when it is unknown.

    With more than one model loaded the engine does not report which one read a
    line, so no single language is claimed; the ordered list stays in the locator.
    """
    resources = settings.ocr
    assert resources is not None
    return resources.languages[0] if len(resources.languages) == 1 else None


def _raster_extraction(
    segments: Sequence[ExtractedSegment],
    records: Sequence[_UnitRecord],
    *,
    unit_type: str,
    expected: int,
    settings: ExtractionSettings,
    budget: ExtractionBudget,
    limitations: dict[str, Any],
) -> RasterExtraction:
    """Reconcile the per-unit outcomes into exact coverage counts.

    The coverage buckets partition the visited units exactly. A unit cut short
    by a budget breach lands in whichever single bucket is true of it: useful
    coverage when it still published text, and coverage never reached - not an
    observed blank - when it did not.
    """
    counts = dict.fromkeys(UNIT_OUTCOMES, 0)
    visited = 0
    useful = 0
    truncated_unreached = 0
    for record in records:
        counts[record.outcome] += 1
        for flag in record.flags:
            counts[flag] += 1
        if record.outcome == OUTCOME_TRUNCATED_BY_LIMIT and not record.published:
            truncated_unreached += 1
            continue
        visited += 1
        if record.outcome in _USEFUL_OUTCOMES or (
            record.outcome == OUTCOME_TRUNCATED_BY_LIMIT and record.published
        ):
            useful += 1
    resources = settings.ocr
    merged = {
        **limitations,
        "processingMode": settings.processing_mode,
        "workerBuild": WORKER_BUILD,
        "ocrEngine": (
            "none"
            if resources is None
            else f"{resources.engine}-{resources.engine_revision}"
        ),
        "ocrLanguages": [] if resources is None else list(resources.languages),
        "ocrModelHashes": [] if resources is None else list(resources.model_hashes),
        "unitOutcomes": {name: counts[name] for name in UNIT_OUTCOMES if counts[name]},
        "units": [record.to_dict() for record in records],
        "knownEmptyMeaning": "no_reliable_text_observed_not_proven_blank",
    }
    if budget.breach is not None:
        merged["limitBreach"] = budget.breach
    if resources is not None and not resources.available:
        # The selected profile keeps its mode and its requested ordered
        # languages; what it does not claim is that models it never had were
        # used. The exact missing resources and the recovery stay here.
        merged["ocrResourceState"] = RESOURCE_STATE_UNAVAILABLE
        merged.update(resources.to_limitations())
    return RasterExtraction(
        segments=tuple(segments),
        unit_type=unit_type,
        expected=max(expected, len(records)),
        visited=visited,
        useful_units=useful,
        known_empty_units=counts[OUTCOME_NO_RELIABLE_OCR_TEXT],
        excluded_units=counts[OUTCOME_EXCLUDED_BY_PROFILE],
        unsupported_units=counts[OUTCOME_UNSUPPORTED_CONTENT],
        failed_units=counts[OUTCOME_PROCESSING_FAILURE],
        unknown_remaining=max(0, expected - len(records)) + truncated_unreached,
        limitations=merged,
    )


def _normalise_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


__all__ = [
    "DEDUPLICATION_VERSION",
    "EMBEDDED_DISABLED",
    "EMBEDDED_MALFORMED",
    "EMBEDDED_NOT_EMBEDDED",
    "EMBEDDED_NO_TEXT",
    "EMBEDDED_OUTCOMES",
    "EMBEDDED_RECOGNISED",
    "EMBEDDED_RESOURCES_UNAVAILABLE",
    "EMBEDDED_TRUNCATED",
    "EMBEDDED_UNSUPPORTED",
    "GEOMETRY_SPACE",
    "IMAGE_DECODER_BUILD",
    "IMAGE_FORMAT_BY_MEDIA_TYPE",
    "MAX_AGGREGATE_PIXELS",
    "MAX_DECODE_SECONDS",
    "MAX_EDGE",
    "MAX_FRAMES",
    "MAX_FRAME_PIXELS",
    "MAX_PDF_PAGES",
    "MAX_SOURCE_BYTES",
    "MAX_TEXT_BYTES",
    "MAX_WORKING_PIXELS",
    "OUTCOME_BOTH_DEDUPLICATED",
    "OUTCOME_CONFLICTING_READINGS",
    "OUTCOME_EMBEDDED_TEXT",
    "OUTCOME_EXCLUDED_BY_PROFILE",
    "OUTCOME_LOW_CONFIDENCE",
    "OUTCOME_NO_RELIABLE_OCR_TEXT",
    "OUTCOME_OCR_TEXT",
    "OUTCOME_PROCESSING_FAILURE",
    "OUTCOME_TRUNCATED_BY_LIMIT",
    "OUTCOME_UNSUPPORTED_CONTENT",
    "OVERLAP_RATIO",
    "PDF_DECODER_BUILD",
    "READING_ORDER_VERSION",
    "UNIT_IMAGE_FRAME",
    "UNIT_OUTCOMES",
    "UNIT_PDF_PAGE",
    "WORKER_BUILD",
    "EmbeddedImageCandidate",
    "EmbeddedImageResult",
    "ExtractionBudget",
    "RasterExtraction",
    "RasterExtractionInvalid",
    "embedded_image_gate",
    "extract_embedded_image",
    "extract_image",
    "extract_pdf",
]
