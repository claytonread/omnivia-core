"""Phase 2 evidence contracts: sources, items, extractions, links, classification.

Evidence records carry only pointers and provenance metadata (locators,
digests, timestamps) -- never raw content or bytes -- per spec 7.5/11:
a digest or a canonical payload built from these types must never let
protected content leak into a log line or an error message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from omnivia_core.semantic_registry.canonical import content_digest
from omnivia_core.semantic_registry.errors import (
    EvidenceValidationError,
    SemanticErrorCode,
    require,
)
from omnivia_core.semantic_registry.temporal import TemporalInstant

EVIDENCE_SCHEMA_VERSION = "1.0.0"

_DIGEST_PREFIX = "sha256:"
_DIGEST_HEX_LEN = 64


def _require_id(field_name: str, value: str) -> None:
    require(
        isinstance(value, str) and value.strip() != "",
        SemanticErrorCode.MISSING_FIELD,
        f"{field_name} is required",
    )


def _require_text(field_name: str, value: str) -> None:
    require(
        isinstance(value, str) and value.strip() != "",
        SemanticErrorCode.MISSING_FIELD,
        f"{field_name} is required",
    )


def _require_digest(field_name: str, value: str) -> None:
    _require_text(field_name, value)
    ok = (
        value.startswith(_DIGEST_PREFIX)
        and len(value) == len(_DIGEST_PREFIX) + _DIGEST_HEX_LEN
        and value[len(_DIGEST_PREFIX) :] == value[len(_DIGEST_PREFIX) :].lower()
        and all(
            char in "0123456789abcdef" for char in value[len(_DIGEST_PREFIX) :]
        )
    )
    require(
        ok,
        SemanticErrorCode.INVALID_FIELD,
        f"{field_name} must be sha256:<64 lowercase hex>",
    )


def _require_unit_interval(field_name: str, value: float) -> None:
    require(
        isinstance(value, (int, float)) and not isinstance(value, bool),
        SemanticErrorCode.INVALID_FIELD,
        f"{field_name} must be a number",
    )
    require(
        0.0 <= float(value) <= 1.0,
        SemanticErrorCode.INVALID_FIELD,
        f"{field_name} must be between 0 and 1",
    )


class EvidenceSourceKind(str, Enum):
    """What kind of thing an evidence source is."""

    MANUAL = "manual"
    DOCUMENT = "document"
    RECORD = "record"
    EVENT = "event"


class EvidenceLocatorScheme(str, Enum):
    """How an evidence source's `locator` should be interpreted."""

    URN = "urn"
    FILE = "file"
    HTTPS = "https"
    OPAQUE = "opaque"


class Classification(str, Enum):
    """Data classification, ordered least to most restrictive."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


_CLASSIFICATION_RANK = {
    Classification.PUBLIC: 0,
    Classification.INTERNAL: 1,
    Classification.CONFIDENTIAL: 2,
    Classification.RESTRICTED: 3,
}


class EvidenceSupportRole(str, Enum):
    """Whether one linked observation is supported or contradicted by evidence."""

    SUPPORT = "support"
    CONTRADICT = "contradict"


@dataclass(frozen=True, slots=True)
class EvidenceSpan:
    """A half-open `[start_offset, end_offset)` locator into evidence content.

    Carries only offsets/page/section -- never the underlying content itself.
    """

    span_id: str
    start_offset: int
    end_offset: int
    page: int | None = None
    section: str | None = None

    def __post_init__(self) -> None:
        _require_id("span_id", self.span_id)
        require(
            isinstance(self.start_offset, int)
            and not isinstance(self.start_offset, bool)
            and self.start_offset >= 0,
            SemanticErrorCode.INVALID_FIELD,
            "start_offset must be a non-negative integer",
        )
        require(
            isinstance(self.end_offset, int)
            and not isinstance(self.end_offset, bool)
            and self.end_offset >= 0,
            SemanticErrorCode.INVALID_FIELD,
            "end_offset must be a non-negative integer",
        )
        require(
            self.start_offset < self.end_offset,
            SemanticErrorCode.INVALID_FIELD,
            "start_offset must be strictly before end_offset",
        )
        if self.page is not None:
            require(
                isinstance(self.page, int)
                and not isinstance(self.page, bool)
                and self.page >= 0,
                SemanticErrorCode.INVALID_FIELD,
                "page must be a non-negative integer",
            )


def _validate_locator(scheme: EvidenceLocatorScheme, locator: str) -> None:
    if scheme is EvidenceLocatorScheme.URN:
        require(
            locator.startswith("urn:"),
            SemanticErrorCode.INVALID_FIELD,
            "locator must start with 'urn:' for scheme urn",
        )
    elif scheme is EvidenceLocatorScheme.FILE:
        require(
            locator.startswith("file:"),
            SemanticErrorCode.INVALID_FIELD,
            "locator must start with 'file:' for scheme file",
        )
    elif scheme is EvidenceLocatorScheme.HTTPS:
        require(
            locator.startswith("https:"),
            SemanticErrorCode.INVALID_FIELD,
            "locator must start with 'https:' for scheme https",
        )
    elif scheme is EvidenceLocatorScheme.OPAQUE:
        pass
    else:  # pragma: no cover - defensive, unreachable via typed enum
        raise EvidenceValidationError(
            SemanticErrorCode.UNSUPPORTED_VALUE,
            "unsupported locator scheme",
        )


@dataclass(frozen=True, slots=True)
class EvidenceSource:
    """Metadata identifying where an evidence item came from -- locator only."""

    source_id: str
    kind: EvidenceSourceKind
    locator_scheme: EvidenceLocatorScheme
    locator: str
    version: str

    def __post_init__(self) -> None:
        _require_id("source_id", self.source_id)
        require(
            isinstance(self.kind, EvidenceSourceKind),
            SemanticErrorCode.UNSUPPORTED_VALUE,
            "kind must be an EvidenceSourceKind",
        )
        require(
            isinstance(self.locator_scheme, EvidenceLocatorScheme),
            SemanticErrorCode.UNSUPPORTED_VALUE,
            "locator_scheme must be an EvidenceLocatorScheme",
        )
        _require_text("locator", self.locator)
        _validate_locator(self.locator_scheme, self.locator)
        _require_id("version", self.version)


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One piece of evidence: pointers and provenance, never raw content."""

    evidence_id: str
    workspace_id: str
    source: EvidenceSource
    content_ref: str
    content_digest: str
    integrity_digest: str
    mime_type: str
    classification: Classification
    retention_class: str
    captured_at: TemporalInstant
    source_time: TemporalInstant | None = None
    span: EvidenceSpan | None = None
    schema_version: str = field(init=False, default=EVIDENCE_SCHEMA_VERSION)

    def __post_init__(self) -> None:
        _require_id("evidence_id", self.evidence_id)
        _require_id("workspace_id", self.workspace_id)
        require(
            isinstance(self.source, EvidenceSource),
            SemanticErrorCode.INVALID_FIELD,
            "source must be an EvidenceSource",
        )
        _require_text("content_ref", self.content_ref)
        _require_digest("content_digest", self.content_digest)
        _require_digest("integrity_digest", self.integrity_digest)
        _require_text("mime_type", self.mime_type)
        require(
            isinstance(self.classification, Classification),
            SemanticErrorCode.UNSUPPORTED_VALUE,
            "classification must be a Classification",
        )
        _require_id("retention_class", self.retention_class)
        require(
            isinstance(self.captured_at, TemporalInstant),
            SemanticErrorCode.INVALID_FIELD,
            "captured_at must be a TemporalInstant",
        )
        if self.source_time is not None:
            require(
                isinstance(self.source_time, TemporalInstant),
                SemanticErrorCode.INVALID_FIELD,
                "source_time must be a TemporalInstant",
            )
            require(
                self.source_time.value <= self.captured_at.value,
                SemanticErrorCode.TEMPORAL_INTERVAL_INVALID,
                "source_time must not be after captured_at",
            )
        if self.span is not None:
            require(
                isinstance(self.span, EvidenceSpan),
                SemanticErrorCode.INVALID_FIELD,
                "span must be an EvidenceSpan",
            )


@dataclass(frozen=True, slots=True)
class EvidenceExtraction:
    """A worker's extraction run over one evidence item -- metadata only."""

    extraction_id: str
    workspace_id: str
    evidence_id: str
    worker_version: str
    template_version: str
    input_digest: str
    output_digest: str
    confidence: float
    model_version: str | None = None
    raw_completion_ref: str | None = None
    schema_version: str = field(init=False, default=EVIDENCE_SCHEMA_VERSION)

    def __post_init__(self) -> None:
        _require_id("extraction_id", self.extraction_id)
        _require_id("workspace_id", self.workspace_id)
        _require_id("evidence_id", self.evidence_id)
        _require_id("worker_version", self.worker_version)
        _require_id("template_version", self.template_version)
        _require_digest("input_digest", self.input_digest)
        _require_digest("output_digest", self.output_digest)
        _require_unit_interval("confidence", self.confidence)
        if self.model_version is not None:
            _require_text("model_version", self.model_version)
        if self.raw_completion_ref is not None:
            _require_text("raw_completion_ref", self.raw_completion_ref)


@dataclass(frozen=True, slots=True)
class EvidenceLink:
    """One evidence item linked to one observation, with a support role."""

    workspace_id: str
    observation_id: str
    evidence_id: str
    role: EvidenceSupportRole
    span_id: str | None = None
    confidence: float = 1.0

    def __post_init__(self) -> None:
        _require_id("workspace_id", self.workspace_id)
        _require_id("observation_id", self.observation_id)
        _require_id("evidence_id", self.evidence_id)
        require(
            isinstance(self.role, EvidenceSupportRole),
            SemanticErrorCode.UNSUPPORTED_VALUE,
            "role must be an EvidenceSupportRole",
        )
        if self.span_id is not None:
            _require_id("span_id", self.span_id)
        _require_unit_interval("confidence", self.confidence)


def effective_classification(
    workspace_floor: Classification,
    source: Classification,
    evidence: Classification,
    derived: tuple[Classification, ...] = (),
) -> Classification:
    """The most restrictive classification across all inputs."""
    for label, value in (
        ("workspace_floor", workspace_floor),
        ("source", source),
        ("evidence", evidence),
    ):
        require(
            isinstance(value, Classification),
            SemanticErrorCode.UNSUPPORTED_VALUE,
            f"{label} must be a Classification",
        )
    for index, value in enumerate(derived):
        require(
            isinstance(value, Classification),
            SemanticErrorCode.UNSUPPORTED_VALUE,
            f"derived[{index}] must be a Classification",
        )
    candidates = (workspace_floor, source, evidence, *derived)
    return max(candidates, key=lambda c: _CLASSIFICATION_RANK[c])


def _instant_payload(instant: TemporalInstant) -> dict[str, Any]:
    return {
        "value": instant.value.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "precision": instant.precision.value,
        "provenance": instant.provenance.value,
    }


def evidence_item_payload(item: EvidenceItem) -> dict[str, Any]:
    """Canonical, JSON-safe semantic content of `item` -- no raw content."""
    return {
        "evidence_id": item.evidence_id,
        "workspace_id": item.workspace_id,
        "source": {
            "source_id": item.source.source_id,
            "kind": item.source.kind.value,
            "locator_scheme": item.source.locator_scheme.value,
            "locator": item.source.locator,
            "version": item.source.version,
        },
        "content_ref": item.content_ref,
        "content_digest": item.content_digest,
        "integrity_digest": item.integrity_digest,
        "mime_type": item.mime_type,
        "classification": item.classification.value,
        "retention_class": item.retention_class,
        "captured_at": _instant_payload(item.captured_at),
        "source_time": (
            None if item.source_time is None else _instant_payload(item.source_time)
        ),
        "span": (
            None
            if item.span is None
            else {
                "span_id": item.span.span_id,
                "start_offset": item.span.start_offset,
                "end_offset": item.span.end_offset,
                "page": item.span.page,
                "section": item.span.section,
            }
        ),
        "schema_version": item.schema_version,
    }


def evidence_item_digest(item: EvidenceItem) -> str:
    return content_digest(evidence_item_payload(item))


def evidence_extraction_payload(extraction: EvidenceExtraction) -> dict[str, Any]:
    """Canonical, JSON-safe semantic content of `extraction` -- metadata only."""
    return {
        "extraction_id": extraction.extraction_id,
        "workspace_id": extraction.workspace_id,
        "evidence_id": extraction.evidence_id,
        "worker_version": extraction.worker_version,
        "template_version": extraction.template_version,
        "input_digest": extraction.input_digest,
        "output_digest": extraction.output_digest,
        "confidence": extraction.confidence,
        "model_version": extraction.model_version,
        "raw_completion_ref": extraction.raw_completion_ref,
        "schema_version": extraction.schema_version,
    }


def evidence_extraction_digest(extraction: EvidenceExtraction) -> str:
    return content_digest(evidence_extraction_payload(extraction))


def evidence_link_payload(link: EvidenceLink) -> dict[str, Any]:
    """Canonical, JSON-safe semantic content of `link`."""
    return {
        "workspace_id": link.workspace_id,
        "observation_id": link.observation_id,
        "evidence_id": link.evidence_id,
        "role": link.role.value,
        "span_id": link.span_id,
        "confidence": link.confidence,
    }


def evidence_link_digest(link: EvidenceLink) -> str:
    return content_digest(evidence_link_payload(link))


def evidence_dedup_signature(item: EvidenceItem, rule_version: str) -> str:
    """A workspace-scoped, versioned dedup signature for `item`.

    Binds `workspace_id`, `source_id`, source `version`, the item's own
    `content_digest` and `rule_version` -- so dedup never crosses workspaces
    and rule changes do not silently collide old and new signatures.
    """
    _require_id("rule_version", rule_version)
    return content_digest(
        {
            "workspace_id": item.workspace_id,
            "source_id": item.source.source_id,
            "source_version": item.source.version,
            "content_digest": item.content_digest,
            "rule_version": rule_version,
        }
    )


__all__ = [
    "EVIDENCE_SCHEMA_VERSION",
    "Classification",
    "EvidenceExtraction",
    "EvidenceItem",
    "EvidenceLink",
    "EvidenceLocatorScheme",
    "EvidenceSource",
    "EvidenceSourceKind",
    "EvidenceSpan",
    "EvidenceSupportRole",
    "effective_classification",
    "evidence_dedup_signature",
    "evidence_extraction_digest",
    "evidence_extraction_payload",
    "evidence_item_digest",
    "evidence_item_payload",
    "evidence_link_digest",
    "evidence_link_payload",
]
