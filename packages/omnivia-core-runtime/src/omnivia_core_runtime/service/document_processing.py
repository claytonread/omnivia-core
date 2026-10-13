"""Bounded processing of durable document-capture outbox obligations."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.service import document_intake
from omnivia_core_runtime.service.document_extractors import (
    DocumentExtractionInvalid,
    DocumentExtractionUnsupported,
    extract_document,
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
TESSERACT_MODEL_HASHES = [
    "sha256:7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2",
    "sha256:9cf5d576fcc47564f11265841e5ca839001e7e6f38ff7f7aacf46d15a96b00ff",
]


@dataclass
class DocumentProcessingExecutor:
    """Rate-limited service-owned document processing.

    The service tick calls this frequently. The executor keeps each pass bounded and
    shares the runner's SQLite gate with request transports; extraction itself runs
    outside the gate so a large document cannot monopolise request admission.
    """

    runner: ServiceRunner
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
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
            return process_capture_outbox(self.runner, limit=budget)
        except sqlite3.Error:
            # SQLite contention and lost ownership leave the durable outbox intact.
            # A later bounded pass replays the same operation key.
            return []


def portable_profile(
    *, media_type: str, profile_ref: str, revision: str
) -> dict[str, Any]:
    """The immutable profile for the portable direct-text worker."""
    adapter = {
        "text/plain": "plain-text",
        "text/markdown": "markdown-text",
        "text/csv": "delimited-text",
        "text/tab-separated-values": "delimited-text",
        "text/html": "inert-html",
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
    uses_ocr = media_type in OCR_MEDIA_TYPES
    return {
        "schema": store.PROFILE_SCHEMA,
        "adapter": adapter,
        "adapterVersion": "1",
        "decoderBuild": f"{profile_ref}:{revision}",
        "processingMode": "text_and_local_ocr" if uses_ocr else "text_only",
        "ocr": {
            "engine": "tesseract" if uses_ocr else "none",
            "engineRevision": "5.5.3" if uses_ocr else "none",
            "modelHashes": TESSERACT_MODEL_HASHES if uses_ocr else [],
            "languages": ["eng"] if uses_ocr else [],
        },
        "embeddedImageOcr": False,
        "revisionView": "current_text_with_separate_annotations",
        "secondaryContentPolicy": "preserve_role_and_visibility",
        "formulaPolicy": "expression_and_saved_cache_without_calculation",
        "html": {"encoding": "utf-8"},
        "delimited": {
            "encoding": "bom_or_utf-8",
            "delimiter": "media_default",
            "quote": '"',
        },
        "limits": {
            "sourceBytes": 50 * 1024 * 1024,
            "decodedPixels": 1,
            "containerEntries": 10_000,
            "expandedBytes": 512 * 1024 * 1024,
            "outputTextBytes": 32 * 1024 * 1024,
            "outputStructuredBytes": 32 * 1024 * 1024,
            "memoryBytes": 256 * 1024 * 1024,
            "timeMs": 30_000,
            "temporaryBytes": 64 * 1024 * 1024,
        },
        "readingOrderVersion": "source-order-1",
        "deduplicationVersion": "none-1",
        "parserFeatures": sorted(
            [
                "direct_text",
                "inert_references",
                "located_segments",
                *(["local_ocr", "pdf_raster"] if uses_ocr else []),
            ]
        ),
        "unknownFeaturePolicy": "partial",
        "completenessPolicy": "explicit_unit_coverage",
    }


def process_capture_outbox(
    runner: ServiceRunner, *, limit: int = 16
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
        profile = portable_profile(
            media_type=capture.media_type,
            profile_ref=capture.processing_profile_ref,
            revision=capture.processing_profile_revision,
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
        try:
            with runner.sqlite_gate:
                content = document_intake.read_retained_source(
                    runner, capture_id=capture.capture_id
                )
            outcome = extract_document(
                content, media_type=capture.media_type, capture_id=capture.capture_id
            )
        except DocumentExtractionUnsupported:
            outcome = _terminal_without_segments("unsupported", "format_not_qualified")
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
