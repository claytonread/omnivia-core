"""Focused semantic tests for `evidence.capture`'s DTOs (ADR-039, v1.3 section 6.2/6.3).

Covers :func:`~omnivia_core.contracts.v1.semantics_evidence.validate_evidence_capture_input`
and :func:`~omnivia_core.contracts.v1.semantics_evidence.validate_evidence_capture_result`:
the exactly-one-of `text`/`content_base64` rule, the strict base64/UTF-8 decode, the
[1, 1_048_576]-byte bound (checked on the encoded length before any base64 allocation, and
again on the real decoded bytes), the `event_at`/`observed_at` ordering rule, and the result's
`source`/`media_type`/`content_checksum`/`content_length_bytes`/`capture_disposition` shape.

Standard library only, matching the module under test.
"""

from __future__ import annotations

import base64
import dataclasses

import pytest

from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.generated import (
    EvidenceCaptureInput,
    EvidenceCaptureResult,
    SourceReference,
)
from omnivia_core.contracts.v1.semantics_evidence import (
    EVIDENCE_CAPTURE_MAX_CONTENT_BYTES,
    EvidenceCaptureSizeLimitError,
    canonical_timestamp_nanoseconds,
    decode_evidence_capture_input,
    validate_evidence_capture_input,
    validate_evidence_capture_result,
)

VALID_INPUT = EvidenceCaptureInput(
    source_native_id="doc-1",
    media_type="text/plain",
    text="hello",
)

VALID_RESULT = EvidenceCaptureResult(
    evidence_id="ev-1",
    source=SourceReference(kind="direct_submission", source_id="doc-1"),
    media_type="text/plain",
    content_checksum="sha256:" + "a" * 64,
    content_length_bytes=5,
    capture_disposition="created",
)


# --------------------------------------------------------------------------
# Input: text/content_base64 exclusivity and content decoding
# --------------------------------------------------------------------------


def test_raw_unicode_text_is_accepted() -> None:
    validate_evidence_capture_input(
        dataclasses.replace(VALID_INPUT, text="héllo wörld 世界", content_base64=None)
    )


def test_strict_base64_unicode_is_accepted() -> None:
    encoded = base64.b64encode("héllo wörld 世界".encode()).decode("ascii")
    validate_evidence_capture_input(
        dataclasses.replace(VALID_INPUT, text=None, content_base64=encoded)
    )


def test_both_text_and_content_base64_present_is_rejected() -> None:
    with pytest.raises(ContractSemanticError, match="found both"):
        validate_evidence_capture_input(
            dataclasses.replace(VALID_INPUT, text="hello", content_base64="aGVsbG8=")
        )


def test_neither_text_nor_content_base64_present_is_rejected() -> None:
    with pytest.raises(ContractSemanticError, match="found neither"):
        validate_evidence_capture_input(
            dataclasses.replace(VALID_INPUT, text=None, content_base64=None)
        )


def test_empty_content_is_rejected() -> None:
    with pytest.raises(ContractSemanticError, match="outside the bounded range"):
        validate_evidence_capture_input(dataclasses.replace(VALID_INPUT, text=""))


def test_one_mebibyte_text_is_accepted() -> None:
    text = "a" * EVIDENCE_CAPTURE_MAX_CONTENT_BYTES
    validate_evidence_capture_input(dataclasses.replace(VALID_INPUT, text=text))


def test_one_mebibyte_plus_one_text_is_rejected() -> None:
    text = "a" * (EVIDENCE_CAPTURE_MAX_CONTENT_BYTES + 1)
    with pytest.raises(EvidenceCaptureSizeLimitError, match="exceeds the maximum"):
        validate_evidence_capture_input(dataclasses.replace(VALID_INPUT, text=text))


def test_multibyte_text_boundary_is_measured_in_utf8_bytes() -> None:
    at_bound = "é" * (EVIDENCE_CAPTURE_MAX_CONTENT_BYTES // 2)
    validate_evidence_capture_input(dataclasses.replace(VALID_INPUT, text=at_bound))
    with pytest.raises(EvidenceCaptureSizeLimitError, match="exceeds the maximum"):
        validate_evidence_capture_input(
            dataclasses.replace(VALID_INPUT, text=at_bound + "a")
        )


def test_encoded_oversize_content_base64_is_rejected_before_decoding() -> None:
    """An encoded string too long to decode within the byte bound is refused on its
    encoded length alone -- garbage content proves no base64 decode was attempted."""
    oversized = "!" * (4 * ((EVIDENCE_CAPTURE_MAX_CONTENT_BYTES + 2) // 3) + 4)
    with pytest.raises(EvidenceCaptureSizeLimitError, match="exceeds the maximum"):
        validate_evidence_capture_input(
            dataclasses.replace(VALID_INPUT, text=None, content_base64=oversized)
        )


def test_decoded_base64_over_the_boundary_has_the_typed_size_refusal() -> None:
    oversized = base64.b64encode(
        b"a" * (EVIDENCE_CAPTURE_MAX_CONTENT_BYTES + 1)
    ).decode("ascii")
    with pytest.raises(EvidenceCaptureSizeLimitError, match="decoded content length"):
        validate_evidence_capture_input(
            dataclasses.replace(VALID_INPUT, text=None, content_base64=oversized)
        )


def test_malformed_base64_is_rejected() -> None:
    with pytest.raises(ContractSemanticError, match="not strict RFC 4648 base64"):
        validate_evidence_capture_input(
            dataclasses.replace(VALID_INPUT, text=None, content_base64="not-base64!!")
        )


def test_base64_valid_but_not_utf8_is_rejected() -> None:
    encoded = base64.b64encode(b"\xff\xfe\xfd").decode("ascii")
    with pytest.raises(ContractSemanticError, match="does not decode to valid UTF-8"):
        validate_evidence_capture_input(
            dataclasses.replace(VALID_INPUT, text=None, content_base64=encoded)
        )


def test_text_with_an_unpaired_surrogate_raises_contract_semantic_error() -> None:
    """A lone surrogate is a valid Python `str` but has no UTF-8 encoding. This must
    surface as `ContractSemanticError`, never a raw `UnicodeEncodeError`."""
    with pytest.raises(ContractSemanticError, match="not valid Unicode text"):
        validate_evidence_capture_input(
            dataclasses.replace(VALID_INPUT, text="broken \ud800 surrogate")
        )


# --------------------------------------------------------------------------
# Input: media type, structural tolerance, event_at/observed_at ordering
# --------------------------------------------------------------------------


def test_invalid_media_type_is_rejected() -> None:
    with pytest.raises(ContractSemanticError, match="media_type"):
        validate_evidence_capture_input(
            dataclasses.replace(VALID_INPUT, media_type="application/json")
        )


def test_unknown_wire_fields_are_dropped_by_the_tolerant_decoder() -> None:
    """Unknown structured fields are a wire-shape concern the tolerant `from_wire`
    decoder already handles by dropping them; the decoded value that reaches semantic
    validation is unaffected and still passes."""
    decoded = EvidenceCaptureInput.from_wire(
        {
            "source_native_id": "doc-1",
            "media_type": "text/plain",
            "text": "hello",
            "unexpected_future_field": {"nested": True},
        }
    )
    assert "unexpected_future_field" not in decoded.to_wire()
    validate_evidence_capture_input(decoded)


def test_the_operation_decoder_rejects_an_unknown_wire_field() -> None:
    """DTO compatibility stays tolerant while this closed operation stays closed."""
    payload = VALID_INPUT.to_wire()
    payload["unexpected_future_field"] = {"nested": True}
    with pytest.raises(ContractSemanticError, match="closed evidence.capture"):
        decode_evidence_capture_input(payload)


def test_event_at_after_observed_at_is_rejected() -> None:
    with pytest.raises(ContractSemanticError, match="event_at.*is after observed_at"):
        validate_evidence_capture_input(
            dataclasses.replace(
                VALID_INPUT,
                event_at="2024-01-02T00:00:00Z",
                observed_at="2024-01-01T00:00:00Z",
            )
        )


def test_event_at_not_after_observed_at_is_accepted() -> None:
    validate_evidence_capture_input(
        dataclasses.replace(
            VALID_INPUT,
            event_at="2024-01-01T00:00:00Z",
            observed_at="2024-01-01T00:00:00Z",
        )
    )


def test_timestamp_ordering_preserves_all_nine_fractional_digits() -> None:
    validate_evidence_capture_input(
        dataclasses.replace(
            VALID_INPUT,
            event_at="2024-01-01T00:00:00.000000001Z",
            observed_at="2024-01-01T00:00:00.000000002Z",
        )
    )
    with pytest.raises(ContractSemanticError, match="event_at.*is after observed_at"):
        validate_evidence_capture_input(
            dataclasses.replace(
                VALID_INPUT,
                event_at="2024-01-01T00:00:00.000000002Z",
                observed_at="2024-01-01T00:00:00.000000001Z",
            )
        )
    assert canonical_timestamp_nanoseconds("1969-12-31T23:59:59.999999999Z") == -1


# --------------------------------------------------------------------------
# Result
# --------------------------------------------------------------------------


def test_valid_result_is_accepted() -> None:
    validate_evidence_capture_result(VALID_RESULT)


def test_result_source_kind_other_than_direct_submission_is_rejected() -> None:
    with pytest.raises(ContractSemanticError, match="direct_submission"):
        validate_evidence_capture_result(
            dataclasses.replace(
                VALID_RESULT,
                source=SourceReference(kind="document", source_id="doc-1"),
            )
        )


def test_result_source_with_a_locator_is_rejected() -> None:
    with pytest.raises(ContractSemanticError, match="source.locator must be absent"):
        validate_evidence_capture_result(
            dataclasses.replace(
                VALID_RESULT,
                source=SourceReference(
                    kind="direct_submission",
                    source_id="doc-1",
                    locator="https://example.test",
                ),
            )
        )


def test_result_source_with_retrieved_at_is_rejected() -> None:
    with pytest.raises(
        ContractSemanticError, match="source.retrieved_at must be absent"
    ):
        validate_evidence_capture_result(
            dataclasses.replace(
                VALID_RESULT,
                source=SourceReference(
                    kind="direct_submission",
                    source_id="doc-1",
                    retrieved_at="2024-01-01T00:00:00Z",
                ),
            )
        )


def test_result_media_type_outside_the_allowlist_is_rejected() -> None:
    with pytest.raises(ContractSemanticError, match="media_type"):
        validate_evidence_capture_result(
            dataclasses.replace(VALID_RESULT, media_type="application/json")
        )


def test_result_checksum_not_a_sha256_digest_is_rejected() -> None:
    with pytest.raises(ContractSemanticError, match="sha256:<64 hex digits>"):
        validate_evidence_capture_result(
            dataclasses.replace(VALID_RESULT, content_checksum="md5:" + "a" * 32)
        )


def test_result_checksum_with_uppercase_hex_is_rejected() -> None:
    with pytest.raises(ContractSemanticError, match="sha256:<64 hex digits>"):
        validate_evidence_capture_result(
            dataclasses.replace(VALID_RESULT, content_checksum="sha256:" + "A" * 64)
        )


def test_result_content_length_bytes_zero_is_rejected() -> None:
    with pytest.raises(ContractSemanticError, match="outside the bounded range"):
        validate_evidence_capture_result(
            dataclasses.replace(VALID_RESULT, content_length_bytes=0)
        )


def test_result_content_length_bytes_over_the_maximum_is_rejected() -> None:
    with pytest.raises(ContractSemanticError, match="outside the bounded range"):
        validate_evidence_capture_result(
            dataclasses.replace(
                VALID_RESULT,
                content_length_bytes=EVIDENCE_CAPTURE_MAX_CONTENT_BYTES + 1,
            )
        )


def test_result_capture_disposition_outside_the_closed_vocabulary_is_rejected() -> None:
    with pytest.raises(ContractSemanticError, match="capture_disposition"):
        validate_evidence_capture_result(
            dataclasses.replace(VALID_RESULT, capture_disposition="overwritten")
        )


def test_result_capture_disposition_already_captured_is_accepted() -> None:
    validate_evidence_capture_result(
        dataclasses.replace(VALID_RESULT, capture_disposition="already_captured")
    )
