"""Phase 2 evidence domain contract tests (spec section 10.5; decision record section 1-3)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from omnivia_core.semantic_registry.errors import (
    SemanticErrorCode,
    SemanticValidationError,
)
from omnivia_core.semantic_registry.evidence import (
    Classification,
    EvidenceExtraction,
    EvidenceItem,
    EvidenceLink,
    EvidenceLocatorScheme,
    EvidenceSource,
    EvidenceSourceKind,
    EvidenceSpan,
    EvidenceSupportRole,
    effective_classification,
    evidence_dedup_signature,
    evidence_extraction_digest,
    evidence_extraction_payload,
    evidence_item_digest,
    evidence_item_payload,
    evidence_link_digest,
    evidence_link_payload,
)
from omnivia_core.semantic_registry.temporal import (
    TemporalInstant,
    TemporalPrecision,
    TemporalProvenance,
)

_VALID_DIGEST = "sha256:" + "a1b2c3d4e5f6" + "0" * 52
assert len(_VALID_DIGEST) == len("sha256:") + 64


def _instant(
    value: datetime, provenance: TemporalProvenance = TemporalProvenance.STATED
) -> TemporalInstant:
    return TemporalInstant(
        value=value, precision=TemporalPrecision.SECOND, provenance=provenance
    )


def _source(source_id: str = "src1") -> EvidenceSource:
    return EvidenceSource(
        source_id=source_id,
        kind=EvidenceSourceKind.DOCUMENT,
        locator_scheme=EvidenceLocatorScheme.HTTPS,
        locator="https://example.test/doc",
        version="v1",
    )


def _item(
    content_ref: str = "content-ref-1",
    content_digest: str = _VALID_DIGEST,
    evidence_id: str = "ev1",
    workspace_id: str = "ws1",
    source: EvidenceSource | None = None,
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        workspace_id=workspace_id,
        source=source or _source(),
        content_ref=content_ref,
        content_digest=content_digest,
        integrity_digest=_VALID_DIGEST,
        mime_type="text/plain",
        classification=Classification.INTERNAL,
        retention_class="standard",
        captured_at=_instant(datetime(2026, 1, 1, tzinfo=UTC)),
    )


# --- Regression 1: digest format -------------------------------------------------


def test_valid_sha256_digest_is_accepted() -> None:
    item = _item(content_digest=_VALID_DIGEST)
    assert item.content_digest == _VALID_DIGEST


def test_valid_all_digit_sha256_digest_is_accepted() -> None:
    digest = "sha256:" + "1" * 64
    item = _item(content_digest=digest)
    assert item.content_digest == digest


@pytest.mark.parametrize(
    "bad_digest",
    [
        "sha256:" + "A" * 64,  # uppercase hex
        "sha256:" + "g" * 64,  # non-hex characters
        "sha256:" + "a" * 63,  # too short
        "sha256:" + "a" * 65,  # too long
        "a" * 64,  # missing prefix
    ],
)
def test_invalid_sha256_digests_are_rejected(bad_digest: str) -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        _item(content_digest=bad_digest)
    assert excinfo.value.code is SemanticErrorCode.INVALID_FIELD


def test_empty_digest_is_rejected_as_missing() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        _item(content_digest="")
    assert excinfo.value.code is SemanticErrorCode.MISSING_FIELD


def test_integrity_digest_uses_same_validation_rule() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        EvidenceItem(
            evidence_id="ev1",
            workspace_id="ws1",
            source=_source(),
            content_ref="ref",
            content_digest=_VALID_DIGEST,
            integrity_digest="sha256:" + "Z" * 64,
            mime_type="text/plain",
            classification=Classification.INTERNAL,
            retention_class="standard",
            captured_at=_instant(datetime(2026, 1, 1, tzinfo=UTC)),
        )
    assert excinfo.value.code is SemanticErrorCode.INVALID_FIELD


# --- Regression 2: content_ref changes the digest --------------------------------


def test_evidence_item_digest_changes_when_content_ref_changes() -> None:
    item_a = _item(content_ref="content-ref-a")
    item_b = _item(content_ref="content-ref-b")
    assert evidence_item_digest(item_a) != evidence_item_digest(item_b)


def test_evidence_item_digest_is_deterministic_for_identical_content() -> None:
    item_a = _item()
    item_b = _item()
    assert evidence_item_digest(item_a) == evidence_item_digest(item_b)
    assert evidence_item_digest(item_a) == evidence_item_digest(item_a)


def test_evidence_item_digest_changes_when_content_digest_changes() -> None:
    other_digest = "sha256:" + "b" * 64
    item_a = _item(content_digest=_VALID_DIGEST)
    item_b = _item(content_digest=other_digest)
    assert evidence_item_digest(item_a) != evidence_item_digest(item_b)


# --- Regression 3: classification cannot be weaker than inherited requirements ---


def test_effective_classification_cannot_be_weaker_than_workspace_floor() -> None:
    result = effective_classification(
        workspace_floor=Classification.CONFIDENTIAL,
        source=Classification.PUBLIC,
        evidence=Classification.PUBLIC,
    )
    assert result is Classification.CONFIDENTIAL


def test_effective_classification_cannot_be_weaker_than_source() -> None:
    result = effective_classification(
        workspace_floor=Classification.PUBLIC,
        source=Classification.RESTRICTED,
        evidence=Classification.PUBLIC,
    )
    assert result is Classification.RESTRICTED


def test_effective_classification_cannot_be_weaker_than_derived_inputs() -> None:
    result = effective_classification(
        workspace_floor=Classification.PUBLIC,
        source=Classification.PUBLIC,
        evidence=Classification.PUBLIC,
        derived=(Classification.INTERNAL, Classification.CONFIDENTIAL),
    )
    assert result is Classification.CONFIDENTIAL


def test_effective_classification_is_public_only_when_every_input_is_public() -> None:
    result = effective_classification(
        workspace_floor=Classification.PUBLIC,
        source=Classification.PUBLIC,
        evidence=Classification.PUBLIC,
    )
    assert result is Classification.PUBLIC


def test_effective_classification_rejects_non_classification_input() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        effective_classification(
            workspace_floor="internal",  # type: ignore[arg-type]
            source=Classification.PUBLIC,
            evidence=Classification.PUBLIC,
        )
    assert excinfo.value.code is SemanticErrorCode.UNSUPPORTED_VALUE


# --- Regression 4: no raw content/completion text in event-safe payloads --------


def test_evidence_extraction_has_no_raw_content_field() -> None:
    field_names = {f.name for f in dataclasses.fields(EvidenceExtraction)}
    assert "raw_completion_ref" in field_names
    forbidden = {"raw_completion", "raw_output", "completion_text", "output_text"}
    assert field_names.isdisjoint(forbidden)


def test_evidence_extraction_payload_only_carries_the_completion_reference() -> None:
    extraction = EvidenceExtraction(
        extraction_id="ext1",
        workspace_id="ws1",
        evidence_id="ev1",
        worker_version="w1",
        template_version="t1",
        input_digest=_VALID_DIGEST,
        output_digest=_VALID_DIGEST,
        confidence=0.9,
        raw_completion_ref="urn:blob:completion-1",
    )
    payload = evidence_extraction_payload(extraction)
    assert payload["raw_completion_ref"] == "urn:blob:completion-1"
    for key in payload:
        assert "text" not in key and "content" not in key


def test_evidence_item_payload_never_embeds_protected_content() -> None:
    item = _item()
    payload = evidence_item_payload(item)
    # only pointers/digests, never the referenced content itself
    assert payload["content_digest"] == item.content_digest
    for value in payload.values():
        if isinstance(value, str):
            assert value != "protected content bytes"
    assert "content" not in payload
    assert "raw_content" not in payload
    assert "bytes" not in payload


def test_evidence_link_payload_never_embeds_content() -> None:
    link = EvidenceLink(
        workspace_id="ws1",
        observation_id="obs1",
        evidence_id="ev1",
        role=EvidenceSupportRole.SUPPORT,
    )
    payload = evidence_link_payload(link)
    assert set(payload) == {
        "workspace_id",
        "observation_id",
        "evidence_id",
        "role",
        "span_id",
        "confidence",
    }


# --- Source / item / span / extraction / link invariants ------------------------


def test_evidence_span_requires_start_before_end() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        EvidenceSpan(span_id="s1", start_offset=10, end_offset=5)
    assert excinfo.value.code is SemanticErrorCode.INVALID_FIELD


def test_evidence_span_rejects_negative_offsets() -> None:
    with pytest.raises(SemanticValidationError):
        EvidenceSpan(span_id="s1", start_offset=-1, end_offset=5)


def test_evidence_span_accepts_valid_bounds() -> None:
    span = EvidenceSpan(span_id="s1", start_offset=0, end_offset=10, page=1)
    assert span.start_offset == 0


@pytest.mark.parametrize(
    "scheme,locator",
    [
        (EvidenceLocatorScheme.URN, "not-a-urn"),
        (EvidenceLocatorScheme.FILE, "https://example.test"),
        (EvidenceLocatorScheme.HTTPS, "file:///etc/passwd"),
    ],
)
def test_evidence_source_rejects_locator_scheme_mismatch(
    scheme: EvidenceLocatorScheme, locator: str
) -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        EvidenceSource(
            source_id="src1", kind=EvidenceSourceKind.DOCUMENT, locator_scheme=scheme, locator=locator, version="v1"
        )
    assert excinfo.value.code is SemanticErrorCode.INVALID_FIELD


def test_evidence_source_opaque_locator_accepts_anything() -> None:
    source = EvidenceSource(
        source_id="src1",
        kind=EvidenceSourceKind.MANUAL,
        locator_scheme=EvidenceLocatorScheme.OPAQUE,
        locator="anything-goes",
        version="v1",
    )
    assert source.locator == "anything-goes"


def test_evidence_item_rejects_source_time_after_captured_at() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        EvidenceItem(
            evidence_id="ev1",
            workspace_id="ws1",
            source=_source(),
            content_ref="ref",
            content_digest=_VALID_DIGEST,
            integrity_digest=_VALID_DIGEST,
            mime_type="text/plain",
            classification=Classification.INTERNAL,
            retention_class="standard",
            captured_at=_instant(datetime(2026, 1, 1, tzinfo=UTC)),
            source_time=_instant(datetime(2026, 1, 2, tzinfo=UTC)),
        )
    assert excinfo.value.code is SemanticErrorCode.TEMPORAL_INTERVAL_INVALID


def test_evidence_item_accepts_source_time_before_captured_at() -> None:
    item = EvidenceItem(
        evidence_id="ev1",
        workspace_id="ws1",
        source=_source(),
        content_ref="ref",
        content_digest=_VALID_DIGEST,
        integrity_digest=_VALID_DIGEST,
        mime_type="text/plain",
        classification=Classification.INTERNAL,
        retention_class="standard",
        captured_at=_instant(datetime(2026, 1, 2, tzinfo=UTC)),
        source_time=_instant(datetime(2026, 1, 1, tzinfo=UTC)),
    )
    assert item.source_time is not None


def test_evidence_extraction_requires_confidence_in_unit_interval() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        EvidenceExtraction(
            extraction_id="ext1",
            workspace_id="ws1",
            evidence_id="ev1",
            worker_version="w1",
            template_version="t1",
            input_digest=_VALID_DIGEST,
            output_digest=_VALID_DIGEST,
            confidence=1.5,
        )
    assert excinfo.value.code is SemanticErrorCode.INVALID_FIELD


def test_evidence_link_requires_confidence_in_unit_interval() -> None:
    with pytest.raises(SemanticValidationError):
        EvidenceLink(
            workspace_id="ws1",
            observation_id="obs1",
            evidence_id="ev1",
            role=EvidenceSupportRole.SUPPORT,
            confidence=-0.1,
        )


def test_evidence_link_rejects_non_enum_role() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        EvidenceLink(
            workspace_id="ws1",
            observation_id="obs1",
            evidence_id="ev1",
            role="support",  # type: ignore[arg-type]
        )
    assert excinfo.value.code is SemanticErrorCode.UNSUPPORTED_VALUE


# --- Deterministic payloads / digests --------------------------------------------


def test_evidence_extraction_digest_is_deterministic() -> None:
    extraction = EvidenceExtraction(
        extraction_id="ext1",
        workspace_id="ws1",
        evidence_id="ev1",
        worker_version="w1",
        template_version="t1",
        input_digest=_VALID_DIGEST,
        output_digest=_VALID_DIGEST,
        confidence=0.5,
    )
    assert evidence_extraction_digest(extraction) == evidence_extraction_digest(
        extraction
    )


def test_evidence_extraction_digest_changes_with_output_digest() -> None:
    other = "sha256:" + "c" * 64
    base = EvidenceExtraction(
        extraction_id="ext1",
        workspace_id="ws1",
        evidence_id="ev1",
        worker_version="w1",
        template_version="t1",
        input_digest=_VALID_DIGEST,
        output_digest=_VALID_DIGEST,
        confidence=0.5,
    )
    changed = dataclasses.replace(base, output_digest=other)
    assert evidence_extraction_digest(base) != evidence_extraction_digest(changed)


def test_evidence_link_digest_changes_with_role() -> None:
    support = EvidenceLink(
        workspace_id="ws1",
        observation_id="obs1",
        evidence_id="ev1",
        role=EvidenceSupportRole.SUPPORT,
    )
    contradict = dataclasses.replace(support, role=EvidenceSupportRole.CONTRADICT)
    assert evidence_link_digest(support) != evidence_link_digest(contradict)


def test_evidence_item_payload_round_trips_span() -> None:
    item = dataclasses.replace(
        _item(), span=EvidenceSpan(span_id="s1", start_offset=0, end_offset=5)
    )
    payload = evidence_item_payload(item)
    assert payload["span"] == {
        "span_id": "s1",
        "start_offset": 0,
        "end_offset": 5,
        "page": None,
        "section": None,
    }


# --- Deduplication signature -----------------------------------------------------


def test_evidence_dedup_signature_is_workspace_scoped() -> None:
    item_ws1 = _item(workspace_id="ws1")
    item_ws2 = _item(workspace_id="ws2")
    sig1 = evidence_dedup_signature(item_ws1, rule_version="rule-v1")
    sig2 = evidence_dedup_signature(item_ws2, rule_version="rule-v1")
    assert sig1 != sig2


def test_evidence_dedup_signature_is_identical_for_identical_content_same_workspace() -> (
    None
):
    item_a = _item(workspace_id="ws1", evidence_id="ev-a")
    item_b = _item(workspace_id="ws1", evidence_id="ev-b")
    sig_a = evidence_dedup_signature(item_a, rule_version="rule-v1")
    sig_b = evidence_dedup_signature(item_b, rule_version="rule-v1")
    assert sig_a == sig_b


def test_evidence_dedup_signature_changes_with_rule_version() -> None:
    item = _item()
    sig1 = evidence_dedup_signature(item, rule_version="rule-v1")
    sig2 = evidence_dedup_signature(item, rule_version="rule-v2")
    assert sig1 != sig2


def test_evidence_dedup_signature_changes_with_source_version() -> None:
    item_v1 = _item(source=_source())
    item_v2 = _item(
        source=EvidenceSource(
            source_id="src1",
            kind=EvidenceSourceKind.DOCUMENT,
            locator_scheme=EvidenceLocatorScheme.HTTPS,
            locator="https://example.test/doc",
            version="v2",
        )
    )
    sig1 = evidence_dedup_signature(item_v1, rule_version="rule-v1")
    sig2 = evidence_dedup_signature(item_v2, rule_version="rule-v1")
    assert sig1 != sig2


def test_evidence_dedup_signature_requires_rule_version() -> None:
    with pytest.raises(SemanticValidationError) as excinfo:
        evidence_dedup_signature(_item(), rule_version="")
    assert excinfo.value.code is SemanticErrorCode.MISSING_FIELD
