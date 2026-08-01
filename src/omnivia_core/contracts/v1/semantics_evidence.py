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

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
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
    EvidenceArtifact,
    EvidenceReference,
    EvidenceSearchInput,
    EvidenceSearchResult,
    ProvenanceEntry,
    SourceReference,
    SourceSpan,
)
from omnivia_core.contracts.v1.semantics import (
    validate_page_limit,
    validate_record_temporal_metadata,
)

__all__ = [
    "EVIDENCE_TOMBSTONE_ACTION",
    "decode_evidence_search_input",
    "validate_evidence_artifact",
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

_EVIDENCE_ID_RE: Final = re.compile(EVIDENCE_ID_PATTERN)
_IDENTIFIER_RE: Final = re.compile(IDENTIFIER_PATTERN)
_OPAQUE_TOKEN_RE: Final = re.compile(OPAQUE_TOKEN_PATTERN)
_OPEN_CODE_RE: Final = re.compile(OPEN_CODE_PATTERN)
_WORKSPACE_ID_RE: Final = re.compile(WORKSPACE_ID_PATTERN)
_EVIDENCE_CHECKSUM_RE: Final = re.compile(EVIDENCE_CHECKSUM_PATTERN)
_MEDIA_TYPE_RE: Final = re.compile(MEDIA_TYPE_PATTERN)
_TIMESTAMP_RE: Final = re.compile(TIMESTAMP_PATTERN)


def _require_type(value: object, expected: type | tuple[type, ...], label: str) -> None:
    """Raise unless `value` is an instance of `expected`.

    The one guard every direct-entry-point function in this module runs before doing
    anything else with an argument it did not itself decode: a caller reaching these
    functions without a tolerant `from_wire` decode in front (a hand-built dataclass, a
    plain dict, `None`, a wrong-typed field on an otherwise-valid dataclass) must see a
    `ContractSemanticError`, never a raw `TypeError`/`AttributeError` from whatever this
    module does with `value` next.
    """
    if isinstance(value, bool) and expected is not bool and not (isinstance(expected, tuple) and bool in expected):
        raise ContractSemanticError(f"{label}: expected {expected!r}, got bool")
    if not isinstance(value, expected):
        raise ContractSemanticError(f"{label}: expected {expected!r}, got {type(value).__name__}")


def _require_str(value: object, label: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise ContractSemanticError(f"{label}: expected a string, got {type(value).__name__}")
    return value


def _require_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ContractSemanticError(f"{label}: expected a boolean, got {type(value).__name__}")
    return value


def _require_int(value: object, label: str) -> int:
    """Raise unless `value` is a real integer, and return it.

    `bool` is refused ahead of `int` deliberately: Python makes `True` an integer equal to
    1, so a hand-built `SourceSpan(start_offset=True)` would otherwise satisfy every
    `>= 0` range check and be honoured as an offset of one. It is a JSON boolean, not a
    JSON number, and the schema says `type: integer`.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractSemanticError(f"{label}: expected an integer, got {type(value).__name__}")
    return value


def _require_sequence(value: object, label: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractSemanticError(f"{label}: expected a sequence, got {type(value).__name__}")
    return value


def _require_mapping(value: object, label: str) -> Mapping[object, object]:
    if not isinstance(value, Mapping):
        raise ContractSemanticError(f"{label}: expected a mapping, got {type(value).__name__}")
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
    if len(text) > _EVIDENCE_CHECKSUM_MAX_LENGTH or not _EVIDENCE_CHECKSUM_RE.fullmatch(text):
        raise ContractSemanticError(f"{label}: {text!r} is not a valid EvidenceChecksum")


def _validate_media_type(value: object, label: str) -> None:
    text = _require_str(value, label)
    if len(text) > _MEDIA_TYPE_MAX_LENGTH or not _MEDIA_TYPE_RE.fullmatch(text):
        raise ContractSemanticError(f"{label}: {text!r} is not a valid MediaType")


def _parse_timestamp(value: object, label: str) -> datetime:
    text = _require_str(value, label)
    if not _TIMESTAMP_RE.match(text):
        raise ContractSemanticError(
            f"{label}: {text!r} is not a canonical RFC 3339 UTC timestamp "
            "(naive, offset, and malformed spellings are all rejected)"
        )
    try:
        return datetime.fromisoformat(text)
    except ValueError as error:
        raise ContractSemanticError(f"{label}: {text!r} is not a valid RFC 3339 timestamp") from error


def _validate_source_reference(source: object, label: str) -> tuple[str, str]:
    """Validate `source` and return its `(kind, source_id)` identity key."""
    _require_type(source, SourceReference, label)
    assert isinstance(source, SourceReference)
    _validate_open_code(source.kind, f"{label}.kind")
    _validate_identifier(source.source_id, f"{label}.source_id")
    if source.locator is not None and len(_require_str(source.locator, f"{label}.locator")) > _LOCATOR_MAX_LENGTH:
        raise ContractSemanticError(f"{label}.locator exceeds the maximum length of {_LOCATOR_MAX_LENGTH}")
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
        raise ContractSemanticError(f"{label}.pointer exceeds the maximum length of {_LOCATOR_MAX_LENGTH}")
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
        raise ContractSemanticError(f"{label}.end_offset is before {label}.start_offset")


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
    if reference.excerpt is not None and len(_require_str(reference.excerpt, f"{label}.excerpt")) > _EXCERPT_MAX_LENGTH:
        raise ContractSemanticError(f"{label}.excerpt exceeds the maximum length of {_EXCERPT_MAX_LENGTH}")


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
                reference, f"{label}.evidence[{index}]", artifact_source_key=artifact_source_key
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
        observed_at = _parse_timestamp(artifact.temporal.observed_at, "temporal.observed_at")
        ingested_at = _parse_timestamp(artifact.temporal.ingested_at, "temporal.ingested_at")
        if observed_at > ingested_at:
            raise ContractSemanticError(
                f"temporal.observed_at {artifact.temporal.observed_at!r} is after "
                f"temporal.ingested_at {artifact.temporal.ingested_at!r}"
            )

    _validate_evidence_checksum(artifact.content_checksum, "content_checksum")
    _validate_media_type(artifact.media_type, "media_type")
    _require_mapping(artifact.metadata, "metadata")

    permission_labels = _require_sequence(artifact.permission_labels, "permission_labels")
    if len(permission_labels) > _PERMISSION_LABELS_MAX_ITEMS:
        raise ContractSemanticError(
            f"permission_labels has {len(permission_labels)} entries, exceeding the maximum "
            f"of {_PERMISSION_LABELS_MAX_ITEMS}"
        )
    seen_labels: set[str] = set()
    for index, label in enumerate(permission_labels):
        validated_label = _validate_open_code(label, f"permission_labels[{index}]")
        if validated_label in seen_labels:
            raise ContractSemanticError(f"permission_labels[{index}] duplicates {validated_label!r}")
        seen_labels.add(validated_label)

    _validate_open_code(artifact.sensitivity, "sensitivity")
    _validate_open_code(artifact.parser_status, "parser_status")
    _validate_open_code(artifact.ingestion_status, "ingestion_status")

    tombstoned = _require_bool(artifact.tombstoned, "tombstoned")

    provenance_history = _require_sequence(artifact.provenance_history, "provenance_history")
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
                entry, f"provenance_history[{index}]", artifact_source_key=artifact_source_key
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
        raise ContractSemanticError(f"query length is outside the bounded range [1, {_QUERY_MAX_LENGTH}]")
    if input_.sensitivity is not None:
        _validate_open_code(input_.sensitivity, "sensitivity")
    if input_.include_tombstoned is not None:
        _require_bool(input_.include_tombstoned, "include_tombstoned")
    if input_.limit is not None:
        validate_page_limit(input_.limit)


def decode_evidence_search_input(payload: object, path: str = "EvidenceSearchInput") -> EvidenceSearchInput:
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
        if request.sensitivity is not None and artifact.sensitivity != request.sensitivity:
            raise ContractSemanticError(
                f"{label}.sensitivity {artifact.sensitivity!r} does not match the requested "
                f"sensitivity {request.sensitivity!r}"
            )
