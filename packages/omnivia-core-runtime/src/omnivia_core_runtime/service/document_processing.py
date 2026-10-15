"""Bounded processing of durable document-capture outbox obligations."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.service import document_intake
from omnivia_core_runtime.service.document_extractors import (
    HTML_MEDIA_TYPES,
    MAX_HTML_SOURCE_BYTES,
    DocumentExtractionInvalid,
    DocumentExtractionUnsupported,
    extract_document,
)
from omnivia_core_runtime.service.ocr_resources import (
    MODE_TEXT_AND_LOCAL_OCR,
    MODE_TEXT_ONLY,
    OCR_SECONDS_PER_DOCUMENT,
    PACKAGED_OCR_CATALOGUE,
    REASON_NEEDS_LANGUAGE,
    ExtractionSettings,
    OcrSelectionInvalid,
    TrustedOcrCatalogue,
)
from omnivia_core_runtime.service.raster_extractors import (
    DEDUPLICATION_VERSION,
    IMAGE_DECODER_BUILD,
    MAX_FRAME_PIXELS,
    MAX_SOURCE_BYTES,
    MAX_WORKING_PIXELS,
    PDF_DECODER_BUILD,
    READING_ORDER_VERSION,
)
from omnivia_core_runtime.service.raster_extractors import (
    MAX_DECODE_SECONDS as RASTER_DECODE_SECONDS,
)
from omnivia_core_runtime.service.raster_extractors import (
    MAX_TEXT_BYTES as RASTER_TEXT_BYTES,
)
from omnivia_core_runtime.service.runner import ServiceRunner
from omnivia_core_runtime.storage import document_intake as store

DEFAULT_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_PROCESSING_BUDGET = 4
OCR_MEDIA_TYPES = frozenset(
    {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/tiff",
        "image/heic",
        "image/heif",
        "image/webp",
    }
)
#: The profile reference that selects the explicit faster Text-only profile.
TEXT_ONLY_PROFILE_REF = "text-only"
#: The profile reference that selects the product default: Text and local OCR where the
#: media type qualifies for it, and direct text everywhere else. Named here, rather than
#: left to whatever string a caller sends, because `document.history` reports the exact
#: approved selections a reprocess may name and a caller must not synthesise its own.
DEFAULT_PROFILE_REF = "document-default"
#: The one approved revision of both references in this build. It is part of the profile's
#: content address through `decoderBuild`, so changing it is a new profile identity.
APPROVED_PROFILE_REVISION = "1"
#: Bumped with the raster/OCR adapter's geometry, dedup and coverage contract.
_ADAPTER_VERSION = "2"
#: The limitation code a Needs OCR language outcome carries.
NEEDS_OCR_LANGUAGE = REASON_NEEDS_LANGUAGE


@dataclass
class DocumentProcessingExecutor:
    """Rate-limited service-owned document processing.

    The service tick calls this frequently. The executor keeps each pass bounded and
    shares the runner's SQLite gate with request transports; extraction itself runs
    outside the gate so a large document cannot monopolise request admission.
    """

    runner: ServiceRunner
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    #: What the signed installation installed, and the configured ordered
    #: recognition languages. Both are trusted configuration, never document
    #: input. ``None`` reads the packaged catalogue at use time.
    ocr_catalogue: TrustedOcrCatalogue | None = None
    ocr_languages: Sequence[str] = field(default_factory=tuple)
    _next_poll: float = 0.0

    def run_pending(
        self,
        *,
        budget: int = DEFAULT_PROCESSING_BUDGET,
        force: bool = False,
    ) -> list[store.StoredResult]:
        if budget <= 0:
            return []
        now = self.runner.clock.monotonic()
        if not force and now < self._next_poll:
            return []
        self._next_poll = now + max(self.poll_interval_seconds, 0.0)
        try:
            return process_capture_outbox(
                self.runner,
                limit=budget,
                ocr_catalogue=self.ocr_catalogue,
                ocr_languages=self.ocr_languages,
            )
        except sqlite3.Error:
            # SQLite contention and lost ownership leave the durable outbox intact.
            # A later bounded pass replays the same operation key.
            return []


def resolve_settings(
    *,
    media_type: str,
    profile_ref: str,
    catalogue: TrustedOcrCatalogue | None = None,
    languages: Sequence[str] = (),
) -> ExtractionSettings:
    """The trusted processing decisions for one capture.

    Nothing here reads the document. The mode comes from the configured profile
    reference, and the ordered recognition languages from the installation's
    configured selection, checked against what the signed installation actually
    installed and verified. A missing or unverified model does not change the
    selected mode and never quietly becomes English: the returned settings keep
    the Text-and-local-OCR identity and the requested order, and say that the
    resources were not served. A malformed selection is a configuration fault
    and is raised.
    """
    if media_type not in OCR_MEDIA_TYPES or profile_ref == TEXT_ONLY_PROFILE_REF:
        return ExtractionSettings(processing_mode=MODE_TEXT_ONLY)
    catalogue = catalogue if catalogue is not None else PACKAGED_OCR_CATALOGUE
    return ExtractionSettings(
        processing_mode=MODE_TEXT_AND_LOCAL_OCR, ocr=catalogue.resolve(languages)
    )


def portable_profile(
    *,
    media_type: str,
    profile_ref: str,
    revision: str,
    settings: ExtractionSettings | None = None,
) -> dict[str, Any]:
    """The immutable profile for the portable direct-text and local-OCR worker."""
    adapter = {
        "text/plain": "plain-text",
        "text/markdown": "markdown-text",
        "text/csv": "delimited-text",
        "text/tab-separated-values": "delimited-text",
        "text/html": "inert-html",
        "application/xhtml+xml": "inert-html",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx-ooxml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx-ooxml",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx-ooxml",
        "application/pdf": "pdfium-text-and-raster",
        "image/png": "pillow-image-ocr",
        "image/jpeg": "pillow-image-ocr",
        "image/tiff": "pillow-image-ocr",
        "image/heic": "pillow-image-ocr",
        "image/heif": "pillow-image-ocr",
        "image/webp": "pillow-image-ocr",
        "application/rtf": "pandoc-rtf-json",
        "text/rtf": "pandoc-rtf-json",
    }.get(media_type, "unsupported")
    settings = settings or resolve_settings(
        media_type=media_type, profile_ref=profile_ref
    )
    is_pdf = media_type == "application/pdf"
    is_image = media_type in OCR_MEDIA_TYPES and not is_pdf
    uses_ocr = settings.runs_ocr
    if is_pdf:
        decoder = PDF_DECODER_BUILD
    elif is_image:
        decoder = IMAGE_DECODER_BUILD
    else:
        decoder = f"{adapter}-{_ADAPTER_VERSION}"
    return {
        "schema": store.PROFILE_SCHEMA,
        "adapter": adapter,
        "adapterVersion": _ADAPTER_VERSION,
        # The caller's trusted profile reference plus the exact decoder build and
        # native binary identity the result was produced by.
        "decoderBuild": f"{profile_ref}:{revision}:{decoder}",
        "processingMode": settings.processing_mode,
        "ocr": (
            settings.ocr.to_profile()
            if settings.ocr is not None
            else {
                "engine": "none",
                "engineRevision": "none",
                "modelHashes": [],
                "languages": [],
            }
        ),
        "embeddedImageOcr": settings.embedded_image_ocr,
        "revisionView": "current_text_with_separate_annotations",
        "secondaryContentPolicy": "preserve_role_and_visibility",
        "formulaPolicy": "expression_and_saved_cache_without_calculation",
        "html": {"encoding": "utf-8"},
        "delimited": {
            "encoding": "bom_or_utf-8",
            "delimiter": "media_default",
            "quote": '"',
        },
        "limits": _profile_limits(
            media_type=media_type, is_pdf=is_pdf, is_image=is_image, uses_ocr=uses_ocr
        ),
        "readingOrderVersion": (
            READING_ORDER_VERSION if (is_pdf or is_image) else "source-order-1"
        ),
        "deduplicationVersion": DEDUPLICATION_VERSION if uses_ocr else "none-1",
        "parserFeatures": sorted(
            {
                "direct_text",
                "inert_references",
                "located_segments",
                *(["page_geometry", "pdf_raster"] if is_pdf else []),
                *(["frame_orientation", "frame_inventory"] if is_image else []),
                *(
                    [
                        "local_ocr",
                        "raw_confidence_observations",
                        "spatial_deduplication",
                    ]
                    if uses_ocr
                    else []
                ),
            }
        ),
        "unknownFeaturePolicy": "partial",
        "completenessPolicy": "explicit_unit_coverage",
    }


@dataclass(frozen=True, slots=True)
class ReprocessProfileOption:
    """One approved profile selection, resolved exactly once.

    `settings` and `profile` are the *same* objects the reprocess handler executes and
    records, not a second resolution of the same reference: the digest a caller is offered
    is therefore the digest of the settings that ran, and a text-only selection cannot be
    reported as text-only and then executed under the default. The digest is not a promise
    about resources -- whether the pinned OCR models were served is recorded in the profile
    itself and reported in the extraction's own outcome.
    """

    profile_ref: str
    revision: str
    processing_mode: str
    profile_digest: str
    settings: ExtractionSettings
    profile: Mapping[str, Any]


def approved_reprocess_profiles(*, media_type: str) -> tuple[ReprocessProfileOption, ...]:
    """The profile selections Core will accept for a reprocess of this media type.

    One option for an ordinary document, because Text-only and the default resolve to the
    same processing for it and offering two identical choices would be a false choice. Two
    for a media type that qualifies for local OCR, which is the product's stated pair: Text
    and local OCR as the default, Text only as the explicit alternate. An unqualified format
    has no option at all rather than one that would be refused.

    This is the single place a reprocess profile is resolved. `document.history` reports what
    it returns and `document.reprocess` executes what it returns, so neither can name a
    selection the other would resolve differently.
    """
    references = (
        (DEFAULT_PROFILE_REF, TEXT_ONLY_PROFILE_REF)
        if media_type in OCR_MEDIA_TYPES
        else (DEFAULT_PROFILE_REF,)
    )
    options: list[ReprocessProfileOption] = []
    for reference in references:
        settings = resolve_settings(media_type=media_type, profile_ref=reference)
        profile = portable_profile(
            media_type=media_type,
            profile_ref=reference,
            revision=APPROVED_PROFILE_REVISION,
            settings=settings,
        )
        if profile["adapter"] == "unsupported":
            return ()
        options.append(
            ReprocessProfileOption(
                profile_ref=reference,
                revision=APPROVED_PROFILE_REVISION,
                processing_mode=str(profile["processingMode"]),
                profile_digest=store.profile_digest(profile),
                settings=settings,
                profile=profile,
            )
        )
    return tuple(options)


def _profile_limits(
    *, media_type: str, is_pdf: bool, is_image: bool, uses_ocr: bool
) -> dict[str, int]:
    """The ceilings this profile was produced under, as actually enforced."""
    if media_type in {"text/plain", "text/markdown"}:
        source_bytes = 1024 * 1024
    elif media_type in HTML_MEDIA_TYPES:
        # Published from the constant the markup adapter enforces, so the two
        # cannot drift into a profile that claims a bound nothing applies.
        source_bytes = MAX_HTML_SOURCE_BYTES
    elif media_type in {"application/rtf", "text/rtf"}:
        source_bytes = 20 * 1024 * 1024
    else:
        source_bytes = MAX_SOURCE_BYTES
    if is_image:
        decoded_pixels = MAX_FRAME_PIXELS
    elif is_pdf:
        decoded_pixels = MAX_WORKING_PIXELS
    else:
        decoded_pixels = 1
    return {
        "sourceBytes": source_bytes,
        "decodedPixels": decoded_pixels,
        "containerEntries": 10_000,
        "expandedBytes": 512 * 1024 * 1024,
        "outputTextBytes": RASTER_TEXT_BYTES,
        "outputStructuredBytes": 128 * 1024 * 1024,
        "memoryBytes": 1024 * 1024 * 1024,
        "timeMs": (OCR_SECONDS_PER_DOCUMENT if uses_ocr else RASTER_DECODE_SECONDS)
        * 1000,
        "temporaryBytes": 2 * 1024 * 1024 * 1024,
    }


def process_capture_outbox(
    runner: ServiceRunner,
    *,
    limit: int = 16,
    ocr_catalogue: TrustedOcrCatalogue | None = None,
    ocr_languages: Sequence[str] = (),
) -> list[store.StoredResult]:
    """Process the oldest capture obligations, settling each terminal result."""
    assert runner.connection is not None and runner.workspace_id is not None
    assert runner.identity is not None and runner.generation is not None
    with runner.sqlite_gate:
        obligations = store.pending_outbox(
            runner.connection, workspace_id=runner.workspace_id, limit=limit
        )
    completed: list[store.StoredResult] = []
    for obligation in obligations:
        with runner.sqlite_gate:
            capture = store.read_capture(
                runner.connection,
                workspace_id=runner.workspace_id,
                capture_id=obligation.capture_id,
            )
        if capture is None:
            continue
        selection_invalid = False
        try:
            settings = resolve_settings(
                media_type=capture.media_type,
                profile_ref=capture.processing_profile_ref,
                catalogue=ocr_catalogue,
                languages=ocr_languages,
            )
        except OcrSelectionInvalid:
            # The configured selection is not a selection at all, so there is no
            # requested local-OCR profile to keep: the revision is admitted under
            # the one profile that pins no OCR resources and settles as failed.
            selection_invalid = True
            settings = ExtractionSettings(processing_mode=MODE_TEXT_ONLY)
        profile = portable_profile(
            media_type=capture.media_type,
            profile_ref=capture.processing_profile_ref,
            revision=capture.processing_profile_revision,
            settings=settings,
        )
        with runner.sqlite_gate:
            profile_digest = document_intake.register_profile(runner, profile)
            head = store.read_head(
                runner.connection,
                workspace_id=runner.workspace_id,
                document_id=capture.document_id,
            )
        assert head is not None
        operation_key = (
            "extract-"
            + hashlib.sha256(
                f"{capture.capture_id}\0{profile_digest}".encode()
            ).hexdigest()
        )
        with runner.sqlite_gate:
            revision, _ = document_intake.admit_extraction(
                runner,
                capture_id=capture.capture_id,
                profile_digest=profile_digest,
                expected_base_extraction_id=head.current_extraction_id,
                operation_key=operation_key,
            )
        if selection_invalid:
            outcome = _terminal_without_segments("failed", "ocr_selection_invalid")
        else:
            try:
                with runner.sqlite_gate:
                    content = document_intake.read_retained_source(
                        runner, capture_id=capture.capture_id
                    )
                outcome = extract_document(
                    content,
                    media_type=capture.media_type,
                    capture_id=capture.capture_id,
                    settings=settings,
                )
            except DocumentExtractionUnsupported:
                outcome = _terminal_without_segments(
                    "unsupported", "format_not_qualified"
                )
            except (DocumentExtractionInvalid, document_intake.DocumentIntakeRefused):
                outcome = _terminal_without_segments("failed", "source_invalid")
        with runner.sqlite_gate:
            result = document_intake.complete_extraction(
                runner,
                extraction_revision_id=revision.extraction_revision_id,
                outcome=outcome,
            )
            with fenced_transaction(
                runner.connection,
                runner.identity,
                workspace_id=runner.workspace_id,
                fencing_generation=runner.generation,
            ):
                store.settle_outbox(
                    runner.connection,
                    workspace_id=runner.workspace_id,
                    capture_id=capture.capture_id,
                    fencing_generation=runner.generation,
                    now_us=time.time_ns() // 1000,
                )
        completed.append(result)
    return completed


def _terminal_without_segments(
    state: str, limitation: str
) -> document_intake.ExtractionOutcome:
    return document_intake.ExtractionOutcome(
        state=state,
        coverage={
            "schema": "omnivia.extraction-coverage.v2",
            "units": [
                {
                    "unitType": "document",
                    "expected": 1,
                    "visited": 1,
                    "usefulText": 0,
                    "knownEmpty": 0,
                    "excludedByProfile": 0,
                    "unsupported": int(state == "unsupported"),
                    "failed": int(state == "failed"),
                    "unknownRemaining": 0,
                }
            ],
            "roleCoverage": {},
        },
        limitations={"code": limitation},
        worker_build="core-portable-text-1",
    )
