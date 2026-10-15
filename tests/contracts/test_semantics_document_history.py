"""Focused semantic tests for the `document.history` profile summary (ADR-039, v1.3).

The whole module under test is
:func:`~omnivia_core.contracts.v1.semantics_evidence.validate_document_history_result`, and
the runtime's own vertical suite exercises it against real pages. What is tested here is the
one bound that no page Core currently assembles can reach, and which therefore has to be
tested directly: a recognition-language selection holds **at most three** languages, and a
summary naming a fourth describes a selection the installation would itself have refused.

Standard library only, matching the module under test.
"""

from __future__ import annotations

import dataclasses

import pytest

from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.generated import (
    DocumentExtractionHeadState,
    DocumentExtractionHistoryEntry,
    DocumentExtractionProfileSummary,
    DocumentHistoryAction,
    DocumentHistoryInput,
    DocumentHistoryResult,
    DocumentSourceHeadState,
    PageMetadata,
)
from omnivia_core.contracts.v1.semantics_evidence import (
    validate_document_history_result,
)

DOCUMENT = "doc-1"
EXTRACTION = "ext-1"

#: Three is the maximum, so this is the largest selection that is still a selection.
THREE_LANGUAGES = ("eng", "deu", "fra")

PROFILE = DocumentExtractionProfileSummary(
    digest="sha256:" + "a" * 64,
    decoder_build="document-default:1:pillow-image-ocr-2",
    processing_mode="text_and_local_ocr",
    embedded_image_ocr=False,
    ocr_languages=THREE_LANGUAGES,
)

EXTRACTION_ENTRY = DocumentExtractionHistoryEntry(
    extraction_revision_id=EXTRACTION,
    capture_id="cap-1",
    document_ordinal=1,
    extraction_sequence=1,
    profile=PROFILE,
    expected_base_extraction_id=None,
    head_eligible=True,
    is_current_extraction=True,
    result_state="succeeded",
    publication_disposition="promoted",
    output_digest="sha256:" + "b" * 64,
    worker_build="core-portable-text-1",
    admitted_at="2026-10-15T00:00:00Z",
    completed_at="2026-10-15T00:00:01Z",
)

RESULT = DocumentHistoryResult(
    document_id=DOCUMENT,
    created_at="2026-10-15T00:00:00Z",
    source=DocumentSourceHeadState(
        source_state="active",
        source_removal_generation=1,
        current_capture_id=None,
        capture_head_sequence=0,
        original_binding_state="connected",
        updated_at="2026-10-15T00:00:00Z",
    ),
    extraction=DocumentExtractionHeadState(
        current_extraction_id=EXTRACTION,
        extraction_head_ordinal=1,
        retrieval_state="retrievable",
        published_at="2026-10-15T00:00:02Z",
    ),
    captures=(),
    capture_count=0,
    extractions=(EXTRACTION_ENTRY,),
    extraction_count=1,
    actions=(
        DocumentHistoryAction(
            action="refresh_original", available=True, unavailable_reason=None
        ),
    ),
    page=PageMetadata(continuation_token=None),
)

REQUEST = DocumentHistoryInput(document_id=DOCUMENT, limit=None, page=None)


def _with_languages(*languages: str) -> DocumentHistoryResult:
    return dataclasses.replace(
        RESULT,
        extractions=(
            dataclasses.replace(
                EXTRACTION_ENTRY,
                profile=dataclasses.replace(PROFILE, ocr_languages=languages),
            ),
        ),
    )


def test_the_largest_valid_language_selection_is_three() -> None:
    validate_document_history_result(_with_languages(*THREE_LANGUAGES), REQUEST, DOCUMENT)


def test_a_fourth_recognition_language_is_rejected() -> None:
    with pytest.raises(ContractSemanticError, match="ocr_languages"):
        validate_document_history_result(
            _with_languages("eng", "deu", "fra", "spa"), REQUEST, DOCUMENT
        )


def test_a_text_only_profile_naming_any_language_is_rejected() -> None:
    """A text-only profile ran no recognition, so naming a language claims one that did not."""
    with pytest.raises(ContractSemanticError, match="text-only profile"):
        validate_document_history_result(
            dataclasses.replace(
                RESULT,
                extractions=(
                    dataclasses.replace(
                        EXTRACTION_ENTRY,
                        profile=dataclasses.replace(
                            PROFILE,
                            processing_mode="text_only",
                            ocr_languages=("eng",),
                        ),
                    ),
                ),
            ),
            REQUEST,
            DOCUMENT,
        )


def test_an_empty_language_selection_is_accepted() -> None:
    validate_document_history_result(_with_languages(), REQUEST, DOCUMENT)
