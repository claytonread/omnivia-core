"""Bounded processing of durable document-capture outbox obligations.

Every obligation executes from the capture row's own frozen
``processing_settings_json`` (migration 0071) and from nothing process-global.
That snapshot decides the mode, the ordered recognition languages and whether
embedded raster images are examined; the installation's managed resources are
consulted only at this point, and only for a selection that snapshot names. A
preference changed, or the service restarted, between admission and execution
therefore cannot move what a queued or active obligation does -- the obligation
is replayed from the same row under the same operation key, and the key is
derived from the profile that row resolves to.

Two deliberate boundaries:

**Text-only never resolves OCR resources.** Not "resolves them and ignores the
answer": :func:`resolve_settings` returns before the catalogue is consulted, so a
text-only capture takes no coordination lock on the managed cache and its profile
pins no engine, model or language at all.

**A null snapshot is the one compatibility path, and it is deterministic.** It
means exactly what migration 0071 says it means -- a capture admitted before the
column existed, which recorded no selection -- and it executes the behaviour that
applied when it was admitted: the mode its own profile reference names, no
embedded-image pass, and an *unconfigured* language request, which reports
``ocr_resources_unconfigured`` rather than claiming English was asked for. A
non-null snapshot that does not decode is never reinterpreted that way: it
settles as a failed extraction naming :data:`PROCESSING_SETTINGS_INVALID`.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
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
from omnivia_core_runtime.service.processing_settings import (
    DEFAULT_SETTINGS,
    ProcessingSettings,
    ProcessingSettingsInvalid,
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
#: The limitation code a capture whose frozen snapshot does not decode settles with.
#: Distinct from every resource condition: the preferences this capture was admitted
#: under cannot be established at all, so there is no requested profile to keep and
#: nothing about the installation to recover by installing.
PROCESSING_SETTINGS_INVALID = "processing_settings_invalid"

#: Resolves the trusted catalogue for one *frozen* ordered selection, at the moment
#: the work runs. A callable rather than a catalogue because the managed cache
#: publishes one directory per selection, so there is no single catalogue that could
#: serve every selection -- see
#: :func:`~omnivia_core_runtime.service.processing_settings.installation_catalogue`.
CatalogueFor = Callable[[Sequence[str]], TrustedOcrCatalogue]


@dataclass
class DocumentProcessingExecutor:
    """Rate-limited service-owned document processing.

    The service tick calls this frequently. The executor keeps each pass bounded and
    shares the runner's SQLite gate with request transports; extraction itself runs
    outside the gate so a large document cannot monopolise request admission.
    """

    runner: ServiceRunner
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    #: How a frozen ordered selection becomes a trusted catalogue, asked at the
    #: moment an obligation runs. The executor holds no language of its own:
    #: which languages are wanted is the capture row's frozen snapshot, and this
    #: answers only whether the installation can serve them. ``None`` reads the
    #: packaged fail-closed catalogue, which serves nothing.
    catalogue_for: CatalogueFor | None = None
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
                self.runner, limit=budget, catalogue_for=self.catalogue_for
            )
        except sqlite3.Error:
            # SQLite contention and lost ownership leave the durable outbox intact.
            # A later bounded pass replays the same operation key.
            return []


def recognises(settings: ProcessingSettings, media_type: str) -> bool:
    """Whether these preferences would run recognition over this media type.

    Two independent ways recognition can be wanted: the media type is one the
    engine reads whole (a PDF or an image), or the source carries raster images
    *inside* it and the embedded-image preference applies to it. Text-only wants
    neither. Decided in one place because three callers need the same answer --
    what to resolve, what limits to publish, and whether a reprocess has a real
    alternate to offer -- and three copies of it would be three chances for the
    executed profile and the offered one to disagree.
    """
    return settings.mode == MODE_TEXT_AND_LOCAL_OCR and (
        media_type in OCR_MEDIA_TYPES or settings.embedded_image_ocr_for(media_type)
    )


def resolve_settings(
    *,
    media_type: str,
    profile_ref: str,
    settings: ProcessingSettings | None = DEFAULT_SETTINGS,
    catalogue_for: CatalogueFor | None = None,
) -> ExtractionSettings:
    """The trusted processing decisions for one capture.

    Nothing here reads the document. `settings` is the capture's own frozen
    snapshot -- its mode, its ordered recognition languages and its
    embedded-image preference -- and it is the only source of all three. The
    installation is consulted once, through `catalogue_for`, and only for the
    languages that snapshot names.

    A missing or unverified model does not change the selected mode and never
    quietly becomes English: the returned settings keep the Text-and-local-OCR
    identity and the requested order, and say that the resources were not served.
    A malformed selection is a configuration fault and is raised.

    ``settings=None`` is the documented compatibility path for a capture admitted
    before migration 0071, which recorded no selection: the mode is the one its
    profile reference names, no embedded-image pass runs, and the language request
    is *empty* -- an unconfigured installation, which is what such a capture was
    admitted under.
    """
    languages: Sequence[str] = () if settings is None else settings.languages
    embedded = settings is not None and settings.embedded_image_ocr_for(media_type)
    qualifies = (
        media_type in OCR_MEDIA_TYPES
        if settings is None
        else recognises(settings, media_type)
    )
    text_only = (
        profile_ref == TEXT_ONLY_PROFILE_REF
        or (settings is not None and settings.mode == MODE_TEXT_ONLY)
        or not qualifies
    )
    if text_only:
        # Returned before the catalogue is asked anything: a text-only capture
        # resolves no OCR resource, takes no lock on the managed cache and pins
        # no model it would never read.
        return ExtractionSettings(processing_mode=MODE_TEXT_ONLY)
    catalogue = (
        PACKAGED_OCR_CATALOGUE if catalogue_for is None else catalogue_for(languages)
    )
    return ExtractionSettings(
        processing_mode=MODE_TEXT_AND_LOCAL_OCR,
        ocr=catalogue.resolve(languages),
        embedded_image_ocr=embedded,
    )


def frozen_settings(capture: store.StoredCapture) -> ProcessingSettings | None:
    """The preferences this capture was admitted under, decoded from its own row.

    ``None`` only for a capture admitted before migration 0071, which recorded no
    selection at all; :func:`resolve_settings` documents what that executes as. A
    stored document that is present but does not decode raises, because the one
    thing that must not happen is reading it as the legacy "no selection" case:
    that would reinterpret an unreadable decision as an absent one and run
    something nobody asked for under the key of something they did.
    """
    stored = capture.processing_settings_json
    if stored is None:
        return None
    try:
        return ProcessingSettings.from_value(json.loads(stored))
    except (TypeError, ValueError) as error:
        raise ProcessingSettingsInvalid(
            f"the frozen processing settings of {capture.capture_id} do not decode: "
            f"{error}"
        ) from error


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


def approved_reprocess_profiles(
    *,
    media_type: str,
    settings: ProcessingSettings = DEFAULT_SETTINGS,
    catalogue_for: CatalogueFor | None = None,
) -> tuple[ReprocessProfileOption, ...]:
    """The profile selections Core will accept for a reprocess of this media type.

    A reprocess is an explicit new decision, so it resolves against the *current*
    workspace preferences rather than against the frozen snapshot the original capture
    executed under -- which is why `settings` is a parameter here and the capture row is
    the authority in :func:`process_capture_outbox`. The caller reads the preferences once
    and passes them in, so the options it offers and the option it later executes are one
    resolution. `settings` defaults to the product defaults, which is what an unconfigured
    workspace stores.

    One option for an ordinary document, because Text-only and the default resolve to the
    same processing for it and offering two identical choices would be a false choice. Two
    where the default would actually recognise something -- a PDF or image, or a source
    carrying embedded images the preference applies to -- which is the product's stated
    pair: Text and local OCR as the default, Text only as the explicit alternate. A
    workspace already set to Text-only therefore offers one option, because under those
    preferences the explicit alternate *is* the default. An unqualified format has no
    option at all rather than one that would be refused.

    This is the single place a reprocess profile is resolved. `document.history` reports what
    it returns and `document.reprocess` executes what it returns, so neither can name a
    selection the other would resolve differently.
    """
    references = (
        (DEFAULT_PROFILE_REF, TEXT_ONLY_PROFILE_REF)
        if recognises(settings, media_type)
        else (DEFAULT_PROFILE_REF,)
    )
    options: list[ReprocessProfileOption] = []
    for reference in references:
        resolved = resolve_settings(
            media_type=media_type,
            profile_ref=reference,
            settings=settings,
            catalogue_for=catalogue_for,
        )
        profile = portable_profile(
            media_type=media_type,
            profile_ref=reference,
            revision=APPROVED_PROFILE_REVISION,
            settings=resolved,
        )
        if profile["adapter"] == "unsupported":
            return ()
        options.append(
            ReprocessProfileOption(
                profile_ref=reference,
                revision=APPROVED_PROFILE_REVISION,
                processing_mode=str(profile["processingMode"]),
                profile_digest=store.profile_digest(profile),
                settings=resolved,
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
    catalogue_for: CatalogueFor | None = None,
) -> list[store.StoredResult]:
    """Process the oldest capture obligations, settling each terminal result.

    Each obligation executes from its own capture row's frozen snapshot. Nothing
    process-global decides a mode, a language order or an embedded-image pass, so
    a preference changed or a service restarted between admission and this pass
    reaches neither a queued obligation nor an active one: the row is the same
    row, the profile it resolves to is the same profile, and the operation key
    derived from that profile's digest is the same key -- which is what makes the
    replay after a restart idempotent rather than a second extraction under a
    second identity.
    """
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
        unexecutable = False
        try:
            settings = resolve_settings(
                media_type=capture.media_type,
                profile_ref=capture.processing_profile_ref,
                settings=frozen_settings(capture),
                catalogue_for=catalogue_for,
            )
        except (ProcessingSettingsInvalid, OcrSelectionInvalid):
            # The frozen snapshot cannot be read as a decision, so there is no
            # requested profile to keep: the revision is admitted under the one
            # profile that pins no OCR resources and settles as failed. It is
            # deliberately *not* executed as the legacy no-selection case, which
            # would run the product default under a key nobody was offered.
            unexecutable = True
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
        if unexecutable:
            outcome = _terminal_without_segments("failed", PROCESSING_SETTINGS_INVALID)
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
