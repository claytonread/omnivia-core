"""Pure semantic validation for the L0 evidence DTOs (`evidence.search`, ADR-039).

Structural decoding lives in :mod:`generated`; this module is the same kind of pure,
standard-library-only layer :mod:`semantics` is for the workspace/governed-memory DTOs,
split out here for its own domain-boundary clarity: an `EvidenceArtifact` is raw L0
material, never governed knowledge, so its invariants are kept separate from
:mod:`semantics`'s `GovernedRecord` invariants rather than folded into that module.
Nothing here parses or produces JSON, and nothing here may depend on runtime, storage,
HTTP, MCP, CLI, Platform, Dev, or a validation framework.

Every public function in this module is a *direct* entry point: a caller may reach it
without going through a tolerant `from_wire` decoder first (a hand-built dataclass, a
value threaded through from another layer, or simply a caller mistake). None of them may
therefore ever raise a raw `TypeError`/`AttributeError` from an unguarded `len()`, regex
match, iteration, or attribute access on a wrongly-typed argument: every such misuse is
caught by an explicit type check up front and reraised as `ContractSemanticError`.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Final

from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.generated import (
    EVIDENCE_CHECKSUM_PATTERN,
    EVIDENCE_ID_PATTERN,
    IDENTIFIER_PATTERN,
    MEDIA_TYPE_PATTERN,
    OPAQUE_TOKEN_PATTERN,
    OPEN_CODE_PATTERN,
    TIMESTAMP_PATTERN,
    WORKSPACE_ID_PATTERN,
    DocumentCaptureHistoryEntry,
    DocumentExtractionHeadState,
    DocumentExtractionHistoryEntry,
    DocumentExtractionProfileSummary,
    DocumentHistoryAction,
    DocumentHistoryInput,
    DocumentHistoryResult,
    DocumentProcessingProfileInput,
    DocumentReprocessProfileOption,
    DocumentSourceHeadState,
    EvidenceArtifact,
    EvidenceCaptureInput,
    EvidenceCaptureResult,
    EvidenceReference,
    EvidenceSearchInput,
    EvidenceSearchResult,
    PageMetadata,
    ProvenanceEntry,
    SourceReference,
    SourceSpan,
)
from omnivia_core.contracts.v1.semantics import (
    validate_page_limit,
    validate_record_temporal_metadata,
)

__all__ = [
    "DOCUMENT_BINDING_STATES",
    "DOCUMENT_CAPTURE_ASSOCIATION_KINDS",
    "DOCUMENT_CAPTURE_DISPOSITIONS",
    "DOCUMENT_CAPTURE_DISPOSITION_REASONS",
    "DOCUMENT_CAPTURE_INTENT_KINDS",
    "DOCUMENT_CAPTURE_PROCESSING_STATES",
    "DOCUMENT_CAPTURE_RETENTION_STATES",
    "DOCUMENT_EXTRACTION_PUBLISHABLE_STATES",
    "DOCUMENT_EXTRACTION_RESULT_STATES",
    "DOCUMENT_HISTORY_ACTIONS",
    "DOCUMENT_HISTORY_MAX_PAGE",
    "DOCUMENT_HISTORY_UNAVAILABLE_REASONS",
    "DOCUMENT_PROCESSING_MODES",
    "DOCUMENT_PUBLICATION_DISPOSITIONS",
    "DOCUMENT_RETRIEVAL_STATES",
    "DOCUMENT_SOURCE_STATES",
    "EVIDENCE_CAPTURE_ALLOWED_MEDIA_TYPES",
    "EVIDENCE_CAPTURE_MAX_CONTENT_BYTES",
    "EVIDENCE_CAPTURE_SOURCE_KIND",
    "EVIDENCE_TOMBSTONE_ACTION",
    "EvidenceCaptureSizeLimitError",
    "canonical_timestamp_nanoseconds",
    "decode_document_history_input",
    "decode_evidence_capture_input",
    "decode_evidence_search_input",
    "validate_document_history_input",
    "validate_document_history_result",
    "validate_evidence_artifact",
    "validate_evidence_capture_input",
    "validate_evidence_capture_result",
    "validate_evidence_search_input",
    "validate_evidence_search_result",
]

_BOUNDED_VALUE_MAX_LENGTH: Final = 128
_OPAQUE_TOKEN_MAX_LENGTH: Final = 512
_EVIDENCE_CHECKSUM_MAX_LENGTH: Final = 256
_MEDIA_TYPE_MAX_LENGTH: Final = 255
_LOCATOR_MAX_LENGTH: Final = 2048
_EXCERPT_MAX_LENGTH: Final = 4096
_QUERY_MAX_LENGTH: Final = 4096
_REASON_COMMENT_MAX_LENGTH: Final = 2048
_DEFAULT_RESULT_LIMIT: Final = 500
"""`EvidenceSearchResult.evidence`'s schema `maxItems`: the ceiling applied when the
request carries no explicit `limit`."""

_PERMISSION_LABELS_MAX_ITEMS: Final = 64
_PROVENANCE_HISTORY_MAX_ITEMS: Final = 1024
_PROVENANCE_ENTRY_EVIDENCE_MAX_ITEMS: Final = 256
"""Every cardinality above is restated from the JSON Schema rather than delegated to it,
for the reason every length bound in this module already is: the tolerant generated decoder
applies no `maxItems`, so an artifact carrying 2000 provenance entries would otherwise
decode, pass semantic validation, and fail only against strict JSON Schema -- and a Context
Pack selecting it would have been validated against a bar the schema does not agree with.

`_PROVENANCE_ENTRY_EVIDENCE_MAX_ITEMS` is the same 256 both evidence arrays declare
(`ProvenanceEntry.evidence` and `CandidateAssertion.evidence`), matching
:mod:`~omnivia_core.contracts.v1.semantics`'s bound on the governed-record side: one number
for one wire shape, wherever that shape is reached from."""

EVIDENCE_TOMBSTONE_ACTION: Final = "tombstoned"
"""The one `ProvenanceEntry.action` value that counts as recording a tombstoning act. A
generic `created`/`modified` entry is not evidence that a tombstone was itself audited."""

EVIDENCE_CAPTURE_ALLOWED_MEDIA_TYPES: Final = frozenset({"text/plain", "text/markdown"})
"""The exact `evidence.capture` media allowlist (v1.3 section 6.2). No media-type
parameter such as `charset=` is admitted: the value must match one of these two literal
strings exactly, not merely start with one."""

EVIDENCE_CAPTURE_MAX_CONTENT_BYTES: Final = 1_048_576
"""The decoded-byte ceiling `evidence.capture` content must fall within (v1.3 section
6.2), inclusive at both ends: `content_length_bytes` in `[1, 1_048_576]`."""

EVIDENCE_CAPTURE_SOURCE_KIND: Final = "direct_submission"
"""The one `SourceReference.kind` a captured evidence artifact's persisted source may
carry (v1.3 section 6.3/6.4). `evidence.capture` never produces any other source kind."""

_EVIDENCE_CAPTURE_DISPOSITIONS: Final = frozenset({"created", "already_captured"})
"""The closed `capture_disposition` vocabulary (v1.3 section 6.3). Unlike the open codes
elsewhere in this module, this result field is not forward-open: a caller cannot decide
what to do with a disposition it has never seen, so an unrecognized value is rejected
rather than preserved."""

_EVIDENCE_CAPTURE_MAX_BASE64_LENGTH: Final = 4 * (
    (EVIDENCE_CAPTURE_MAX_CONTENT_BYTES + 2) // 3
)
"""The maximum `content_base64` string length that could possibly decode to no more than
:data:`EVIDENCE_CAPTURE_MAX_CONTENT_BYTES` bytes. Checked before any base64 decode is
attempted, so an oversized encoded payload is rejected on its encoded length alone rather
than after allocating the decoded buffer -- the encoded-size bound v1.3 section 6.2
requires ahead of unbounded base64 allocation."""

_EVIDENCE_ID_RE: Final = re.compile(EVIDENCE_ID_PATTERN)
_IDENTIFIER_RE: Final = re.compile(IDENTIFIER_PATTERN)
_OPAQUE_TOKEN_RE: Final = re.compile(OPAQUE_TOKEN_PATTERN)
_OPEN_CODE_RE: Final = re.compile(OPEN_CODE_PATTERN)
_WORKSPACE_ID_RE: Final = re.compile(WORKSPACE_ID_PATTERN)
_EVIDENCE_CHECKSUM_RE: Final = re.compile(EVIDENCE_CHECKSUM_PATTERN)
_MEDIA_TYPE_RE: Final = re.compile(MEDIA_TYPE_PATTERN)
_TIMESTAMP_RE: Final = re.compile(TIMESTAMP_PATTERN)
_EPOCH_ORDINAL: Final = date(1970, 1, 1).toordinal()


class EvidenceCaptureSizeLimitError(ContractSemanticError):
    """A capture content representation is provably above the decoded-byte ceiling.

    This is deliberately narrower than :class:`ContractSemanticError`: empty content,
    malformed base64 and non-UTF-8 bytes are invalid input, while a well-formed value
    above the advertised bound is the Application Contract's
    ``size_limit_exceeded`` branch.  Keeping that distinction in the shared semantic
    layer lets every transport ask the service for the same canonical classification.
    """


def _require_type(value: object, expected: type | tuple[type, ...], label: str) -> None:
    """Raise unless `value` is an instance of `expected`.

    The one guard every direct-entry-point function in this module runs before doing
    anything else with an argument it did not itself decode: a caller reaching these
    functions without a tolerant `from_wire` decode in front (a hand-built dataclass, a
    plain dict, `None`, a wrong-typed field on an otherwise-valid dataclass) must see a
    `ContractSemanticError`, never a raw `TypeError`/`AttributeError` from whatever this
    module does with `value` next.
    """
    if (
        isinstance(value, bool)
        and expected is not bool
        and not (isinstance(expected, tuple) and bool in expected)
    ):
        raise ContractSemanticError(f"{label}: expected {expected!r}, got bool")
    if not isinstance(value, expected):
        raise ContractSemanticError(
            f"{label}: expected {expected!r}, got {type(value).__name__}"
        )


def _require_str(value: object, label: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise ContractSemanticError(
            f"{label}: expected a string, got {type(value).__name__}"
        )
    return value


def _require_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ContractSemanticError(
            f"{label}: expected a boolean, got {type(value).__name__}"
        )
    return value


def _require_int(value: object, label: str) -> int:
    """Raise unless `value` is a real integer, and return it.

    `bool` is refused ahead of `int` deliberately: Python makes `True` an integer equal to
    1, so a hand-built `SourceSpan(start_offset=True)` would otherwise satisfy every
    `>= 0` range check and be honoured as an offset of one. It is a JSON boolean, not a
    JSON number, and the schema says `type: integer`.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractSemanticError(
            f"{label}: expected an integer, got {type(value).__name__}"
        )
    return value


def _require_sequence(value: object, label: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractSemanticError(
            f"{label}: expected a sequence, got {type(value).__name__}"
        )
    return value


def _require_mapping(value: object, label: str) -> Mapping[object, object]:
    if not isinstance(value, Mapping):
        raise ContractSemanticError(
            f"{label}: expected a mapping, got {type(value).__name__}"
        )
    return value


def _validate_identifier(value: object, label: str) -> None:
    text = _require_str(value, label)
    if len(text) > _BOUNDED_VALUE_MAX_LENGTH or not _IDENTIFIER_RE.fullmatch(text):
        raise ContractSemanticError(f"{label}: {text!r} is not a valid Identifier")


def _validate_opaque_token(value: object, label: str) -> None:
    """Validate a server-issued opaque token, held to the same bound and pattern the sibling
    semantic modules hold one to: the token domain is one domain wherever it is reached from,
    and `import_run_id` is reached from both the job side and this one."""
    text = _require_str(value, label)
    if len(text) > _OPAQUE_TOKEN_MAX_LENGTH or not _OPAQUE_TOKEN_RE.fullmatch(text):
        raise ContractSemanticError(f"{label}: {text!r} is not a valid OpaqueToken")


def _validate_open_code(value: object, label: str) -> str:
    text = _require_str(value, label)
    if len(text) > _BOUNDED_VALUE_MAX_LENGTH or not _OPEN_CODE_RE.fullmatch(text):
        raise ContractSemanticError(f"{label}: {text!r} is not a valid OpenCode")
    return text


def _validate_workspace_id(value: object, label: str) -> None:
    text = _require_str(value, label)
    if len(text) > _BOUNDED_VALUE_MAX_LENGTH or not _WORKSPACE_ID_RE.fullmatch(text):
        raise ContractSemanticError(f"{label}: {text!r} is not a valid WorkspaceId")


def _validate_evidence_id(value: object, label: str) -> None:
    text = _require_str(value, label)
    if len(text) > _BOUNDED_VALUE_MAX_LENGTH or not _EVIDENCE_ID_RE.fullmatch(text):
        raise ContractSemanticError(f"{label}: {text!r} is not a valid EvidenceId")


def _validate_evidence_checksum(value: object, label: str) -> None:
    text = _require_str(value, label)
    if len(text) > _EVIDENCE_CHECKSUM_MAX_LENGTH or not _EVIDENCE_CHECKSUM_RE.fullmatch(
        text
    ):
        raise ContractSemanticError(
            f"{label}: {text!r} is not a valid EvidenceChecksum"
        )


def _validate_media_type(value: object, label: str) -> None:
    text = _require_str(value, label)
    if len(text) > _MEDIA_TYPE_MAX_LENGTH or not _MEDIA_TYPE_RE.fullmatch(text):
        raise ContractSemanticError(f"{label}: {text!r} is not a valid MediaType")


def canonical_timestamp_nanoseconds(value: object, label: str = "timestamp") -> int:
    """Return an exact UTC epoch-nanosecond value for one canonical timestamp.

    The contract admits one through nine fractional digits.  ``datetime`` validates
    the calendar and clock fields, but its microsecond field cannot represent the last
    three digits; the fraction is therefore parsed independently and combined with an
    integer day/second offset.  No float participates, so ordering and identity remain
    exact below a microsecond as well as before the Unix epoch.
    """
    text = _require_str(value, label)
    if not _TIMESTAMP_RE.match(text):
        raise ContractSemanticError(
            f"{label}: {text!r} is not a canonical RFC 3339 UTC timestamp "
            "(naive, offset, and malformed spellings are all rejected)"
        )
    try:
        instant = datetime(
            int(text[0:4]),
            int(text[5:7]),
            int(text[8:10]),
            int(text[11:13]),
            int(text[14:16]),
            int(text[17:19]),
            tzinfo=UTC,
        )
    except ValueError as error:
        raise ContractSemanticError(
            f"{label}: {text!r} is not a valid RFC 3339 timestamp"
        ) from error
    fraction = text[20:-1] if text[19:20] == "." else ""
    nanoseconds = int(fraction.ljust(9, "0")) if fraction else 0
    days = instant.date().toordinal() - _EPOCH_ORDINAL
    seconds = (
        days * 86_400 + instant.hour * 3_600 + instant.minute * 60 + instant.second
    )
    return seconds * 1_000_000_000 + nanoseconds


def _parse_timestamp(value: object, label: str) -> int:
    """Validate a timestamp and return its exact ordering key."""
    return canonical_timestamp_nanoseconds(value, label)


def _validate_source_reference(source: object, label: str) -> tuple[str, str]:
    """Validate `source` and return its `(kind, source_id)` identity key."""
    _require_type(source, SourceReference, label)
    assert isinstance(source, SourceReference)
    _validate_open_code(source.kind, f"{label}.kind")
    _validate_identifier(source.source_id, f"{label}.source_id")
    if (
        source.locator is not None
        and len(_require_str(source.locator, f"{label}.locator")) > _LOCATOR_MAX_LENGTH
    ):
        raise ContractSemanticError(
            f"{label}.locator exceeds the maximum length of {_LOCATOR_MAX_LENGTH}"
        )
    if source.retrieved_at is not None:
        _parse_timestamp(source.retrieved_at, f"{label}.retrieved_at")
    return (source.kind, source.source_id)


def _validate_source_span(span: object, label: str) -> None:
    """Raise unless `span` has a bounded `pointer` and a coherent, non-negative offset pair.

    Each present offset must be a *real* integer before it is compared, not merely something
    that answers `< 0`. The schema says `type: integer, minimum: 0`, and two values would
    otherwise slip past a bare comparison: a `bool`, which Python makes an integer and which
    a `>= 0` test silently accepts as an offset of one, and a `str`, which would raise a raw
    `TypeError` out of this contract layer instead of a `ContractSemanticError`.
    """
    _require_type(span, SourceSpan, label)
    assert isinstance(span, SourceSpan)
    if len(_require_str(span.pointer, f"{label}.pointer")) > _LOCATOR_MAX_LENGTH:
        raise ContractSemanticError(
            f"{label}.pointer exceeds the maximum length of {_LOCATOR_MAX_LENGTH}"
        )
    start = None
    if span.start_offset is not None:
        start = _require_int(span.start_offset, f"{label}.start_offset")
        if start < 0:
            raise ContractSemanticError(f"{label}.start_offset must be non-negative")
    end = None
    if span.end_offset is not None:
        end = _require_int(span.end_offset, f"{label}.end_offset")
        if end < 0:
            raise ContractSemanticError(f"{label}.end_offset must be non-negative")
    if start is not None and end is not None and end < start:
        raise ContractSemanticError(
            f"{label}.end_offset is before {label}.start_offset"
        )


def _validate_evidence_reference(
    reference: object, label: str, *, artifact_source_key: tuple[str, str]
) -> None:
    """Validate one `EvidenceReference` nested inside a `ProvenanceEntry`, and require it
    to agree with the owning artifact's exact `(source.kind, source.source_id)` identity.

    A `ProvenanceEntry.evidence` entry naming a different source than the artifact it is
    attached to is a foreign, undeclared source smuggled into this artifact's own
    provenance -- exactly the kind of unaudited cross-artifact reference this artifact's
    append-only history must never allow.
    """
    _require_type(reference, EvidenceReference, label)
    assert isinstance(reference, EvidenceReference)
    source_key = _validate_source_reference(reference.source, f"{label}.source")
    if source_key != artifact_source_key:
        raise ContractSemanticError(
            f"{label}.source {source_key!r} does not agree with this artifact's own source "
            f"{artifact_source_key!r} (foreign, undeclared source evidence)"
        )
    if reference.span is not None:
        _validate_source_span(reference.span, f"{label}.span")
    if (
        reference.excerpt is not None
        and len(_require_str(reference.excerpt, f"{label}.excerpt"))
        > _EXCERPT_MAX_LENGTH
    ):
        raise ContractSemanticError(
            f"{label}.excerpt exceeds the maximum length of {_EXCERPT_MAX_LENGTH}"
        )


def _validate_provenance_entry(
    entry: object, label: str, *, artifact_source_key: tuple[str, str]
) -> str:
    """Validate one `ProvenanceEntry` and every nested `EvidenceReference` it carries.
    Returns the entry's validated `action`.

    `reason_code`/`reason_comment` are optional on the wire and are validated whenever
    present, exactly as :mod:`~omnivia_core.contracts.v1.semantics` validates them on a
    governed record's history: the same `ProvenanceEntry` shape reached from the evidence
    side must not be held to a weaker bar than the same shape reached from the record side,
    or an artifact rejected as a record's history would be accepted as an artifact's.
    Requiring them is a governance-transition concern that L0 evidence has no notion of, so
    only their shape is checked here.
    """
    _require_type(entry, ProvenanceEntry, label)
    assert isinstance(entry, ProvenanceEntry)
    _validate_identifier(entry.actor_id, f"{label}.actor_id")
    _validate_open_code(entry.actor_kind, f"{label}.actor_kind")
    _validate_open_code(entry.action, f"{label}.action")
    _parse_timestamp(entry.occurred_at, f"{label}.occurred_at")
    if entry.reason_code is not None:
        _validate_open_code(entry.reason_code, f"{label}.reason_code")
    if entry.reason_comment is not None:
        comment = _require_str(entry.reason_comment, f"{label}.reason_comment")
        if len(comment) > _REASON_COMMENT_MAX_LENGTH:
            raise ContractSemanticError(
                f"{label}.reason_comment exceeds the maximum length of "
                f"{_REASON_COMMENT_MAX_LENGTH}"
            )
    if entry.evidence is not None:
        references = _require_sequence(entry.evidence, f"{label}.evidence")
        if len(references) > _PROVENANCE_ENTRY_EVIDENCE_MAX_ITEMS:
            raise ContractSemanticError(
                f"{label}.evidence has {len(references)} entries, exceeding the maximum of "
                f"{_PROVENANCE_ENTRY_EVIDENCE_MAX_ITEMS}"
            )
        for index, reference in enumerate(references):
            _validate_evidence_reference(
                reference,
                f"{label}.evidence[{index}]",
                artifact_source_key=artifact_source_key,
            )
    return entry.action


def validate_evidence_artifact(artifact: object) -> None:
    """Raise unless `artifact` is an internally coherent L0 evidence artifact.

    Validates `evidence_id`/`workspace_id` shape; `source.kind`/`source_id` shape and
    bounded `locator`/`retrieved_at` (mirroring `records.schema.json#/$defs/SourceReference`);
    `temporal` (composing :func:`~omnivia_core.contracts.v1.semantics.
    validate_record_temporal_metadata`), plus the evidence-specific rule that `observed_at`,
    when present, must not be after `ingested_at`: a system cannot ingest a fact before it was
    observed, regardless of what `event_at` says; `content_checksum`/`media_type` shape;
    `metadata` is present (an empty object is a valid, deliberate "no metadata" statement,
    but an absent field is not: it must never be conflated with "not yet decided"); every
    `provenance_history` entry is structurally valid, including its optional
    `reason_code`/`reason_comment` and every nested
    `EvidenceReference`/`source`/`span`/`excerpt`, and every nested reference must agree with
    this artifact's own exact `source` identity -- a nested reference naming a different
    source is rejected as foreign, undeclared source evidence; `provenance_history` itself is
    never empty (even first capture is itself a provenance entry); `permission_labels` are
    each a bounded, pattern-valid `OpenCode` with no duplicates (a repeated label is a
    contradiction of a clean label set, not additive detail); `sensitivity`/`parser_status`/
    `ingestion_status` shape; `tombstoned` is a real JSON boolean rather than anything that
    merely tests truthy -- an integer `0`/`1` there would silently decide whether a
    tombstone had to be audited at all; a tombstoned artifact (`tombstoned` true) must carry
    at least one `provenance_history` entry whose `action` is the frozen tombstone action
    (:data:`EVIDENCE_TOMBSTONE_ACTION`) -- a generic `created`/`modified` entry is not
    evidence the tombstoning act itself was audited; and `import_run_id`, when present, is a
    bounded, pattern-valid `OpaqueToken` (absent because evidence may be captured outside an
    import run, but validated when the artifact does name one) -- the same token domain as
    `JobIdentity.job_id` and `ImportCompletionResult.import_run_id`, so a run that completed
    under an opaque job id such as `job/opaque-token` can actually be written into the
    backlink the completion contract promises, rather than naming a run no artifact could
    point back to.

    Every bounded array is held to its schema `maxItems` here as well as in the schema --
    `permission_labels` (:data:`_PERMISSION_LABELS_MAX_ITEMS`), `provenance_history`
    (:data:`_PROVENANCE_HISTORY_MAX_ITEMS`), and each entry's own `evidence`
    (:data:`_PROVENANCE_ENTRY_EVIDENCE_MAX_ITEMS`) -- because the tolerant decoder applies
    no cardinality at all.
    """
    _require_type(artifact, EvidenceArtifact, "artifact")
    assert isinstance(artifact, EvidenceArtifact)

    _validate_evidence_id(artifact.evidence_id, "evidence_id")
    _validate_workspace_id(artifact.workspace_id, "workspace_id")

    artifact_source_key = _validate_source_reference(artifact.source, "source")

    _require_type(artifact.temporal, type(artifact.temporal), "temporal")
    validate_record_temporal_metadata(artifact.temporal)
    if artifact.temporal.observed_at is not None:
        observed_at = _parse_timestamp(
            artifact.temporal.observed_at, "temporal.observed_at"
        )
        ingested_at = _parse_timestamp(
            artifact.temporal.ingested_at, "temporal.ingested_at"
        )
        if observed_at > ingested_at:
            raise ContractSemanticError(
                f"temporal.observed_at {artifact.temporal.observed_at!r} is after "
                f"temporal.ingested_at {artifact.temporal.ingested_at!r}"
            )

    _validate_evidence_checksum(artifact.content_checksum, "content_checksum")
    _validate_media_type(artifact.media_type, "media_type")
    _require_mapping(artifact.metadata, "metadata")

    permission_labels = _require_sequence(
        artifact.permission_labels, "permission_labels"
    )
    if len(permission_labels) > _PERMISSION_LABELS_MAX_ITEMS:
        raise ContractSemanticError(
            f"permission_labels has {len(permission_labels)} entries, exceeding the maximum "
            f"of {_PERMISSION_LABELS_MAX_ITEMS}"
        )
    seen_labels: set[str] = set()
    for index, label in enumerate(permission_labels):
        validated_label = _validate_open_code(label, f"permission_labels[{index}]")
        if validated_label in seen_labels:
            raise ContractSemanticError(
                f"permission_labels[{index}] duplicates {validated_label!r}"
            )
        seen_labels.add(validated_label)

    _validate_open_code(artifact.sensitivity, "sensitivity")
    _validate_open_code(artifact.parser_status, "parser_status")
    _validate_open_code(artifact.ingestion_status, "ingestion_status")

    tombstoned = _require_bool(artifact.tombstoned, "tombstoned")

    provenance_history = _require_sequence(
        artifact.provenance_history, "provenance_history"
    )
    if len(provenance_history) > _PROVENANCE_HISTORY_MAX_ITEMS:
        raise ContractSemanticError(
            f"provenance_history has {len(provenance_history)} entries, exceeding the "
            f"maximum of {_PROVENANCE_HISTORY_MAX_ITEMS}"
        )
    if tombstoned and not provenance_history:
        raise ContractSemanticError(
            "a tombstoned evidence artifact must carry a provenance_history entry whose "
            f"action is {EVIDENCE_TOMBSTONE_ACTION!r}; an empty provenance_history is not "
            "evidence the tombstoning act itself was audited"
        )
    if not provenance_history:
        raise ContractSemanticError(
            "provenance_history must not be empty: even first capture of this artifact is "
            "itself a provenance entry"
        )
    actions: list[str] = []
    for index, entry in enumerate(provenance_history):
        actions.append(
            _validate_provenance_entry(
                entry,
                f"provenance_history[{index}]",
                artifact_source_key=artifact_source_key,
            )
        )

    if tombstoned and EVIDENCE_TOMBSTONE_ACTION not in actions:
        raise ContractSemanticError(
            f"a tombstoned evidence artifact must carry a provenance_history entry whose "
            f"action is {EVIDENCE_TOMBSTONE_ACTION!r}; a generic create/modify entry is not "
            "evidence the tombstoning act itself was audited"
        )

    if artifact.import_run_id is not None:
        _validate_opaque_token(artifact.import_run_id, "import_run_id")


def validate_evidence_search_input(input_: object) -> None:
    """Raise unless `input_` is a structurally and semantically valid `evidence.search`
    input: bounded `query` length, a valid `sensitivity` open code when present, and a
    bounded `limit` when present.
    """
    _require_type(input_, EvidenceSearchInput, "input_")
    assert isinstance(input_, EvidenceSearchInput)
    query = _require_str(input_.query, "query")
    if not (1 <= len(query) <= _QUERY_MAX_LENGTH):
        raise ContractSemanticError(
            f"query length is outside the bounded range [1, {_QUERY_MAX_LENGTH}]"
        )
    if input_.sensitivity is not None:
        _validate_open_code(input_.sensitivity, "sensitivity")
    if input_.include_tombstoned is not None:
        _require_bool(input_.include_tombstoned, "include_tombstoned")
    if input_.limit is not None:
        validate_page_limit(input_.limit)


def decode_evidence_search_input(
    payload: object, path: str = "EvidenceSearchInput"
) -> EvidenceSearchInput:
    """Decode and fully validate `payload` into a semantically valid `EvidenceSearchInput`."""
    input_ = EvidenceSearchInput.from_wire(payload, path)
    validate_evidence_search_input(input_)
    return input_


def validate_evidence_search_result(
    result: object, request: object, expected_workspace_id: object
) -> None:
    """Raise unless every evidence artifact in `result` is valid, belongs to
    `expected_workspace_id`, and agrees with everything `request` (the original,
    already-validated `EvidenceSearchInput`) actually asked for.

    `request` is bound in full, not just `include_tombstoned` in isolation: when
    `request.sensitivity` is set, every returned artifact must carry exactly that
    sensitivity; a tombstoned artifact may appear only when `request.include_tombstoned`
    is explicitly `true` (an absent or `false` request field means "excluded", and this
    function never assumes a default on the caller's behalf); and the number of returned
    artifacts may never exceed `request.limit` when set, or the schema's own default
    result-page ceiling (:data:`_DEFAULT_RESULT_LIMIT`) when it is not.
    """
    _require_type(result, EvidenceSearchResult, "result")
    assert isinstance(result, EvidenceSearchResult)
    _require_type(request, EvidenceSearchInput, "request")
    assert isinstance(request, EvidenceSearchInput)
    validate_evidence_search_input(request)
    _validate_workspace_id(expected_workspace_id, "expected_workspace_id")

    evidence = _require_sequence(result.evidence, "result.evidence")
    max_results = request.limit if request.limit is not None else _DEFAULT_RESULT_LIMIT
    if len(evidence) > max_results:
        raise ContractSemanticError(
            f"result.evidence has {len(evidence)} entries, exceeding the applicable limit of "
            f"{max_results}"
        )

    include_tombstoned = bool(request.include_tombstoned)
    for index, artifact in enumerate(evidence):
        label = f"evidence[{index}]"
        validate_evidence_artifact(artifact)
        assert isinstance(artifact, EvidenceArtifact)
        if artifact.workspace_id != expected_workspace_id:
            raise ContractSemanticError(
                f"{label}.workspace_id {artifact.workspace_id!r} does not match the "
                f"selected workspace {expected_workspace_id!r}"
            )
        if artifact.tombstoned and not include_tombstoned:
            raise ContractSemanticError(
                f"{label} is tombstoned but the request did not set include_tombstoned=true"
            )
        if (
            request.sensitivity is not None
            and artifact.sensitivity != request.sensitivity
        ):
            raise ContractSemanticError(
                f"{label}.sensitivity {artifact.sensitivity!r} does not match the requested "
                f"sensitivity {request.sensitivity!r}"
            )


def _decode_evidence_capture_content(input_: EvidenceCaptureInput) -> bytes:
    """Raise unless `input_` carries exactly one content form, and return its decoded bytes.

    Checked in this order, each ahead of the next so no step does costly work a cheaper
    prior step could have refused: exactly one of `text`/`content_base64` is present;
    `content_base64`'s own encoded length is bounded before any decode is attempted (the
    encoded-size bound v1.3 section 6.2 requires ahead of unbounded base64 allocation);
    the encoded text decodes as strict RFC 4648 base64 (no whitespace tolerance, no
    non-alphabet characters, exact padding); and only then is the decoded byte length
    bound (:data:`EVIDENCE_CAPTURE_MAX_CONTENT_BYTES`) checked against the real decoded
    bytes -- `text`'s own UTF-8 encoding is bounded the same way, since a `text` value is
    already decoded and skips only the base64 step.
    """
    has_text = input_.text is not None
    has_base64 = input_.content_base64 is not None
    if has_text == has_base64:
        raise ContractSemanticError(
            "exactly one of text/content_base64 is required, "
            f"found {'both' if has_text else 'neither'}"
        )

    if has_text:
        text = _require_str(input_.text, "text")
        try:
            content = text.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ContractSemanticError(
                f"text is not valid Unicode text: {error}"
            ) from error
    else:
        encoded = _require_str(input_.content_base64, "content_base64")
        if len(encoded) > _EVIDENCE_CAPTURE_MAX_BASE64_LENGTH:
            raise EvidenceCaptureSizeLimitError(
                f"content_base64 length {len(encoded)} exceeds the maximum of "
                f"{_EVIDENCE_CAPTURE_MAX_BASE64_LENGTH} encoded characters, the largest "
                f"encoding that could decode to no more than "
                f"{EVIDENCE_CAPTURE_MAX_CONTENT_BYTES} bytes"
            )
        if not re.fullmatch(r"[A-Za-z0-9+/]*={0,2}", encoded) or len(encoded) % 4 != 0:
            raise ContractSemanticError("content_base64 is not strict RFC 4648 base64")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ContractSemanticError(
                f"content_base64 is not strict RFC 4648 base64: {error}"
            ) from error
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ContractSemanticError(
                f"content_base64 does not decode to valid UTF-8: {error}"
            ) from error

    if len(content) > EVIDENCE_CAPTURE_MAX_CONTENT_BYTES:
        raise EvidenceCaptureSizeLimitError(
            f"decoded content length {len(content)} exceeds the maximum of "
            f"{EVIDENCE_CAPTURE_MAX_CONTENT_BYTES} bytes"
        )
    if len(content) < 1:
        raise ContractSemanticError(
            f"decoded content length {len(content)} is outside the bounded range "
            f"[1, {EVIDENCE_CAPTURE_MAX_CONTENT_BYTES}]"
        )
    return content


def validate_evidence_capture_input(input_: object) -> None:
    """Raise unless `input_` is a structurally and semantically valid `evidence.capture`
    input: bounded `source_native_id`/`source_version` identifiers, `media_type` restricted
    to the exact allowlist (:data:`EVIDENCE_CAPTURE_ALLOWED_MEDIA_TYPES`), exactly one of
    `text`/`content_base64` present and decodable to 1..1,048,576 bytes of valid UTF-8
    (:func:`_decode_evidence_capture_content`), and -- when both are present --
    `event_at` not later than `observed_at`.
    """
    _require_type(input_, EvidenceCaptureInput, "input_")
    assert isinstance(input_, EvidenceCaptureInput)

    _validate_identifier(input_.source_native_id, "source_native_id")

    media_type = _require_str(input_.media_type, "media_type")
    if media_type not in EVIDENCE_CAPTURE_ALLOWED_MEDIA_TYPES:
        raise ContractSemanticError(
            f"media_type {media_type!r} is not one of {sorted(EVIDENCE_CAPTURE_ALLOWED_MEDIA_TYPES)!r}"
        )

    _decode_evidence_capture_content(input_)

    if input_.source_version is not None:
        _validate_identifier(input_.source_version, "source_version")

    event_at = None
    observed_at = None
    if input_.event_at is not None:
        event_at = _parse_timestamp(input_.event_at, "event_at")
    if input_.observed_at is not None:
        observed_at = _parse_timestamp(input_.observed_at, "observed_at")
    if event_at is not None and observed_at is not None and event_at > observed_at:
        raise ContractSemanticError(
            f"event_at {input_.event_at!r} is after observed_at {input_.observed_at!r}"
        )


def decode_evidence_capture_input(
    payload: object, path: str = "EvidenceCaptureInput"
) -> EvidenceCaptureInput:
    """Decode one closed operation input and fully validate its semantics.

    Generated DTO ``from_wire`` methods remain additive-tolerant for minor-version
    compatibility.  The operation itself is intentionally closed, though, so its public
    decoder rejects a raw key the schema does not declare before the tolerant DTO layer
    could discard it.  MCP and the service both use this function.
    """
    if isinstance(payload, Mapping):
        accepted = {
            "source_native_id",
            "media_type",
            "text",
            "content_base64",
            "source_version",
            "event_at",
            "observed_at",
        }
        if any(not isinstance(key, str) or key not in accepted for key in payload):
            raise ContractSemanticError(
                f"{path}: contains a member the closed evidence.capture input does not declare"
            )
    input_ = EvidenceCaptureInput.from_wire(payload, path)
    validate_evidence_capture_input(input_)
    return input_


def validate_evidence_capture_result(result: object) -> None:
    """Raise unless `result` is an internally coherent `evidence.capture` result:
    bounded `evidence_id`; `source` is exactly `{kind: "direct_submission", source_id}`
    with a bounded, pattern-valid `source_id` and no `locator`/`retrieved_at` (a direct
    submission's identity is the caller-supplied native id alone, never a locator or a
    retrieval instant borrowed from some other source kind); `media_type` restricted to
    the exact allowlist (:data:`EVIDENCE_CAPTURE_ALLOWED_MEDIA_TYPES`); `content_checksum`
    is shape-valid and specifically a lowercase `sha256:` digest, not merely any
    `EvidenceChecksum`-shaped algorithm; `content_length_bytes` falls in
    `[1, 1_048_576]`; and `capture_disposition` is one of the closed
    `created`/`already_captured` vocabulary.
    """
    _require_type(result, EvidenceCaptureResult, "result")
    assert isinstance(result, EvidenceCaptureResult)

    _validate_evidence_id(result.evidence_id, "evidence_id")

    source = result.source
    _require_type(source, SourceReference, "source")
    assert isinstance(source, SourceReference)
    if source.kind != EVIDENCE_CAPTURE_SOURCE_KIND:
        raise ContractSemanticError(
            f"source.kind {source.kind!r} must be {EVIDENCE_CAPTURE_SOURCE_KIND!r}"
        )
    _validate_identifier(source.source_id, "source.source_id")
    if source.locator is not None:
        raise ContractSemanticError(
            "source.locator must be absent for a direct_submission source"
        )
    if source.retrieved_at is not None:
        raise ContractSemanticError(
            "source.retrieved_at must be absent for a direct_submission source"
        )

    media_type = _require_str(result.media_type, "media_type")
    if media_type not in EVIDENCE_CAPTURE_ALLOWED_MEDIA_TYPES:
        raise ContractSemanticError(
            f"media_type {media_type!r} is not one of {sorted(EVIDENCE_CAPTURE_ALLOWED_MEDIA_TYPES)!r}"
        )

    checksum = _require_str(result.content_checksum, "content_checksum")
    _validate_evidence_checksum(checksum, "content_checksum")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", checksum):
        raise ContractSemanticError(
            f"content_checksum {checksum!r} must be a lowercase sha256:<64 hex digits> digest"
        )

    length = _require_int(result.content_length_bytes, "content_length_bytes")
    if not (1 <= length <= EVIDENCE_CAPTURE_MAX_CONTENT_BYTES):
        raise ContractSemanticError(
            f"content_length_bytes {length} is outside the bounded range "
            f"[1, {EVIDENCE_CAPTURE_MAX_CONTENT_BYTES}]"
        )

    disposition = _require_str(result.capture_disposition, "capture_disposition")
    if disposition not in _EVIDENCE_CAPTURE_DISPOSITIONS:
        raise ContractSemanticError(
            f"capture_disposition {disposition!r} is not one of "
            f"{sorted(_EVIDENCE_CAPTURE_DISPOSITIONS)!r}"
        )


# --------------------------------------------------------------------------
# `document.history`
# --------------------------------------------------------------------------
#
# The wire shape types every one of this read's state fields as `OpenCode`, because the
# envelope must survive a newer peer adding vocabulary. The closed sets below are this
# build's own reading of its own v1 result: a value outside them is a state this build
# cannot describe, and describing it anyway -- as a default, or as a near-miss -- is the
# dishonest outcome this module exists to prevent. The validators also hold the result to
# its request and to itself, because the properties that matter here are relational: which
# member of a history is current is stated by the head and nowhere else, an action's
# unavailability must carry its reason, and a page may never claim more than it holds.

#: `intent_kind`: why the capture was admitted.
DOCUMENT_CAPTURE_INTENT_KINDS: Final = frozenset(
    {"new_capture", "refresh", "automatic_capture", "folder_capture"}
)
#: `association_kind`: what original lineage the capture was admitted under.
DOCUMENT_CAPTURE_ASSOCIATION_KINDS: Final = frozenset(
    {"snapshot_only", "active_binding", "historical_binding"}
)
#: `capture_disposition`: whether admission left the capture able to become the head.
DOCUMENT_CAPTURE_DISPOSITIONS: Final = frozenset({"head_eligible", "historical_only"})
#: `disposition_reason`: why that disposition was assigned.
DOCUMENT_CAPTURE_DISPOSITION_REASONS: Final = frozenset(
    {
        "eligible",
        "removed_source",
        "superseded_generation",
        "disconnected_generation",
        "creation_conflict",
        "stale_base",
    }
)
#: `retention_state`: whether the captured bytes are still held.
DOCUMENT_CAPTURE_RETENTION_STATES: Final = frozenset({"retained", "expired", "deleted"})
#: `processing_state`: the capture's processing obligation, never a retrieval claim.
DOCUMENT_CAPTURE_PROCESSING_STATES: Final = frozenset(
    {"pending", "settled", "not_queued"}
)
#: `result_state`: one extraction's terminal outcome.
DOCUMENT_EXTRACTION_RESULT_STATES: Final = frozenset(
    {"succeeded", "partial", "unsupported", "failed"}
)
#: The two terminal states that produce a qualified output and may therefore be published.
DOCUMENT_EXTRACTION_PUBLISHABLE_STATES: Final = frozenset({"succeeded", "partial"})
#: `publication_disposition`: what the terminal result did to retrieval.
DOCUMENT_PUBLICATION_DISPOSITIONS: Final = frozenset(
    {
        "promoted",
        "superseded",
        "historical_only",
        "fenced_removal",
        "fenced_retention",
        "not_published",
    }
)
#: `source_state`: the logical source's own disposition.
DOCUMENT_SOURCE_STATES: Final = frozenset({"active", "removed"})
#: `original_binding_state`: an aggregate over the opaque routes, never one route.
DOCUMENT_BINDING_STATES: Final = frozenset({"none", "connected", "disconnected"})
#: `retrieval_state`: whether retrieval rows are published for the document now.
DOCUMENT_RETRIEVAL_STATES: Final = frozenset({"retrievable", "none"})
#: `processing_mode`: the two modes a profile may select.
DOCUMENT_PROCESSING_MODES: Final = frozenset({"text_only", "text_and_local_ocr"})
#: The actions this build reports, and the reasons it reports them unavailable for.
DOCUMENT_HISTORY_ACTIONS: Final = frozenset(
    {"reprocess_captured_copy", "refresh_original"}
)
DOCUMENT_HISTORY_UNAVAILABLE_REASONS: Final = frozenset(
    {
        "source_removed",
        "source_not_retained",
        "format_not_qualified",
        "no_original_binding",
        "original_disconnected",
    }
)
#: The largest page `document.history` serves, for each of its two histories.
DOCUMENT_HISTORY_MAX_PAGE: Final = 200
_DOCUMENT_HISTORY_MAX_ACTIONS: Final = 8
_DECODER_BUILD_MAX_LENGTH: Final = 512
_WORKER_BUILD_MAX_LENGTH: Final = 128
#: The normative maximum a recognition-language selection may hold. A summary naming a
#: fourth describes a selection the installation would itself have refused, so it is not a
#: profile this build will report.
_OCR_LANGUAGE_MAX_COUNT: Final = 3
_DOCUMENT_HISTORY_MAX_SOURCE_BYTES: Final = 52428800
_INT64_MAX: Final = 9223372036854775807


def _validate_closed_code(value: object, label: str, allowed: frozenset[str]) -> str:
    """Validate one wire-open code against the closed set this build can describe."""
    text = _validate_open_code(value, label)
    if text not in allowed:
        raise ContractSemanticError(
            f"{label}: {text!r} is not one of {sorted(allowed)!r}"
        )
    return text


def _validate_bounded_int(value: object, label: str, *, minimum: int) -> int:
    number = _require_int(value, label)
    if not (minimum <= number <= _INT64_MAX):
        raise ContractSemanticError(
            f"{label}: {number} is outside the bounded range [{minimum}, {_INT64_MAX}]"
        )
    return number


def _validate_history_request_page(page: object, label: str) -> None:
    """A request's `page` is the position it continues from, so it must name one.

    Rejecting a present-but-empty `page` is the same rule the other paginated reads apply:
    "start at the beginning" is stated by omitting the field, so an empty one is a third
    spelling a validator could only honour by guessing.
    """
    _require_type(page, PageMetadata, label)
    assert isinstance(page, PageMetadata)
    if page.continuation_token is None:
        raise ContractSemanticError(
            f"{label} is present but names no continuation_token; an absent page is how "
            "'start at the first page' is stated"
        )
    _validate_opaque_token(page.continuation_token, f"{label}.continuation_token")


def _validate_history_result_page(page: object, label: str) -> None:
    """A result's `page` is always present; `{}` is the statement that the read is exhausted."""
    _require_type(page, PageMetadata, label)
    assert isinstance(page, PageMetadata)
    if page.continuation_token is not None:
        _validate_opaque_token(page.continuation_token, f"{label}.continuation_token")


def _validated_history_input(input_: object, label: str) -> DocumentHistoryInput:
    """Validate the complete input and return it, for the result check to bind against."""
    _require_type(input_, DocumentHistoryInput, label)
    assert isinstance(input_, DocumentHistoryInput)
    _validate_identifier(input_.document_id, f"{label}.document_id")
    if input_.limit is not None:
        validate_page_limit(_require_int(input_.limit, f"{label}.limit"))
    if input_.page is not None:
        _validate_history_request_page(input_.page, f"{label}.page")
    return input_


def validate_document_history_input(input_: object) -> None:
    """Raise unless `input_` is a semantically valid `document.history` input.

    One document, a bounded page size, and a continuation that actually names a position.
    Nothing else: this read has no filter, no selector and no second workspace, so there is
    nothing else that could widen it.
    """
    _validated_history_input(input_, "input_")


def decode_document_history_input(
    payload: object, path: str = "DocumentHistoryInput"
) -> DocumentHistoryInput:
    """Decode and fully validate `payload` into a semantically valid `DocumentHistoryInput`."""
    return _validated_history_input(DocumentHistoryInput.from_wire(payload, path), path)


def _validate_history_action(action: object, label: str) -> tuple[str, bool]:
    _require_type(action, DocumentHistoryAction, label)
    assert isinstance(action, DocumentHistoryAction)
    name = _validate_closed_code(action.action, f"{label}.action", DOCUMENT_HISTORY_ACTIONS)
    available = _require_bool(action.available, f"{label}.available")
    if available and action.unavailable_reason is not None:
        raise ContractSemanticError(
            f"{label}: an available action carries no unavailable_reason"
        )
    if not available:
        if action.unavailable_reason is None:
            raise ContractSemanticError(
                f"{label}: an unavailable action must state why, so a caller shows the "
                "reason rather than a control that cannot work"
            )
        _validate_closed_code(
            action.unavailable_reason,
            f"{label}.unavailable_reason",
            DOCUMENT_HISTORY_UNAVAILABLE_REASONS,
        )
    return name, available


def _validate_reprocess_profile_option(option: object, label: str) -> None:
    _require_type(option, DocumentReprocessProfileOption, label)
    assert isinstance(option, DocumentReprocessProfileOption)
    _validate_identifier(option.profile_ref, f"{label}.profile_ref")
    _validate_identifier(option.revision, f"{label}.revision")
    _validate_closed_code(
        option.processing_mode, f"{label}.processing_mode", DOCUMENT_PROCESSING_MODES
    )
    _validate_evidence_checksum(option.profile_digest, f"{label}.profile_digest")


def _validate_capture_history_entry(
    entry: object, label: str, *, current_capture_id: str | None
) -> None:
    _require_type(entry, DocumentCaptureHistoryEntry, label)
    assert isinstance(entry, DocumentCaptureHistoryEntry)
    _validate_identifier(entry.capture_id, f"{label}.capture_id")
    _validate_bounded_int(entry.capture_sequence, f"{label}.capture_sequence", minimum=1)
    _validate_evidence_id(entry.evidence_id, f"{label}.evidence_id")
    _validate_evidence_checksum(entry.source_digest, f"{label}.source_digest")
    length = _require_int(entry.source_length_bytes, f"{label}.source_length_bytes")
    if not (0 <= length <= _DOCUMENT_HISTORY_MAX_SOURCE_BYTES):
        raise ContractSemanticError(
            f"{label}.source_length_bytes: {length} is outside the bounded range "
            f"[0, {_DOCUMENT_HISTORY_MAX_SOURCE_BYTES}]"
        )
    _validate_media_type(entry.media_type, f"{label}.media_type")
    _validate_identifier(
        entry.requested_source_identity, f"{label}.requested_source_identity"
    )
    _validate_closed_code(
        entry.intent_kind, f"{label}.intent_kind", DOCUMENT_CAPTURE_INTENT_KINDS
    )
    _validate_closed_code(
        entry.association_kind, f"{label}.association_kind", DOCUMENT_CAPTURE_ASSOCIATION_KINDS
    )
    disposition = _validate_closed_code(
        entry.capture_disposition, f"{label}.capture_disposition", DOCUMENT_CAPTURE_DISPOSITIONS
    )
    reason = _validate_closed_code(
        entry.disposition_reason, f"{label}.disposition_reason", DOCUMENT_CAPTURE_DISPOSITION_REASONS
    )
    if (disposition == "head_eligible") != (reason == "eligible"):
        raise ContractSemanticError(
            f"{label}: a head-eligible capture is exactly an eligible one, and a "
            "historical-only capture must say which other reason applies"
        )
    current = _require_bool(entry.is_current_source, f"{label}.is_current_source")
    if current != (entry.capture_id == current_capture_id):
        raise ContractSemanticError(
            f"{label}.is_current_source disagrees with source.current_capture_id; which "
            "capture is current is the head's statement and never the sequence's"
        )
    if current and disposition != "head_eligible":
        raise ContractSemanticError(
            f"{label}: a historical-only capture is never the current source"
        )
    _validate_closed_code(
        entry.retention_state, f"{label}.retention_state", DOCUMENT_CAPTURE_RETENTION_STATES
    )
    _validate_bounded_int(
        entry.capture_retention_generation, f"{label}.capture_retention_generation", minimum=1
    )
    _validate_bounded_int(
        entry.source_removal_generation, f"{label}.source_removal_generation", minimum=1
    )
    _require_type(
        entry.requested_profile, DocumentProcessingProfileInput, f"{label}.requested_profile"
    )
    _validate_identifier(
        entry.requested_profile.profile_ref, f"{label}.requested_profile.profile_ref"
    )
    _validate_identifier(
        entry.requested_profile.revision, f"{label}.requested_profile.revision"
    )
    _validate_closed_code(
        entry.processing_state, f"{label}.processing_state", DOCUMENT_CAPTURE_PROCESSING_STATES
    )
    if entry.expected_base_capture_id is not None:
        _validate_identifier(
            entry.expected_base_capture_id, f"{label}.expected_base_capture_id"
        )
        if entry.expected_base_capture_id == entry.capture_id:
            raise ContractSemanticError(
                f"{label}: a capture is never its own frozen base"
            )
    if entry.intent_kind == "new_capture" and entry.expected_base_capture_id is not None:
        raise ContractSemanticError(
            f"{label}: an initial capture froze a creation precondition, not a base"
        )
    _parse_timestamp(entry.captured_at, f"{label}.captured_at")
    if entry.retention_changed_at is not None:
        _parse_timestamp(entry.retention_changed_at, f"{label}.retention_changed_at")
    actions = _require_sequence(entry.actions, f"{label}.actions")
    if len(actions) > _DOCUMENT_HISTORY_MAX_ACTIONS:
        raise ContractSemanticError(
            f"{label}.actions holds {len(actions)} entries, exceeding "
            f"{_DOCUMENT_HISTORY_MAX_ACTIONS}"
        )
    reprocess_available = False
    seen: set[str] = set()
    for index, action in enumerate(actions):
        name, available = _validate_history_action(action, f"{label}.actions[{index}]")
        if name in seen:
            raise ContractSemanticError(f"{label}.actions names {name!r} twice")
        seen.add(name)
        if name == "refresh_original":
            raise ContractSemanticError(
                f"{label}.actions: refresh_original reads the original and is a "
                "document-level action, never a capture-level one"
            )
        if name == "reprocess_captured_copy":
            reprocess_available = available
    options = _require_sequence(entry.reprocess_profiles, f"{label}.reprocess_profiles")
    if len(options) > _DOCUMENT_HISTORY_MAX_ACTIONS:
        raise ContractSemanticError(
            f"{label}.reprocess_profiles holds {len(options)} entries, exceeding "
            f"{_DOCUMENT_HISTORY_MAX_ACTIONS}"
        )
    if options and not reprocess_available:
        raise ContractSemanticError(
            f"{label}.reprocess_profiles offers a selection for an action this result "
            "reports unavailable"
        )
    if reprocess_available and not options:
        raise ContractSemanticError(
            f"{label}.reprocess_profiles is empty while reprocess_captured_copy is "
            "reported available, which would leave a caller no approved profile to name"
        )
    for index, option in enumerate(options):
        _validate_reprocess_profile_option(option, f"{label}.reprocess_profiles[{index}]")


def _validate_extraction_profile_summary(profile: object, label: str) -> None:
    _require_type(profile, DocumentExtractionProfileSummary, label)
    assert isinstance(profile, DocumentExtractionProfileSummary)
    _validate_evidence_checksum(profile.digest, f"{label}.digest")
    build = _require_str(profile.decoder_build, f"{label}.decoder_build")
    if not (1 <= len(build) <= _DECODER_BUILD_MAX_LENGTH):
        raise ContractSemanticError(
            f"{label}.decoder_build length is outside the bounded range "
            f"[1, {_DECODER_BUILD_MAX_LENGTH}]"
        )
    mode = _validate_closed_code(
        profile.processing_mode, f"{label}.processing_mode", DOCUMENT_PROCESSING_MODES
    )
    _require_bool(profile.embedded_image_ocr, f"{label}.embedded_image_ocr")
    languages = _require_sequence(profile.ocr_languages, f"{label}.ocr_languages")
    if len(languages) > _OCR_LANGUAGE_MAX_COUNT:
        raise ContractSemanticError(
            f"{label}.ocr_languages holds {len(languages)} entries, exceeding "
            f"{_OCR_LANGUAGE_MAX_COUNT}"
        )
    for index, language in enumerate(languages):
        _validate_open_code(language, f"{label}.ocr_languages[{index}]")
    if mode == "text_only" and languages:
        raise ContractSemanticError(
            f"{label}: a text-only profile selected no recognition language, so naming "
            "one would claim recognition that never ran"
        )


def _validate_extraction_history_entry(
    entry: object, label: str, *, current_extraction_id: str | None
) -> None:
    _require_type(entry, DocumentExtractionHistoryEntry, label)
    assert isinstance(entry, DocumentExtractionHistoryEntry)
    _validate_identifier(entry.extraction_revision_id, f"{label}.extraction_revision_id")
    _validate_identifier(entry.capture_id, f"{label}.capture_id")
    _validate_bounded_int(entry.document_ordinal, f"{label}.document_ordinal", minimum=1)
    _validate_bounded_int(entry.extraction_sequence, f"{label}.extraction_sequence", minimum=1)
    _validate_extraction_profile_summary(entry.profile, f"{label}.profile")
    if entry.expected_base_extraction_id is not None:
        _validate_identifier(
            entry.expected_base_extraction_id, f"{label}.expected_base_extraction_id"
        )
        if entry.expected_base_extraction_id == entry.extraction_revision_id:
            raise ContractSemanticError(
                f"{label}: an extraction is never its own frozen base"
            )
    eligible = _require_bool(entry.head_eligible, f"{label}.head_eligible")
    current = _require_bool(entry.is_current_extraction, f"{label}.is_current_extraction")
    if current != (entry.extraction_revision_id == current_extraction_id):
        raise ContractSemanticError(
            f"{label}.is_current_extraction disagrees with "
            "extraction.current_extraction_id; which extraction is current is the head's "
            "statement and never the ordinal's"
        )
    if current and not eligible:
        raise ContractSemanticError(
            f"{label}: a revision admitted against a stale base is never promoted, so it "
            "cannot be the current extraction"
        )
    _parse_timestamp(entry.admitted_at, f"{label}.admitted_at")
    terminal = entry.result_state is not None
    if terminal != (entry.publication_disposition is not None):
        raise ContractSemanticError(
            f"{label}: a terminal result states both its outcome and what that outcome "
            "did to retrieval, and a pending one states neither"
        )
    if not terminal:
        for name in ("output_digest", "worker_build", "completed_at"):
            if getattr(entry, name) is not None:
                raise ContractSemanticError(
                    f"{label}.{name} is present while the revision has no terminal result"
                )
        if current:
            raise ContractSemanticError(
                f"{label}: a revision with no terminal result is never the current extraction"
            )
        return
    state = _validate_closed_code(
        entry.result_state, f"{label}.result_state", DOCUMENT_EXTRACTION_RESULT_STATES
    )
    disposition = _validate_closed_code(
        entry.publication_disposition,
        f"{label}.publication_disposition",
        DOCUMENT_PUBLICATION_DISPOSITIONS,
    )
    publishable = state in DOCUMENT_EXTRACTION_PUBLISHABLE_STATES
    if publishable == (disposition == "not_published"):
        raise ContractSemanticError(
            f"{label}: only a succeeded or partial extraction produces something to "
            "publish, and an unsupported or failed one publishes nothing"
        )
    if publishable != (entry.output_digest is not None):
        raise ContractSemanticError(
            f"{label}: a qualified output has a digest and an unqualified one has none"
        )
    if entry.output_digest is not None:
        _validate_evidence_checksum(entry.output_digest, f"{label}.output_digest")
    if current and disposition != "promoted":
        raise ContractSemanticError(
            f"{label}: the current extraction is the promoted one; any other disposition "
            "is historical-only and must not read as current"
        )
    build = _require_str(entry.worker_build, f"{label}.worker_build")
    if not (1 <= len(build) <= _WORKER_BUILD_MAX_LENGTH):
        raise ContractSemanticError(
            f"{label}.worker_build length is outside the bounded range "
            f"[1, {_WORKER_BUILD_MAX_LENGTH}]"
        )
    _parse_timestamp(entry.completed_at, f"{label}.completed_at")


def _validate_source_head_state(source: object, label: str) -> str | None:
    _require_type(source, DocumentSourceHeadState, label)
    assert isinstance(source, DocumentSourceHeadState)
    _validate_closed_code(source.source_state, f"{label}.source_state", DOCUMENT_SOURCE_STATES)
    _validate_bounded_int(
        source.source_removal_generation, f"{label}.source_removal_generation", minimum=1
    )
    sequence = _validate_bounded_int(
        source.capture_head_sequence, f"{label}.capture_head_sequence", minimum=0
    )
    if (source.current_capture_id is None) != (sequence == 0):
        raise ContractSemanticError(
            f"{label}: a document with no successful capture states sequence zero, and one "
            "with a successful capture states that capture's own sequence"
        )
    if source.current_capture_id is not None:
        _validate_identifier(source.current_capture_id, f"{label}.current_capture_id")
    _validate_closed_code(
        source.original_binding_state, f"{label}.original_binding_state", DOCUMENT_BINDING_STATES
    )
    _parse_timestamp(source.updated_at, f"{label}.updated_at")
    return source.current_capture_id


def _validate_extraction_head_state(extraction: object, label: str) -> str | None:
    _require_type(extraction, DocumentExtractionHeadState, label)
    assert isinstance(extraction, DocumentExtractionHeadState)
    ordinal = _validate_bounded_int(
        extraction.extraction_head_ordinal, f"{label}.extraction_head_ordinal", minimum=0
    )
    if (extraction.current_extraction_id is None) != (ordinal == 0):
        raise ContractSemanticError(
            f"{label}: a document with no successful extraction states ordinal zero, and "
            "one with a successful extraction states that revision's own ordinal"
        )
    if extraction.current_extraction_id is not None:
        _validate_identifier(
            extraction.current_extraction_id, f"{label}.current_extraction_id"
        )
    state = _validate_closed_code(
        extraction.retrieval_state, f"{label}.retrieval_state", DOCUMENT_RETRIEVAL_STATES
    )
    if (state == "retrievable") != (extraction.published_at is not None):
        raise ContractSemanticError(
            f"{label}: retrievable means rows are published and states when; `none` means "
            "nothing is published and states no instant"
        )
    if extraction.published_at is not None:
        _parse_timestamp(extraction.published_at, f"{label}.published_at")
    if state == "retrievable" and extraction.current_extraction_id is None:
        raise ContractSemanticError(
            f"{label}: published retrieval rows belong to a promoted extraction, so a "
            "retrievable document names its current extraction"
        )
    return extraction.current_extraction_id


def validate_document_history_result(
    result: object, request: object, expected_document_id: object
) -> None:
    """Raise unless `result` is a truthful `document.history` answer to `request`.

    Three classes of check, and the second two are the ones worth reading. The first is the
    ordinary one: every scalar is in its domain and every page is within the limit the
    request asked for. The second holds the result to the *heads*: a capture or an
    extraction reads as current exactly when the corresponding head names it, so a page
    cannot imply currentness from its own order. The third holds it to itself: an
    unavailable action carries its reason, an offered profile exists only for an available
    action, a pending extraction claims no outcome, and a promoted one is the only kind the
    extraction head may name.
    """
    _require_type(result, DocumentHistoryResult, "result")
    assert isinstance(result, DocumentHistoryResult)
    validated = _validated_history_input(request, "request")
    _validate_identifier(expected_document_id, "expected_document_id")
    _validate_identifier(result.document_id, "result.document_id")
    if result.document_id != expected_document_id:
        raise ContractSemanticError(
            f"result.document_id {result.document_id!r} is not the requested document "
            f"{expected_document_id!r}"
        )
    if result.document_id != validated.document_id:
        raise ContractSemanticError(
            "result.document_id does not name the document the request selected"
        )
    _parse_timestamp(result.created_at, "result.created_at")
    current_capture_id = _validate_source_head_state(result.source, "result.source")
    current_extraction_id = _validate_extraction_head_state(
        result.extraction, "result.extraction"
    )

    limit = (
        validated.limit
        if validated.limit is not None
        else DOCUMENT_HISTORY_MAX_PAGE
    )
    captures = _require_sequence(result.captures, "result.captures")
    extractions = _require_sequence(result.extractions, "result.extractions")
    for name, page in (("captures", captures), ("extractions", extractions)):
        if len(page) > limit:
            raise ContractSemanticError(
                f"result.{name} has {len(page)} entries, exceeding the applicable limit "
                f"of {limit}"
            )
    capture_count = _validate_bounded_int(
        result.capture_count, "result.capture_count", minimum=0
    )
    extraction_count = _validate_bounded_int(
        result.extraction_count, "result.extraction_count", minimum=0
    )
    if len(captures) > capture_count or len(extractions) > extraction_count:
        raise ContractSemanticError(
            "a page may never hold more members than the total it reports"
        )

    sequences: list[int] = []
    for index, entry in enumerate(captures):
        _validate_capture_history_entry(
            entry, f"result.captures[{index}]", current_capture_id=current_capture_id
        )
        assert isinstance(entry, DocumentCaptureHistoryEntry)
        sequences.append(entry.capture_sequence)
    if sequences != sorted(set(sequences)):
        raise ContractSemanticError(
            "result.captures must be a duplicate-free ascending run of capture sequences"
        )

    ordinals: list[int] = []
    for index, entry in enumerate(extractions):
        _validate_extraction_history_entry(
            entry,
            f"result.extractions[{index}]",
            current_extraction_id=current_extraction_id,
        )
        assert isinstance(entry, DocumentExtractionHistoryEntry)
        ordinals.append(entry.document_ordinal)
    if ordinals != sorted(set(ordinals)):
        raise ContractSemanticError(
            "result.extractions must be a duplicate-free ascending run of document ordinals"
        )

    actions = _require_sequence(result.actions, "result.actions")
    if len(actions) > _DOCUMENT_HISTORY_MAX_ACTIONS:
        raise ContractSemanticError(
            f"result.actions holds {len(actions)} entries, exceeding "
            f"{_DOCUMENT_HISTORY_MAX_ACTIONS}"
        )
    seen: set[str] = set()
    for index, action in enumerate(actions):
        name, _ = _validate_history_action(action, f"result.actions[{index}]")
        if name in seen:
            raise ContractSemanticError(f"result.actions names {name!r} twice")
        seen.add(name)
        if name == "reprocess_captured_copy":
            raise ContractSemanticError(
                "result.actions: reprocess_captured_copy names one retained capture and "
                "is reported against that capture, never against the document"
            )
    _validate_history_result_page(result.page, "result.page")
