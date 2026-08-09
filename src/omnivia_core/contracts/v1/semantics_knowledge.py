"""Pure semantic validation for the governed-knowledge, graph, and Context Pack DTOs
(`knowledge.search`, `knowledge.propose`, `candidate.approve`, `candidate.reject`,
`record.supersede`, `graph.traverse`, `context_pack.build`; ADR-039).

Structural decoding lives in :mod:`generated`; this module is the same kind of pure,
standard-library-only layer :mod:`semantics` is for the workspace/governed-memory DTOs,
split out here for its own domain-boundary clarity. Every governance-transition result
validator composes one shared reciprocal-supersession check
(:func:`_validate_governance_transition`) so `knowledge.propose`, `candidate.approve`,
`candidate.reject`, and `record.supersede` cannot silently disagree on what "preserve
history, never erase, always reciprocal" means. Nothing here parses or produces JSON,
and nothing here may depend on runtime, storage, HTTP, MCP, CLI, Platform, Dev, or a
validation framework.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from datetime import datetime
from hashlib import sha256
from typing import Final, NamedTuple

from omnivia_core.contracts.v1.canonical_json import (
    canonical_bytes,
    parse_json_document,
    utf16_sort_key,
)
from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.generated import (
    CAPABILITY_ID_PATTERN,
    CONTEXT_PACK_DIGEST_PATTERN,
    CONTEXT_PACK_MODE_PATTERN,
    CONTRACT_VERSION_PATTERN,
    EVIDENCE_CHECKSUM_PATTERN,
    EVIDENCE_DISPOSITION_PATTERN,
    EVIDENCE_ID_PATTERN,
    GOVERNED_RECORD_TYPE_PATTERN,
    GRAPH_DIRECTION_PATTERN,
    IDENTIFIER_PATTERN,
    OPAQUE_TOKEN_PATTERN,
    OPEN_CODE_PATTERN,
    PROJECTION_VERSION_PATTERN,
    PURPOSE_PATTERN,
    RECORD_ID_PATTERN,
    RECORD_VERSION_PATTERN,
    SCOPE_PATTERN,
    TIMESTAMP_PATTERN,
    WORKSPACE_ID_PATTERN,
    CandidateApproveInput,
    CandidateApproveResult,
    CandidateAssertion,
    CandidateExtractionMetadata,
    CandidateRejectInput,
    CandidateRejectResult,
    CapabilityRef,
    ContextPackAuthorizationContext,
    ContextPackAuthorizedCandidateSetManifest,
    ContextPackAuthorizedEvidenceCandidate,
    ContextPackAuthorizedRecordCandidate,
    ContextPackBudget,
    ContextPackBuildInput,
    ContextPackBuildResult,
    ContextPackConflict,
    ContextPackEvidenceCitation,
    ContextPackEvidenceReference,
    ContextPackNormalizedRequest,
    ContextPackRecordCitation,
    ContextPackReproducibility,
    ContextPackSection,
    ContextPackUncertainty,
    EvidenceArtifact,
    EvidenceReference,
    GovernanceRationale,
    GovernedRecord,
    GrantedAuthority,
    GraphEdge,
    GraphNode,
    GraphTraversalInput,
    GraphTraversalResult,
    KnowledgeProposeInput,
    KnowledgeProposeResult,
    KnowledgeSearchInput,
    KnowledgeSearchResult,
    MemoryCreateInput,
    MemorySearchInput,
    MemorySearchResult,
    MutationPrecondition,
    Omission,
    PageMetadata,
    ProjectionFreshness,
    ProvenanceEntry,
    RecordProvenance,
    RecordSupersedeInput,
    RecordSupersedeResult,
    RecordTemporalMetadata,
    RecordVersionReference,
    SourceReference,
    SourceSpan,
)
from omnivia_core.contracts.v1.semantics import (
    AUTHORITY_LEVEL_PROPOSED,
    GOVERNANCE_LAYER_CANDIDATE,
    GOVERNANCE_STATE_ACCEPTED,
    GOVERNANCE_STATE_CANDIDATE,
    GOVERNANCE_STATE_PROPOSED,
    GOVERNANCE_STATE_REJECTED,
    GOVERNED_RECORD_VIEW_CANDIDATES,
    GOVERNED_RECORD_VIEW_CURRENT_CANONICAL,
    GOVERNED_RECORD_VIEW_HISTORY,
    GOVERNED_RECORD_VIEWS,
    RECORD_CURRENTNESS_CURRENT,
    RECORD_CURRENTNESS_SUPERSEDED,
    resolve_governed_record_view,
    validate_governed_record,
    validate_memory_create_input,
    validate_page_limit,
    validate_record_domain_scope,
)
from omnivia_core.contracts.v1.semantics_evidence import validate_evidence_artifact

__all__ = [
    "CONTEXT_PACK_ARTIFACT_CANONICALIZATION",
    "CONTEXT_PACK_AUTHORIZED_CANDIDATE_SET_FORMAT",
    "CONTEXT_PACK_CANDIDATE_PARTITIONS",
    "CONTEXT_PACK_CANDIDATE_PARTITION_CONTEXT_MODELS",
    "CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE",
    "CONTEXT_PACK_CANDIDATE_PARTITION_HISTORY",
    "CONTEXT_PACK_CANDIDATE_PARTITION_RECORDS",
    "CONTEXT_PACK_DIGEST_ALGORITHM",
    "CONTEXT_PACK_FORMAT_VERSION",
    "CONTEXT_PACK_GOVERNED_CANDIDATE_PARTITIONS",
    "CONTEXT_PACK_MAX_TOKEN_BUDGET",
    "CONTEXT_PACK_MAX_TOKEN_COUNT",
    "CONTEXT_PACK_MIN_TOKEN_BUDGET",
    "CONTEXT_PACK_MODES",
    "CONTEXT_PACK_MODE_DETERMINISTIC_VIEW",
    "CONTEXT_PACK_NORMALIZED_REQUEST_VIEW",
    "CONTEXT_PACK_REJECTED_INPUT_FIELDS",
    "CONTEXT_PACK_REJECTED_MODES",
    "CONTEXT_PACK_SUMMARIZER_DISABLED",
    "GOVERNANCE_ACTIONS",
    "GOVERNANCE_ACTION_CANDIDATE_APPROVE",
    "GOVERNANCE_ACTION_CANDIDATE_REJECT",
    "GOVERNANCE_ACTION_KNOWLEDGE_PROPOSE",
    "GOVERNANCE_ACTION_RECORD_SUPERSEDE",
    "GOVERNANCE_AUTHORITY_LEVEL_CANONICAL",
    "GOVERNANCE_AUTHORITY_LEVEL_REJECTED",
    "GOVERNANCE_LAYER_CONTEXT_MODEL",
    "GOVERNANCE_LAYER_GOVERNED",
    "GOVERNANCE_TRANSITION_RESERVED_INPUT_FIELDS",
    "GRAPH_BOUNDARY_REASONS",
    "GRAPH_BOUNDARY_REASON_DEPTH",
    "GRAPH_BOUNDARY_REASON_PAGE",
    "GRAPH_DEFAULT_DEPTH_LIMIT",
    "GRAPH_DIRECTIONS",
    "GRAPH_DIRECTION_DEFAULT",
    "GRAPH_MAX_DEPTH_LIMIT",
    "GRAPH_MAX_RELATION_TYPE_FILTERS",
    "GRAPH_MAX_START_REFERENCES",
    "GRAPH_MIN_DEPTH_LIMIT",
    "GRAPH_MIN_RELATION_TYPE_FILTERS",
    "GRAPH_MIN_START_REFERENCES",
    "GRAPH_ORDERING_BASES",
    "GRAPH_ORDERING_BASIS_DEPTH_RECORD_VERSION_ASC",
    "KNOWLEDGE_SEARCH_CANONICAL_AUTHORITY_LEVEL",
    "KNOWLEDGE_SEARCH_DEFAULT_RESULT_LIMIT",
    "KNOWLEDGE_SEARCH_ORDERS",
    "KNOWLEDGE_SEARCH_ORDER_RECENCY",
    "KNOWLEDGE_SEARCH_ORDER_RELEVANCE",
    "compute_authorized_candidate_set_checksum",
    "compute_context_pack_artifact_digest",
    "decode_candidate_approve_input",
    "decode_candidate_reject_input",
    "decode_context_pack_build_input",
    "decode_knowledge_propose_input",
    "decode_knowledge_search_input",
    "decode_record_supersede_input",
    "resolve_graph_direction",
    "validate_candidate_approve_input",
    "validate_candidate_approve_result",
    "validate_candidate_reject_input",
    "validate_candidate_reject_result",
    "validate_context_pack_build_input",
    "validate_context_pack_build_result",
    "validate_context_pack_build_result_document",
    "validate_context_pack_citation",
    "validate_graph_traversal_input",
    "validate_graph_traversal_result",
    "validate_knowledge_propose_input",
    "validate_knowledge_propose_result",
    "validate_knowledge_search_input",
    "validate_knowledge_search_result",
    "validate_memory_search_input",
    "validate_memory_search_result",
    "validate_projection_freshness",
    "validate_record_supersede_input",
    "validate_record_supersede_result",
    "verify_context_pack_artifact_document",
]

_BOUNDED_VALUE_MAX_LENGTH: Final = 128
_OPAQUE_TOKEN_MAX_LENGTH: Final = 512
_RECORD_VERSION_MAX_LENGTH: Final = 512
_COMMENT_MAX_LENGTH: Final = 2048
_DESCRIPTION_MAX_LENGTH: Final = 4096
_LOCATOR_MAX_LENGTH: Final = 2048

_IDENTIFIER_RE: Final = re.compile(IDENTIFIER_PATTERN)
_OPEN_CODE_RE: Final = re.compile(OPEN_CODE_PATTERN)
_WORKSPACE_ID_RE: Final = re.compile(WORKSPACE_ID_PATTERN)
_RECORD_ID_RE: Final = re.compile(RECORD_ID_PATTERN)
_RECORD_VERSION_RE: Final = re.compile(RECORD_VERSION_PATTERN)
_OPAQUE_TOKEN_RE: Final = re.compile(OPAQUE_TOKEN_PATTERN)
_PURPOSE_RE: Final = re.compile(PURPOSE_PATTERN)
_EVIDENCE_DISPOSITION_RE: Final = re.compile(EVIDENCE_DISPOSITION_PATTERN)
_PROJECTION_VERSION_RE: Final = re.compile(PROJECTION_VERSION_PATTERN)
_TIMESTAMP_RE: Final = re.compile(TIMESTAMP_PATTERN)
_GRAPH_DIRECTION_RE: Final = re.compile(GRAPH_DIRECTION_PATTERN)
_CONTEXT_PACK_MODE_RE: Final = re.compile(CONTEXT_PACK_MODE_PATTERN)


def _require_type(value: object, expected: type | tuple[type, ...], label: str) -> None:
    """Raise unless `value` is an instance of `expected`.

    The guard every direct-entry-point function in this module runs before doing anything
    else with an argument it did not itself decode: a caller reaching these functions
    without a tolerant `from_wire` decode in front (a hand-built dataclass, a plain dict,
    `None`, a wrong-typed field on an otherwise-valid dataclass) must see a
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


def _require_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractSemanticError(f"{label}: expected an integer, got {type(value).__name__}")
    return value


def _require_sequence(value: object, label: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractSemanticError(f"{label}: expected a sequence, got {type(value).__name__}")
    return value


def _require_mapping(value: object, label: str) -> Mapping[str, object]:
    """Raise unless `value` is a mapping, so a hand-built DTO carrying a list, a string, or
    `None` where an open map belongs fails as a `ContractSemanticError` rather than as a raw
    `AttributeError` from the first `.items()` call."""
    if not isinstance(value, Mapping):
        raise ContractSemanticError(f"{label}: expected a mapping, got {type(value).__name__}")
    mapping: Mapping[str, object] = value
    return mapping


def _require_set_of_str(value: object, label: str) -> AbstractSet[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, AbstractSet):
        raise ContractSemanticError(f"{label}: expected a set, got {type(value).__name__}")
    for item in value:
        if isinstance(item, bool) or not isinstance(item, str):
            raise ContractSemanticError(
                f"{label}: every member must be a string, got {type(item).__name__}"
            )
    return value


def _validate_identifier(value: str, label: str) -> None:
    if len(value) > _BOUNDED_VALUE_MAX_LENGTH or not _IDENTIFIER_RE.fullmatch(value):
        raise ContractSemanticError(f"{label}: {value!r} is not a valid Identifier")


def _validate_open_code(value: str, label: str) -> None:
    if len(value) > _BOUNDED_VALUE_MAX_LENGTH or not _OPEN_CODE_RE.fullmatch(value):
        raise ContractSemanticError(f"{label}: {value!r} is not a valid OpenCode")


def _validate_workspace_id(value: str, label: str) -> None:
    if len(value) > _BOUNDED_VALUE_MAX_LENGTH or not _WORKSPACE_ID_RE.fullmatch(value):
        raise ContractSemanticError(f"{label}: {value!r} is not a valid WorkspaceId")


def _validate_record_id(value: str, label: str) -> None:
    if len(value) > _BOUNDED_VALUE_MAX_LENGTH or not _RECORD_ID_RE.fullmatch(value):
        raise ContractSemanticError(f"{label}: {value!r} is not a valid RecordId")


def _validate_record_version(value: str, label: str) -> None:
    if len(value) > _RECORD_VERSION_MAX_LENGTH or not _RECORD_VERSION_RE.fullmatch(value):
        raise ContractSemanticError(f"{label}: {value!r} is not a valid RecordVersion")


def _validate_opaque_token(value: str, label: str) -> None:
    if len(value) > _OPAQUE_TOKEN_MAX_LENGTH or not _OPAQUE_TOKEN_RE.fullmatch(value):
        raise ContractSemanticError(f"{label}: {value!r} is not a valid OpaqueToken")


def _validate_projection_version(value: str, label: str) -> None:
    if not (1 <= len(value) <= _BOUNDED_VALUE_MAX_LENGTH) or not _PROJECTION_VERSION_RE.fullmatch(value):
        raise ContractSemanticError(f"{label}: {value!r} is not a valid ProjectionVersion")


def _validate_purpose(value: str, label: str) -> None:
    if len(value) > _BOUNDED_VALUE_MAX_LENGTH or not _PURPOSE_RE.fullmatch(value):
        raise ContractSemanticError(f"{label}: {value!r} is not a valid Purpose")


def _validate_evidence_disposition_code(value: str, label: str) -> None:
    if len(value) > _BOUNDED_VALUE_MAX_LENGTH or not _EVIDENCE_DISPOSITION_RE.fullmatch(value):
        raise ContractSemanticError(f"{label}: {value!r} is not a valid EvidenceDisposition")


def _parse_timestamp(value: str, label: str) -> datetime:
    if not _TIMESTAMP_RE.match(value):
        raise ContractSemanticError(
            f"{label}: {value!r} is not a canonical RFC 3339 UTC timestamp "
            "(naive, offset, and malformed spellings are all rejected)"
        )
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise ContractSemanticError(f"{label}: {value!r} is not a valid RFC 3339 timestamp") from error


def _validate_source_reference(source: object, label: str) -> None:
    _require_type(source, SourceReference, label)
    assert isinstance(source, SourceReference)
    _validate_open_code(_require_str(source.kind, f"{label}.kind"), f"{label}.kind")
    _validate_identifier(_require_str(source.source_id, f"{label}.source_id"), f"{label}.source_id")
    if source.locator is not None and len(_require_str(source.locator, f"{label}.locator")) > _LOCATOR_MAX_LENGTH:
        raise ContractSemanticError(f"{label}.locator exceeds the maximum length of {_LOCATOR_MAX_LENGTH}")
    if source.retrieved_at is not None:
        _parse_timestamp(_require_str(source.retrieved_at, f"{label}.retrieved_at"), f"{label}.retrieved_at")


def _validate_record_version_reference(reference: object, label: str) -> tuple[str, str]:
    """Raise unless `reference` is a shape-valid `RecordVersionReference`, and return the
    `(record_id, version)` identity it names."""
    _require_type(reference, RecordVersionReference, label)
    assert isinstance(reference, RecordVersionReference)
    record_id = _require_str(reference.record_id, f"{label}.record_id")
    version = _require_str(reference.version, f"{label}.version")
    _validate_record_id(record_id, f"{label}.record_id")
    _validate_record_version(version, f"{label}.version")
    return (record_id, version)


def _validate_governance_rationale(rationale: object, label: str = "rationale") -> None:
    """Raise unless `rationale` is a `GovernanceRationale` whose `reason_code` is a valid
    `OpenCode` and whose optional `comment` is within its bounded length."""
    _require_type(rationale, GovernanceRationale, label)
    assert isinstance(rationale, GovernanceRationale)
    _validate_open_code(_require_str(rationale.reason_code, f"{label}.reason_code"), f"{label}.reason_code")
    if rationale.comment is not None and len(_require_str(rationale.comment, f"{label}.comment")) > _COMMENT_MAX_LENGTH:
        raise ContractSemanticError(f"{label}.comment exceeds the maximum length of {_COMMENT_MAX_LENGTH}")


# --- governance mutation inputs: no server-owned field may be smuggled in -----

GOVERNANCE_TRANSITION_RESERVED_INPUT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "workspace_id",
        "governance_state",
        "layer",
        "currentness",
        "authority_level",
        "reviewer",
        "version",
        "supersedes",
        "superseded_by",
        "superseded_at",
        "provenance",
        "identity",
        "history",
        "temporal",
        "ingested_at",
        "recorded_at",
    }
)
"""Server-owned governance/identity/temporal fields no governance-mutation input on this
module may accept from a caller.

Mirrors :data:`~omnivia_core.contracts.v1.semantics.MEMORY_CREATE_RESERVED_INPUT_FIELDS`'s
purpose: every one of `KnowledgeProposeInput`/`CandidateApproveInput`/`CandidateRejectInput`/
`RecordSupersedeInput`'s JSON Schemas already excludes these (`unevaluatedProperties: false`),
but the tolerant generated decoder production actually calls ignores unknown fields by
design, so a raw payload smuggling one of these names would otherwise decode successfully
instead of failing loudly.
"""


_OPAQUE_CONTENT_FIELD_NAME: Final = "content"
"""The one field name this recursive scan never descends into: opaque, caller-supplied
application content (e.g. `RecordSupersedeInput.content`) may legitimately contain any
key, including one that happens to spell a reserved governance field name, and that is
never itself a smuggling attempt against this contract's own governance fields."""


def _reject_reserved_governance_fields(payload: object, path: str) -> None:
    """Raise unless no reserved server-owned governance field name
    (:data:`GOVERNANCE_TRANSITION_RESERVED_INPUT_FIELDS`) appears anywhere in `payload`,
    at any nesting depth.

    A governance-mutation input's own JSON Schema already excludes these fields
    (`unevaluatedProperties: false`), but the tolerant decoder production this contract
    ignores unknown fields by design, so a raw payload smuggling a reserved name -- at the
    top level, or nested inside e.g. `rationale` or a `sources` entry -- would otherwise
    decode successfully instead of failing loudly. Recursion never descends into
    :data:`_OPAQUE_CONTENT_FIELD_NAME`: that field's value is opaque application content,
    not a nested governance shape this contract can reason about.
    """
    if not isinstance(payload, Mapping):
        return
    present = sorted(GOVERNANCE_TRANSITION_RESERVED_INPUT_FIELDS & set(payload))
    if present:
        raise ContractSemanticError(
            f"{path}: reserved server-owned field(s) not accepted: {present}"
        )
    for key, value in payload.items():
        if key == _OPAQUE_CONTENT_FIELD_NAME:
            continue
        nested_path = f"{path}.{key}"
        if isinstance(value, Mapping):
            _reject_reserved_governance_fields(value, nested_path)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for index, item in enumerate(value):
                if isinstance(item, Mapping):
                    _reject_reserved_governance_fields(item, f"{nested_path}[{index}]")


def validate_knowledge_propose_input(candidate: object) -> None:
    """Raise unless `candidate` is a shape-valid `knowledge.propose` input.

    `rationale` is required, exactly as it is on the other three governance transitions:
    every governance decision this contract records carries an explicit, auditable reason,
    and `knowledge.propose` is not an exception merely because it escalates no authority.
    """
    _require_type(candidate, KnowledgeProposeInput, "candidate")
    assert isinstance(candidate, KnowledgeProposeInput)
    _validate_record_id(_require_str(candidate.record_id, "record_id"), "record_id")
    _validate_governance_rationale(candidate.rationale)


def decode_knowledge_propose_input(
    payload: object, path: str = "KnowledgeProposeInput"
) -> KnowledgeProposeInput:
    """Decode and fully validate `payload` into a semantically valid `KnowledgeProposeInput`."""
    _reject_reserved_governance_fields(payload, path)
    candidate = KnowledgeProposeInput.from_wire(payload, path)
    validate_knowledge_propose_input(candidate)
    return candidate


def validate_candidate_approve_input(candidate: object) -> None:
    """Raise unless `candidate` is a shape-valid `candidate.approve` input."""
    _require_type(candidate, CandidateApproveInput, "candidate")
    assert isinstance(candidate, CandidateApproveInput)
    _validate_record_id(_require_str(candidate.record_id, "record_id"), "record_id")
    _validate_governance_rationale(candidate.rationale)


def decode_candidate_approve_input(
    payload: object, path: str = "CandidateApproveInput"
) -> CandidateApproveInput:
    """Decode and fully validate `payload` into a semantically valid `CandidateApproveInput`."""
    _reject_reserved_governance_fields(payload, path)
    candidate = CandidateApproveInput.from_wire(payload, path)
    validate_candidate_approve_input(candidate)
    return candidate


def validate_candidate_reject_input(candidate: object) -> None:
    """Raise unless `candidate` is a shape-valid `candidate.reject` input."""
    _require_type(candidate, CandidateRejectInput, "candidate")
    assert isinstance(candidate, CandidateRejectInput)
    _validate_record_id(_require_str(candidate.record_id, "record_id"), "record_id")
    _validate_governance_rationale(candidate.rationale)


def decode_candidate_reject_input(
    payload: object, path: str = "CandidateRejectInput"
) -> CandidateRejectInput:
    """Decode and fully validate `payload` into a semantically valid `CandidateRejectInput`."""
    _reject_reserved_governance_fields(payload, path)
    candidate = CandidateRejectInput.from_wire(payload, path)
    validate_candidate_reject_input(candidate)
    return candidate


def _reject_duplicate_evidence_references(evidence: Sequence[object], label: str) -> None:
    """Raise unless no two entries in `evidence` are the same complete `EvidenceReference`.

    Compared by full dataclass equality, not by source key: two references to the same
    source at different spans are two distinct pieces of evidence and are both kept, while
    the same span/excerpt listed twice is a duplicate. Compared against a list rather than a
    set so nothing here depends on `EvidenceReference` staying hashable.
    """
    seen: list[object] = []
    for index, item in enumerate(evidence):
        if item in seen:
            raise ContractSemanticError(
                f"{label}[{index}] duplicates an already-declared evidence reference"
            )
        seen.append(item)


def _validate_replacement_claim(replacement: object, label: str = "replacement") -> MemoryCreateInput:
    """Raise unless `replacement` is a complete, internally coherent replacement claim.

    `RecordSupersedeInput.replacement` is a whole `MemoryCreateInput`, so it gets exactly
    the semantic validation a `memory.create` proposal gets
    (:func:`~omnivia_core.contracts.v1.semantics.validate_memory_create_input`: domain-scope
    and record-type shape, evidence-disposition/source agreement, duplicate-source
    rejection, assertion actor/role/time/validity coherence, evidence-to-declared-source
    agreement, and extraction identifier/timestamp/confidence bounds), plus the three things
    a nested, caller-supplied replacement needs that a top-level `memory.create` decode does
    not:

    - every field this function or `validate_memory_create_input` reaches into is
      type-guarded first, so a hand-built dataclass carrying a wrongly typed field raises
      `ContractSemanticError` rather than a raw `TypeError`/`AttributeError` -- including
      `content`, which must be a mapping and which `memory.create`'s own validator never
      inspects;
    - every piece of assertion evidence must point at a source that *exactly* equals one of
      the declared `sources` entries -- complete dataclass equality, locator and
      `retrieved_at` included -- rather than merely agreeing on `(kind, source_id)`, and no
      two pieces of evidence may be the same complete reference
      (:func:`_reject_duplicate_evidence_references`);
    - `event_at` must not be after `observed_at` when both are supplied: a fact cannot be
      observed before it occurred.
    """
    _require_type(replacement, MemoryCreateInput, label)
    assert isinstance(replacement, MemoryCreateInput)
    _require_str(replacement.record_type, f"{label}.record_type")
    _require_str(replacement.domain_scope, f"{label}.domain_scope")
    _require_type(replacement.content, Mapping, f"{label}.content")
    _validate_evidence_disposition_code(
        _require_str(replacement.evidence_disposition, f"{label}.evidence_disposition"),
        f"{label}.evidence_disposition",
    )
    sources = _require_sequence(replacement.sources, f"{label}.sources")
    for index, source in enumerate(sources):
        _validate_source_reference(source, f"{label}.sources[{index}]")

    assertion = replacement.assertion
    _require_type(assertion, CandidateAssertion, f"{label}.assertion")
    assert isinstance(assertion, CandidateAssertion)
    _require_str(assertion.actor_id, f"{label}.assertion.actor_id")
    _require_str(assertion.actor_kind, f"{label}.assertion.actor_kind")
    _require_str(assertion.actor_role, f"{label}.assertion.actor_role")
    _require_str(assertion.asserted_at, f"{label}.assertion.asserted_at")
    evidence = _require_sequence(assertion.evidence, f"{label}.assertion.evidence")
    for index, item in enumerate(evidence):
        _require_type(item, EvidenceReference, f"{label}.assertion.evidence[{index}]")
    if replacement.extraction is not None:
        _require_type(replacement.extraction, CandidateExtractionMetadata, f"{label}.extraction")

    validate_memory_create_input(replacement)

    declared = tuple(replacement.sources)
    for index, item in enumerate(assertion.evidence):
        if item.source not in declared:
            raise ContractSemanticError(
                f"{label}.assertion.evidence[{index}].source does not exactly match any "
                f"declared {label}.sources entry"
            )
    _reject_duplicate_evidence_references(assertion.evidence, f"{label}.assertion.evidence")

    if replacement.event_at is not None and replacement.observed_at is not None:
        event_at = _parse_timestamp(replacement.event_at, f"{label}.event_at")
        observed_at = _parse_timestamp(replacement.observed_at, f"{label}.observed_at")
        if event_at > observed_at:
            raise ContractSemanticError(
                f"{label}.event_at {replacement.event_at!r} is after {label}.observed_at "
                f"{replacement.observed_at!r}"
            )
    return replacement


def validate_record_supersede_input(candidate: object) -> None:
    """Raise unless `candidate` is an internally coherent `record.supersede` input.

    The input is exactly three fields -- which record, the complete replacement claim, and
    why -- so this is exactly three checks: a valid `RecordId`, a fully valid replacement
    claim (:func:`_validate_replacement_claim`), and a valid `GovernanceRationale`. The
    replacement supplies the new claim's content and its complete assertion/extraction/
    evidence lineage; nothing about the superseded record's own type or domain scope is
    checked here, because that comparison needs the prior record and so belongs to
    :func:`validate_record_supersede_result`.
    """
    _require_type(candidate, RecordSupersedeInput, "candidate")
    assert isinstance(candidate, RecordSupersedeInput)
    _validate_record_id(_require_str(candidate.record_id, "record_id"), "record_id")
    _validate_replacement_claim(candidate.replacement)
    _validate_governance_rationale(candidate.rationale)


def decode_record_supersede_input(
    payload: object, path: str = "RecordSupersedeInput"
) -> RecordSupersedeInput:
    """Decode and fully validate `payload` into a semantically valid `RecordSupersedeInput`."""
    _reject_reserved_governance_fields(payload, path)
    candidate = RecordSupersedeInput.from_wire(payload, path)
    validate_record_supersede_input(candidate)
    return candidate


# --- knowledge.search: fail closed on the default canonical view -------------

GOVERNANCE_LAYER_GOVERNED: Final = "l2"
"""The known governed/canonical layer: accepted knowledge, as opposed to
:data:`~omnivia_core.contracts.v1.semantics.GOVERNANCE_LAYER_CANDIDATE` (`l1`)."""

GOVERNANCE_LAYER_CONTEXT_MODEL: Final = "l3"
"""The known context-model layer: canonical knowledge *about* the workspace's own model,
built on `l2` rather than alongside it. A distinct namespace, not a weaker one -- a
`context_pack.build` `context_models` entry is held to exactly the same accepted, canonical,
reviewed, currently-valid bar an `l2` record is (:func:`_validate_selected_record`)."""

KNOWLEDGE_SEARCH_CANONICAL_AUTHORITY_LEVEL: Final = "canonical"
"""The only `authority_level` a default/`current_canonical`-view `knowledge.search` result
record may carry. Distinct from, and stricter than, the `accepted` `governance_state` that
same record must also carry: `governance_state` describes the record's position in the
governance workflow, `authority_level` asserts what a caller may treat it as, and only
`canonical` asserts the record is settled, citable knowledge -- an `accepted` record still
carrying e.g. `reviewed` authority has not yet reached that bar."""

_QUERY_MIN_LENGTH: Final = 1
_QUERY_MAX_LENGTH: Final = 4096
"""`MemoryQuery`'s schema `minLength`/`maxLength`."""

KNOWLEDGE_SEARCH_ORDER_RELEVANCE: Final = "relevance"
KNOWLEDGE_SEARCH_ORDER_RECENCY: Final = "recency"

KNOWLEDGE_SEARCH_ORDERS: Final[frozenset[str]] = frozenset(
    {KNOWLEDGE_SEARCH_ORDER_RELEVANCE, KNOWLEDGE_SEARCH_ORDER_RECENCY}
)
"""The only known `MemorySearchOrder` values `knowledge.search` recognizes. `MemorySearchOrder`
is a wire-open vocabulary, but an order this build does not recognize cannot be honoured or
verified, so it fails closed here rather than being passed through -- mirroring
:data:`GRAPH_ORDERING_BASES`."""

KNOWLEDGE_SEARCH_DEFAULT_RESULT_LIMIT: Final = 500
"""`KnowledgeSearchResult.records`'s schema `maxItems`: the ceiling applied when the request
carries no explicit `limit`."""


def _validate_view_selector(view: str | None, label: str = "view") -> None:
    """Raise unless `view` is absent or one of the known `GovernedRecordView` values.

    `GovernedRecordView` is wire-open, but the view selector is trust-sensitive: an
    unrecognized value could silently mean anything, including a wider view than the
    caller intended, so it fails closed here rather than being passed through.
    """
    if view is not None and view not in GOVERNED_RECORD_VIEWS:
        raise ContractSemanticError(
            f"{label} {view!r} is not a recognized GovernedRecordView; must be one of "
            f"{sorted(GOVERNED_RECORD_VIEWS)!r} or absent"
        )


def _validate_order_selector(
    order: str | None, label: str = "order", operation: str = "knowledge.search"
) -> None:
    """Raise unless `order` is absent or one of the known `MemorySearchOrder` values
    :data:`KNOWLEDGE_SEARCH_ORDERS` names.

    Mirrors :func:`_validate_view_selector`: `MemorySearchOrder` is wire-open, but an
    unrecognized order cannot be honoured or verified by this build, so it fails closed
    rather than being passed through. `operation` names the refused read because
    `MemorySearchOrder` is the single declared order domain that both `knowledge.search`
    and `memory.search` draw from, so the value alone does not say which one refused.
    """
    if order is not None and order not in KNOWLEDGE_SEARCH_ORDERS:
        raise ContractSemanticError(
            f"{label} {order!r} is not a recognized MemorySearchOrder for {operation}; "
            f"must be one of {sorted(KNOWLEDGE_SEARCH_ORDERS)!r} or absent"
        )


def _validate_governed_search_input(
    input_: KnowledgeSearchInput | MemorySearchInput, operation: str
) -> None:
    """The input rules `knowledge.search` and `memory.search` share exactly.

    `MemorySearchInput` is `KnowledgeSearchInput` minus `domain_scope`
    (`memory.schema.json`), so every field the two do share is governed by one rule here
    rather than by two copies that can drift. `domain_scope` stays with the caller that
    actually has the field.
    """
    query = _require_str(input_.query, "query")
    if not (_QUERY_MIN_LENGTH <= len(query) <= _QUERY_MAX_LENGTH):
        raise ContractSemanticError(
            f"query length is outside the bounded range [{_QUERY_MIN_LENGTH}, {_QUERY_MAX_LENGTH}]"
        )
    if input_.order is not None:
        _validate_order_selector(_require_str(input_.order, "order"), operation=operation)
    if input_.view is not None:
        _validate_view_selector(_require_str(input_.view, "view"))
    if input_.record_type is not None:
        _validate_open_code(_require_str(input_.record_type, "record_type"), "record_type")
    if input_.limit is not None:
        validate_page_limit(_require_int(input_.limit, "limit"))


def validate_knowledge_search_input(input_: object) -> None:
    """Raise unless `input_` is a structurally and semantically valid `knowledge.search`
    input: bounded `query` length, a recognized `order`/`view` when present (both fail
    closed on an unrecognized value, since neither can be honoured or verified otherwise),
    a valid `record_type`/`domain_scope` when present, and a bounded `limit` when present.

    A direct entry point: `input_` need not have passed through
    :func:`decode_knowledge_search_input` first, so every field access is guarded and a
    wrongly typed argument raises `ContractSemanticError`, never a raw
    `TypeError`/`AttributeError`.
    """
    _require_type(input_, KnowledgeSearchInput, "input_")
    assert isinstance(input_, KnowledgeSearchInput)
    _validate_governed_search_input(input_, "knowledge.search")
    if input_.domain_scope is not None:
        validate_record_domain_scope(_require_str(input_.domain_scope, "domain_scope"))


def validate_memory_search_input(input_: object) -> None:
    """Raise unless `input_` is a structurally and semantically valid `memory.search` input.

    The same rules :func:`validate_knowledge_search_input` applies, minus `domain_scope`,
    which `MemorySearchInput` does not carry. In particular the `view` selector fails closed
    on an unrecognized value, so `candidates` and `history` can only ever be reached by
    naming one of the two exactly -- an absent `view` resolves to `current_canonical`
    (:func:`~omnivia_core.contracts.v1.semantics.resolve_governed_record_view`) and a
    misspelling is refused rather than silently defaulted into the canonical view.

    A direct entry point, like its `knowledge.search` counterpart: every field access is
    guarded, so a wrongly typed argument raises `ContractSemanticError` rather than a raw
    `TypeError`/`AttributeError`.
    """
    _require_type(input_, MemorySearchInput, "input_")
    assert isinstance(input_, MemorySearchInput)
    _validate_governed_search_input(input_, "memory.search")


def decode_knowledge_search_input(
    payload: object, path: str = "KnowledgeSearchInput"
) -> KnowledgeSearchInput:
    """Decode and fully validate `payload` into a semantically valid `KnowledgeSearchInput`.

    `knowledge.search` is a read operation and carries no server-owned field a caller could
    smuggle in, so unlike the governance-mutation `decode_*` functions this does not call
    :func:`_reject_reserved_governance_fields`.
    """
    input_ = KnowledgeSearchInput.from_wire(payload, path)
    validate_knowledge_search_input(input_)
    return input_


def _validate_record_valid_at_canonical_resolution_time(
    temporal: RecordTemporalMetadata, resolution_instant: datetime, label: str
) -> None:
    """Raise unless `temporal`'s `valid_from`/`valid_until` window (either bound may be
    absent and unbounded on that side) actually contains `resolution_instant`."""
    if temporal.valid_from is not None:
        valid_from = _parse_timestamp(
            _require_str(temporal.valid_from, f"{label}.valid_from"), f"{label}.valid_from"
        )
        if resolution_instant < valid_from:
            raise ContractSemanticError(
                f"{label} is not yet valid at the canonical-resolution time: "
                f"valid_from {temporal.valid_from!r} is after the resolution instant"
            )
    if temporal.valid_until is not None:
        valid_until = _parse_timestamp(
            _require_str(temporal.valid_until, f"{label}.valid_until"), f"{label}.valid_until"
        )
        if resolution_instant > valid_until:
            raise ContractSemanticError(
                f"{label} is no longer valid at the canonical-resolution time: "
                f"valid_until {temporal.valid_until!r} is before the resolution instant"
            )


def _validate_record_under_history_view(
    record: GovernedRecord, resolution_instant: datetime, label: str
) -> None:
    """Raise unless `record` is exactly what the `history` view permits: a version that was
    *canonical knowledge* and had already been replaced by `resolution_instant`.

    The one place this rule is stated, shared by `knowledge.search`'s history branch
    (:func:`validate_knowledge_search_result`) and `graph.traverse`'s
    (:func:`_validate_graph_record_under_view`), exactly as
    :func:`_validate_governance_transition` is the one place the reciprocity rule is stated:
    the two operations return the same governed versions through different projections, so a
    predicate spelled twice is a predicate that can drift, and a traversal must never hand
    back under `history` what a search would refuse.

    Four things, all exact and all fail-closed, in order:

    - the record sits at exactly `l2`/`accepted`/`superseded`. A candidate version, a
      still-current one, or an unrecognized open value on any axis is not history;
    - `authority_level` is exactly :data:`KNOWLEDGE_SEARCH_CANONICAL_AUTHORITY_LEVEL`.
      History is what *was* canonical, so it is held to the same authority bar the
      `current_canonical` view holds a live record to: an `accepted`-but-never-canonical
      version was never citable knowledge, and being superseded does not promote it;
    - a `reviewer` is present, and `superseded_at` is present. Both are defence in depth,
      not the only line: on every path into this helper
      :func:`~omnivia_core.contracts.v1.semantics.validate_governed_record` has already run
      and already refuses an `accepted` record with no reviewer and a `superseded` one with
      no `superseded_at`, so neither guard is reachable today. They are stated anyway so
      this helper states the complete history bar on its own, rather than leaving a reader
      to reconstruct which axis some other validator happens to cover -- and so that
      loosening one of those universal rules cannot silently widen this view;
    - `superseded_at` is not *after* `resolution_instant`. This is the check that makes the
      view honest, and the one nothing else covers: a version replaced only after the
      instant the read resolved at was still the canonical answer at that instant, so
      returning it as history misstates what the workspace knew then. Equality is accepted
      -- a version superseded exactly at the resolution instant is already history at it.

    Deliberately *not* checked: the `valid_from`/`valid_until` containment
    :func:`_validate_record_valid_at_canonical_resolution_time` applies under
    `current_canonical`. A historical version's validity window is expected to have closed;
    requiring it to contain the resolution instant would reject almost every real
    historical record. Supersession, not validity, is what places a version in the past.
    """
    identity = record.provenance.identity
    if (
        identity.layer != GOVERNANCE_LAYER_GOVERNED
        or identity.governance_state != GOVERNANCE_STATE_ACCEPTED
        or identity.currentness != RECORD_CURRENTNESS_SUPERSEDED
    ):
        raise ContractSemanticError(
            f"{label} is not an exact l2/accepted/superseded historical record "
            f"(layer={identity.layer!r}, governance_state={identity.governance_state!r}, "
            f"currentness={identity.currentness!r}) under the history view"
        )
    if record.authority_level != KNOWLEDGE_SEARCH_CANONICAL_AUTHORITY_LEVEL:
        raise ContractSemanticError(
            f"{label}.authority_level must be exactly "
            f"{KNOWLEDGE_SEARCH_CANONICAL_AUTHORITY_LEVEL!r} under the history view, got "
            f"{record.authority_level!r}; history returns versions that were canonical "
            "knowledge, never merely accepted ones"
        )
    if record.reviewer is None:
        raise ContractSemanticError(
            f"{label} is offered as history but carries no reviewer; a version that was "
            "never reviewed was never canonical knowledge"
        )
    if record.provenance.temporal.superseded_at is None:
        raise ContractSemanticError(
            f"{label} is offered as history but records no superseded_at instant; without "
            "one it cannot be placed before the canonical-resolution time"
        )
    superseded_at = _parse_timestamp(
        _require_str(
            record.provenance.temporal.superseded_at,
            f"{label}.provenance.temporal.superseded_at",
        ),
        f"{label}.provenance.temporal.superseded_at",
    )
    if superseded_at > resolution_instant:
        raise ContractSemanticError(
            f"{label} was superseded at "
            f"{record.provenance.temporal.superseded_at!r}, after the "
            "canonical-resolution time; at that instant it was still the canonical answer, "
            "not history"
        )


def _validate_record_under_current_canonical_view(
    record: GovernedRecord,
    resolution_instant: datetime,
    label: str,
    *,
    layer: str = GOVERNANCE_LAYER_GOVERNED,
    view_label: str = "the default current_canonical view",
) -> None:
    """Raise unless `record` is exactly what a current-canonical read may hand back: the
    live version of settled, citable knowledge, valid at the instant the read resolved at.

    The counterpart to :func:`_validate_record_under_history_view`, and stated once for the
    same reason: `knowledge.search`, `graph.traverse`, and `context_pack.build` all return
    current canonical governed records through different projections, so a predicate
    spelled three times is a predicate that can drift, and no operation may hand back what
    another would refuse.

    Four things, all exact and all fail-closed:

    - the record sits at exactly `layer`/`accepted`/`current`. `layer` is a parameter, not
      a constant, for exactly one reason: a Context Pack's `context_models` partition is
      current canonical knowledge at `l3` rather than `l2`, and holding it to a *different*
      authority, reviewer, or validity bar than an `l2` record would be a second, weaker
      canonical rule. Everything except which namespace the record lives in is shared;
    - `authority_level` is exactly :data:`KNOWLEDGE_SEARCH_CANONICAL_AUTHORITY_LEVEL`. An
      `accepted`-but-not-yet-canonical version is not citable knowledge;
    - a `reviewer` is present. Canonical knowledge is reviewed knowledge;
    - the record's `valid_from`/`valid_until` window contains `resolution_instant`
      (:func:`_validate_record_valid_at_canonical_resolution_time`). A version whose
      validity had not begun, or had already ended, was not the canonical answer then.

    `view_label` names the view in the failure message, so a Context Pack partition failure
    reads as what it is rather than as a `knowledge.search` view leak.
    """
    identity = record.provenance.identity
    if (
        identity.layer != layer
        or identity.governance_state != GOVERNANCE_STATE_ACCEPTED
        or identity.currentness != RECORD_CURRENTNESS_CURRENT
    ):
        raise ContractSemanticError(
            f"{label} leaks non-canonical governed knowledge (layer={identity.layer!r}, "
            f"governance_state={identity.governance_state!r}, "
            f"currentness={identity.currentness!r}) under {view_label}"
        )
    if record.authority_level != KNOWLEDGE_SEARCH_CANONICAL_AUTHORITY_LEVEL:
        raise ContractSemanticError(
            f"{label}.authority_level must be exactly "
            f"{KNOWLEDGE_SEARCH_CANONICAL_AUTHORITY_LEVEL!r} under {view_label}, got "
            f"{record.authority_level!r}"
        )
    if record.reviewer is None:
        raise ContractSemanticError(f"{label} is canonical but carries no reviewer")
    _validate_record_valid_at_canonical_resolution_time(
        record.provenance.temporal, resolution_instant, label
    )


def validate_knowledge_search_result(
    result: object,
    request: object,
    expected_workspace_id: object,
    canonical_resolution_time: object,
    authorized_views: object,
) -> None:
    """Raise unless `result` is workspace-scoped, agrees with everything `request` asked
    for, and never grants a view's trust beyond what `authorized_views` actually allows.

    `request` must be the complete, original `KnowledgeSearchInput` (re-validated here via
    :func:`validate_knowledge_search_input`, so a caller cannot bypass its checks by
    constructing one by hand), not just its raw `view` field: `record_type`/`domain_scope`
    filters, when set, must be honoured by every returned record (a record naming a
    different value is a request-filter mismatch, rejected regardless of view), and the
    number of returned records may never exceed `request.limit` when set, or
    :data:`KNOWLEDGE_SEARCH_DEFAULT_RESULT_LIMIT` when it is not.

    `authorized_views` is the caller's already-validated set of `GovernedRecordView` values
    a capability layer (not implemented by this function -- no capability identifier is
    ever frozen here) has actually authorized for this request; the *view string alone*
    named on `request` never widens trust on its own. Resolution happens here via
    :func:`~omnivia_core.contracts.v1.semantics.resolve_governed_record_view`, so a caller
    cannot bypass the default-view leak check by pre-resolving it themselves. When the
    resolved view is `current_canonical` (absent, or explicitly named -- always allowed,
    never gated on `authorized_views`), every returned record must be exactly
    `l2`/`accepted`/`current`, carry authority_level exactly
    :data:`KNOWLEDGE_SEARCH_CANONICAL_AUTHORITY_LEVEL`, carry a reviewer, and be valid (its
    `valid_from`/`valid_until` window must contain) `canonical_resolution_time`. When the
    resolved view is `candidates`, it must be present in `authorized_views`, and every
    returned record must be exactly `l1`/`candidate`/`current`, carry authority_level
    exactly `proposed`, and carry no reviewer. When the resolved view is `history`, it must
    likewise be present in `authorized_views`, and every returned record must satisfy the
    shared historical-canonical rule (:func:`_validate_record_under_history_view`, the same
    one `graph.traverse` applies): exactly `l2`/`accepted`/`superseded`, carrying
    `canonical` authority and a reviewer, and superseded no later than
    `canonical_resolution_time` -- a governed version that *was* canonical knowledge and
    had already been replaced at the instant this read resolved at, never a still-current
    one, a still-candidate one, or one that was merely accepted.
    """
    _require_type(result, KnowledgeSearchResult, "result")
    assert isinstance(result, KnowledgeSearchResult)
    _require_type(request, KnowledgeSearchInput, "request")
    assert isinstance(request, KnowledgeSearchInput)
    validate_knowledge_search_input(request)
    _validate_governed_search_result(
        result.records,
        requested_view=request.view,
        request_record_type=request.record_type,
        request_domain_scope=request.domain_scope,
        request_limit=request.limit,
        expected_workspace_id=expected_workspace_id,
        canonical_resolution_time=canonical_resolution_time,
        authorized_views=authorized_views,
    )


def validate_memory_search_result(
    result: object,
    request: object,
    expected_workspace_id: object,
    canonical_resolution_time: object,
    authorized_views: object,
) -> None:
    """Raise unless `result` is a valid `memory.search` result under exactly the view rules
    :func:`validate_knowledge_search_result` applies.

    The gap this closes: `memory.search` declares the same `view` selector as
    `knowledge.search` (`memory.schema.json`, `MemorySearchInput`), so an absent `view`
    resolves to `current_canonical` and only an explicit `view` may ask for `candidates` or
    `history` -- but nothing enforced that, so a `memory.search` answering a default-view
    request with candidate or superseded records conformed. It is the same rule, applied
    through the same function (:func:`_validate_governed_search_result`), so the two reads
    cannot drift on what a view means.

    `request` must be the complete, original `MemorySearchInput`, re-validated here through
    :func:`validate_memory_search_input`. `MemorySearchInput` carries no `domain_scope`, so
    no domain filter is checked; every other rule is identical, including that
    `authorized_views` -- not the view string on the request -- is what widens trust.
    """
    _require_type(result, MemorySearchResult, "result")
    assert isinstance(result, MemorySearchResult)
    _require_type(request, MemorySearchInput, "request")
    assert isinstance(request, MemorySearchInput)
    validate_memory_search_input(request)
    _validate_governed_search_result(
        result.records,
        requested_view=request.view,
        request_record_type=request.record_type,
        request_domain_scope=None,
        request_limit=request.limit,
        expected_workspace_id=expected_workspace_id,
        canonical_resolution_time=canonical_resolution_time,
        authorized_views=authorized_views,
    )


def _validate_governed_search_result(
    result_records: object,
    *,
    requested_view: str | None,
    request_record_type: str | None,
    request_domain_scope: str | None,
    request_limit: int | None,
    expected_workspace_id: object,
    canonical_resolution_time: object,
    authorized_views: object,
) -> None:
    """The governed-record page rule `knowledge.search` and `memory.search` share exactly.

    One body, two callers, so the view semantics -- which records a resolved view may
    contain, and which views the request alone may reach -- cannot be true of one read and
    false of the other. `request_domain_scope` is `None` for `memory.search`, whose input
    declares no such filter.
    """
    _validate_workspace_id(_require_str(expected_workspace_id, "expected_workspace_id"), "expected_workspace_id")
    resolution_instant = _parse_timestamp(
        _require_str(canonical_resolution_time, "canonical_resolution_time"), "canonical_resolution_time"
    )
    authorized = _require_set_of_str(authorized_views, "authorized_views")
    for view in authorized:
        _validate_view_selector(view, "authorized_views entry")

    resolved_view = resolve_governed_record_view(requested_view)
    if (
        requested_view is not None
        and resolved_view != GOVERNED_RECORD_VIEW_CURRENT_CANONICAL
        and resolved_view not in authorized
    ):
        raise ContractSemanticError(
            f"view {resolved_view!r} was explicitly requested but is not present in "
            f"authorized_views {sorted(authorized)!r}; the view string alone never "
            "widens trust"
        )

    records = _require_sequence(result_records, "records")
    max_results = request_limit if request_limit is not None else KNOWLEDGE_SEARCH_DEFAULT_RESULT_LIMIT
    if len(records) > max_results:
        raise ContractSemanticError(
            f"result.records has {len(records)} entries, exceeding the applicable limit of "
            f"{max_results}"
        )

    for index, record in enumerate(records):
        label = f"records[{index}]"
        _require_type(record, GovernedRecord, label)
        assert isinstance(record, GovernedRecord)
        validate_governed_record(record)
        if record.workspace_id != expected_workspace_id:
            raise ContractSemanticError(
                f"{label}.workspace_id {record.workspace_id!r} does not match the "
                f"selected workspace {expected_workspace_id!r}"
            )
        if request_record_type is not None and record.record_type != request_record_type:
            raise ContractSemanticError(
                f"{label}.record_type {record.record_type!r} does not match the requested "
                f"record_type {request_record_type!r}"
            )
        if request_domain_scope is not None and record.domain_scope != request_domain_scope:
            raise ContractSemanticError(
                f"{label}.domain_scope {record.domain_scope!r} does not match the requested "
                f"domain_scope {request_domain_scope!r}"
            )

        identity = record.provenance.identity
        if resolved_view == GOVERNED_RECORD_VIEW_CURRENT_CANONICAL:
            _validate_record_under_current_canonical_view(record, resolution_instant, label)
        elif resolved_view == GOVERNED_RECORD_VIEW_CANDIDATES:
            if (
                identity.layer != GOVERNANCE_LAYER_CANDIDATE
                or identity.governance_state != GOVERNANCE_STATE_CANDIDATE
                or identity.currentness != RECORD_CURRENTNESS_CURRENT
            ):
                raise ContractSemanticError(
                    f"{label} is not an exact l1/candidate/current record (layer="
                    f"{identity.layer!r}, governance_state={identity.governance_state!r}, "
                    f"currentness={identity.currentness!r}) under the candidates view"
                )
            if record.authority_level != AUTHORITY_LEVEL_PROPOSED:
                raise ContractSemanticError(
                    f"{label}.authority_level must be exactly {AUTHORITY_LEVEL_PROPOSED!r} "
                    f"under the candidates view, got {record.authority_level!r}"
                )
            if record.reviewer is not None:
                raise ContractSemanticError(
                    f"{label} carries a reviewer but the candidates view requires none"
                )
        elif resolved_view == GOVERNED_RECORD_VIEW_HISTORY:
            _validate_record_under_history_view(record, resolution_instant, label)


# --- shared governance-transition reciprocity ---------------------------------

GOVERNANCE_ACTION_KNOWLEDGE_PROPOSE: Final = "knowledge.propose"
GOVERNANCE_ACTION_CANDIDATE_APPROVE: Final = "candidate.approve"
GOVERNANCE_ACTION_CANDIDATE_REJECT: Final = "candidate.reject"
GOVERNANCE_ACTION_RECORD_SUPERSEDE: Final = "record.supersede"

GOVERNANCE_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        GOVERNANCE_ACTION_KNOWLEDGE_PROPOSE,
        GOVERNANCE_ACTION_CANDIDATE_APPROVE,
        GOVERNANCE_ACTION_CANDIDATE_REJECT,
        GOVERNANCE_ACTION_RECORD_SUPERSEDE,
    }
)
"""The exact `ProvenanceEntry.action` value each governance transition appends.

`action` is a wire-open `OpenCode`, but the audit event a governance transition writes is
the record of *which* operation was authorized, so it is frozen here rather than left to a
server's choice of spelling: a caller verifying an approval cannot tell an `approved` event
from a `candidate.approve` one apart from an exact, published value. Each result validator
requires its own operation's exact string, so no transition can pass by naming another's.
"""

GOVERNANCE_AUTHORITY_LEVEL_CANONICAL: Final = "canonical"
"""The exact `authority_level` an accepted, current governed record carries after
`candidate.approve` or `record.supersede`. The same value
:data:`KNOWLEDGE_SEARCH_CANONICAL_AUTHORITY_LEVEL` requires of a default-view
`knowledge.search` result, stated separately here because the two rules are independent:
this one is what a transition must *produce*, that one is what a read must not *exceed*."""

GOVERNANCE_AUTHORITY_LEVEL_REJECTED: Final = "rejected"
"""The exact `authority_level` a record carries after `candidate.reject`. Never a
favourable or accepted authority decision -- it carries a reviewer because a decision was
made, not because the decision went the record's way."""


def _require_exact(actual: object, expected: object, label: str) -> None:
    """Raise unless `actual` equals `expected` exactly.

    The one comparison every frozen governance matrix in this module goes through, so
    "exact" always means the same thing: full equality, never membership in a set of
    acceptable values, never a subset or prefix relationship, and -- for a dataclass or a
    tuple of dataclasses -- complete field-by-field, order-sensitive equality rather than
    agreement on some chosen key.
    """
    if actual != expected:
        raise ContractSemanticError(f"{label} must be exactly {expected!r}, got {actual!r}")


def _validate_mutation_precondition(
    precondition: object, label: str = "precondition"
) -> MutationPrecondition:
    """Raise unless `precondition` is a shape-valid `MutationPrecondition`.

    A direct entry point's argument, so it is type-guarded and its `record_version` is
    revalidated as a bounded, pattern-valid `OpaqueToken` rather than trusted: a malformed
    precondition can never be satisfied by any record, so it fails closed here rather than
    silently comparing equal to nothing.
    """
    _require_type(precondition, MutationPrecondition, label)
    assert isinstance(precondition, MutationPrecondition)
    _validate_opaque_token(
        _require_str(precondition.record_version, f"{label}.record_version"),
        f"{label}.record_version",
    )
    return precondition


def _require_governed_record_state(
    record: GovernedRecord,
    label: str,
    *,
    layer: str,
    governance_state: str,
    currentness: str,
    authority_level: str,
    reviewer: str | None,
    reviewer_required: bool,
) -> None:
    """Raise unless `record`'s governance position is exactly the one named.

    Every axis is an exact match, never a membership test: `layer`, `governance_state`,
    `currentness`, and `authority_level` are all wire-open vocabularies, and a transition
    that landed on an unrecognized value on any of them has produced a state this build
    cannot verify. `reviewer_required` states whether *some* reviewer must be present
    without saying who (the prior side of `record.supersede`, which carries whatever
    reviewer approved it originally); `reviewer` names the exact trusted identity the
    updated side must carry, or `None` when it must carry none at all.
    """
    identity = record.provenance.identity
    _require_exact(identity.layer, layer, f"{label}.provenance.identity.layer")
    _require_exact(
        identity.governance_state, governance_state, f"{label}.provenance.identity.governance_state"
    )
    _require_exact(identity.currentness, currentness, f"{label}.provenance.identity.currentness")
    _require_exact(record.authority_level, authority_level, f"{label}.authority_level")
    if reviewer is not None:
        _require_exact(record.reviewer, reviewer, f"{label}.reviewer")
        return
    if reviewer_required:
        if record.reviewer is None:
            raise ContractSemanticError(f"{label}.reviewer is required but absent")
        return
    if record.reviewer is not None:
        raise ContractSemanticError(
            f"{label}.reviewer must be absent at this point in the governance workflow, "
            f"got {record.reviewer!r}"
        )


def _validate_governance_transition(
    previous: object,
    updated: object,
    *,
    action: str,
    request_record_id: str,
    precondition: MutationPrecondition,
    rationale: GovernanceRationale,
    expected_workspace_id: str,
    expected_actor_id: str,
    expected_actor_kind: str,
    previous_layer: str,
    previous_state: str,
    previous_authority: str,
    previous_reviewer_required: bool,
    updated_layer: str,
    updated_state: str,
    updated_authority: str,
    updated_reviewer: str | None,
    expected_event_evidence: tuple[EvidenceReference, ...],
) -> tuple[GovernedRecord, GovernedRecord]:
    """Raise unless `previous`/`updated` are exactly the governance transition described.

    Shared by `knowledge.propose`, `candidate.approve`, `candidate.reject`, and
    `record.supersede`: all four produce the same shape of result -- the prior version and
    the new version of one record -- so everything they have in common is stated once here,
    and each operation passes its own frozen state/authority/reviewer matrix, exact audit
    action, and expected transition-event evidence in. The per-operation claim rules
    (:func:`_require_preserved_claim` for the three that must not touch the claim,
    :func:`_require_supersede_replacement_binding` for the one that replaces it) layer on
    top; nothing about the claim itself is decided here.

    In order, and all fail-closed:

    - both records are individually valid governed records
      (:func:`~omnivia_core.contracts.v1.semantics.validate_governed_record`, which composes
      temporal/evidence/currentness/authority coherence) and belong to
      `expected_workspace_id`;
    - both carry `request_record_id` as their stable `record_id`: the transition is bound to
      the record the request actually named, not merely to whatever the two records agree
      between themselves;
    - `precondition.record_version` is exactly `previous`'s version, so a result computed
      against a version the caller did not target is rejected rather than accepted as a
      stale-but-plausible answer, and the two versions differ (no self-supersession);
    - both records sit at exactly their frozen layer/state/currentness/authority/reviewer
      position (:func:`_require_governed_record_state`);
    - `previous` is superseded with a `superseded_by` naming `updated`'s exact
      `(record_id, version)` and a `reason` exactly equal to the request rationale's
      `reason_code`; `updated` is current, not itself superseded, and its `supersedes`
      names `previous`'s exact `(record_id, version)` with the same exact reason;
    - one transition instant governs the whole transition: `previous.temporal.superseded_at`,
      `updated.temporal.recorded_at`, and the appended event's `occurred_at` are all exactly
      equal, and `previous.temporal.recorded_at` is not after it -- a version cannot be
      superseded before it was recorded;
    - `updated`'s history is `previous`'s history plus *exactly one* entry. Not zero (an
      unaudited transition), not two (an unexplained extra event), and the shared prefix is
      compared by full dataclass equality, so a rewritten earlier entry is caught as loudly
      as a deleted one;
    - that one entry names exactly `action`, exactly the trusted `expected_actor_id` /
      `expected_actor_kind` the caller vouched for (never an actor the result asserts for
      itself), and carries the request rationale's `reason_code` and `comment` exactly --
      including an absent comment, which must stay absent rather than being filled in;
    - that entry's evidence is exactly `expected_event_evidence`, by complete ordered
      dataclass equality: empty for the three transitions that assert no new evidence, and
      the replacement's own assertion evidence for `record.supersede`;
    - both records carry an `assertion`. It is structurally optional so a record written
      before that field existed still decodes, but a governance transition may not lose the
      lineage of the claim it is deciding on.
    """
    _require_type(previous, GovernedRecord, "previous_record")
    assert isinstance(previous, GovernedRecord)
    _require_type(updated, GovernedRecord, "updated_record")
    assert isinstance(updated, GovernedRecord)
    validate_governed_record(previous)
    validate_governed_record(updated)

    for label, record in (("previous_record", previous), ("updated_record", updated)):
        if record.workspace_id != expected_workspace_id:
            raise ContractSemanticError(
                f"{label}.workspace_id {record.workspace_id!r} does not match the "
                f"selected workspace {expected_workspace_id!r}"
            )

    prev_identity = previous.provenance.identity
    upd_identity = updated.provenance.identity
    _require_exact(
        prev_identity.record_id, request_record_id, "previous_record.provenance.identity.record_id"
    )
    _require_exact(
        upd_identity.record_id, request_record_id, "updated_record.provenance.identity.record_id"
    )
    _require_exact(
        precondition.record_version,
        prev_identity.version,
        "precondition.record_version",
    )
    if prev_identity.version == upd_identity.version:
        raise ContractSemanticError(
            "previous_record and updated_record must not share the same version "
            "(a record cannot supersede itself)"
        )

    _require_governed_record_state(
        previous,
        "previous_record",
        layer=previous_layer,
        governance_state=previous_state,
        currentness=RECORD_CURRENTNESS_SUPERSEDED,
        authority_level=previous_authority,
        reviewer=None,
        reviewer_required=previous_reviewer_required,
    )
    _require_governed_record_state(
        updated,
        "updated_record",
        layer=updated_layer,
        governance_state=updated_state,
        currentness=RECORD_CURRENTNESS_CURRENT,
        authority_level=updated_authority,
        reviewer=updated_reviewer,
        reviewer_required=updated_reviewer is not None,
    )

    forward = prev_identity.superseded_by
    if forward is None or (forward.record_id, forward.version) != (
        request_record_id,
        upd_identity.version,
    ):
        found = None if forward is None else (forward.record_id, forward.version)
        raise ContractSemanticError(
            "previous_record.superseded_by must reciprocally reference "
            f"({request_record_id!r}, {upd_identity.version!r}), found {found!r}"
        )
    _require_exact(
        forward.reason, rationale.reason_code, "previous_record.superseded_by.reason"
    )

    backward = upd_identity.supersedes
    if backward is None or (backward.record_id, backward.version) != (
        request_record_id,
        prev_identity.version,
    ):
        found = None if backward is None else (backward.record_id, backward.version)
        raise ContractSemanticError(
            "updated_record.supersedes must reciprocally reference "
            f"({request_record_id!r}, {prev_identity.version!r}), found {found!r}"
        )
    _require_exact(backward.reason, rationale.reason_code, "updated_record.supersedes.reason")

    transition_instant = previous.provenance.temporal.superseded_at
    if transition_instant is None:
        raise ContractSemanticError(
            "previous_record.provenance.temporal.superseded_at is required: a superseded "
            "version must record when it was superseded"
        )
    _require_exact(
        updated.provenance.temporal.recorded_at,
        transition_instant,
        "updated_record.provenance.temporal.recorded_at",
    )
    instant = _parse_timestamp(
        transition_instant, "previous_record.provenance.temporal.superseded_at"
    )
    previously_recorded = _parse_timestamp(
        previous.provenance.temporal.recorded_at, "previous_record.provenance.temporal.recorded_at"
    )
    if previously_recorded > instant:
        raise ContractSemanticError(
            f"previous_record.provenance.temporal.recorded_at "
            f"{previous.provenance.temporal.recorded_at!r} is after the transition instant "
            f"{transition_instant!r}"
        )

    prev_history = previous.provenance.history
    upd_history = updated.provenance.history
    if len(upd_history) != len(prev_history) + 1:
        raise ContractSemanticError(
            f"updated_record.provenance.history must append exactly one {action!r} transition "
            f"event to previous_record's {len(prev_history)} entries, found "
            f"{len(upd_history)} entries"
        )
    if upd_history[: len(prev_history)] != prev_history:
        raise ContractSemanticError(
            "updated_record.provenance.history must preserve previous_record.provenance.history "
            "as an unmodified prefix; history must never be erased or rewritten"
        )

    event = upd_history[-1]
    _require_exact(event.action, action, "transition event action")
    _require_exact(event.actor_id, expected_actor_id, "transition event actor_id")
    _require_exact(event.actor_kind, expected_actor_kind, "transition event actor_kind")
    _require_exact(event.reason_code, rationale.reason_code, "transition event reason_code")
    _require_exact(event.reason_comment, rationale.comment, "transition event reason_comment")
    _require_exact(event.occurred_at, transition_instant, "transition event occurred_at")
    _require_exact(
        tuple(event.evidence or ()), expected_event_evidence, "transition event evidence"
    )

    for label, record in (("previous_record", previous), ("updated_record", updated)):
        if record.provenance.assertion is None:
            raise ContractSemanticError(
                f"{label}.provenance.assertion is required on a governance transition: the "
                "lineage of the claim being decided on must never be dropped"
            )
    return previous, updated


def _require_preserved_claim(previous: GovernedRecord, updated: GovernedRecord) -> None:
    """Raise unless `updated` carries exactly `previous`'s claim, untouched.

    `knowledge.propose`, `candidate.approve`, and `candidate.reject` decide *about* a claim;
    none of them may edit it. Only the identity state/version/currentness/pointers, the
    reviewer/authority pair, the recorded/superseded instants, and the single appended
    transition event may differ between the two versions -- so every other part of the claim
    is compared here for exact equality: `record_type`, `domain_scope`, the opaque `content`,
    the evidence disposition, the complete ordered `SourceReference` tuple (locator and
    `retrieved_at` included, by full dataclass equality rather than by source key), the
    assertion and extraction lineage, and the temporal fields a decision never moves --
    `event_at`, `observed_at`, `ingested_at`, `valid_from`, and `valid_until`.
    """
    _require_exact(updated.record_type, previous.record_type, "updated_record.record_type")
    _require_exact(updated.domain_scope, previous.domain_scope, "updated_record.domain_scope")
    _require_exact(updated.content, previous.content, "updated_record.content")

    previous_provenance = previous.provenance
    updated_provenance = updated.provenance
    _require_exact(
        updated_provenance.evidence_disposition,
        previous_provenance.evidence_disposition,
        "updated_record.provenance.evidence_disposition",
    )
    _require_exact(
        updated_provenance.sources,
        previous_provenance.sources,
        "updated_record.provenance.sources",
    )
    _require_exact(
        updated_provenance.assertion,
        previous_provenance.assertion,
        "updated_record.provenance.assertion",
    )
    _require_exact(
        updated_provenance.extraction,
        previous_provenance.extraction,
        "updated_record.provenance.extraction",
    )
    for field_name in ("event_at", "observed_at", "ingested_at", "valid_from", "valid_until"):
        _require_exact(
            getattr(updated_provenance.temporal, field_name),
            getattr(previous_provenance.temporal, field_name),
            f"updated_record.provenance.temporal.{field_name}",
        )


def _require_supersede_replacement_binding(
    previous: GovernedRecord, updated: GovernedRecord, replacement: MemoryCreateInput
) -> None:
    """Raise unless `updated`'s claim is exactly the one `replacement` supplied.

    `record.supersede` is the one transition that does replace the claim, so the rule is the
    mirror image of :func:`_require_preserved_claim`: instead of matching the prior version,
    every part of the new claim must match the request's `replacement` exactly -- opaque
    `content`, evidence disposition, the complete ordered `SourceReference` tuple, the
    assertion and extraction lineage, the `event_at`/`observed_at` instants, and the
    `valid_from`/`valid_until` window the replacement's assertion proposed. Because the
    binding is exact equality rather than containment, a server cannot quietly union the
    superseded version's sources or evidence into the new claim: a replacement drawing on
    wholly different sources produces a record declaring wholly those sources and no others.

    `record_type` and `domain_scope` are the one thing supersession may *not* change: both
    must equal the superseded record's, and the updated record must carry them too, so a
    supersession can never be used to silently reclassify a record into a different type or
    domain while presenting itself as a routine content replacement.
    """
    _require_exact(
        replacement.record_type, previous.record_type, "request.replacement.record_type"
    )
    _require_exact(
        replacement.domain_scope, previous.domain_scope, "request.replacement.domain_scope"
    )
    _require_exact(updated.record_type, replacement.record_type, "updated_record.record_type")
    _require_exact(updated.domain_scope, replacement.domain_scope, "updated_record.domain_scope")
    _require_exact(updated.content, replacement.content, "updated_record.content")

    provenance = updated.provenance
    _require_exact(
        provenance.evidence_disposition,
        replacement.evidence_disposition,
        "updated_record.provenance.evidence_disposition",
    )
    _require_exact(
        provenance.sources, tuple(replacement.sources), "updated_record.provenance.sources"
    )
    _require_exact(
        provenance.assertion, replacement.assertion, "updated_record.provenance.assertion"
    )
    _require_exact(
        provenance.extraction, replacement.extraction, "updated_record.provenance.extraction"
    )
    _require_exact(
        provenance.temporal.event_at,
        replacement.event_at,
        "updated_record.provenance.temporal.event_at",
    )
    _require_exact(
        provenance.temporal.observed_at,
        replacement.observed_at,
        "updated_record.provenance.temporal.observed_at",
    )
    _require_exact(
        provenance.temporal.valid_from,
        replacement.assertion.proposed_valid_from,
        "updated_record.provenance.temporal.valid_from",
    )
    _require_exact(
        provenance.temporal.valid_until,
        replacement.assertion.proposed_valid_until,
        "updated_record.provenance.temporal.valid_until",
    )


def _validate_transition_arguments(
    precondition: object,
    expected_workspace_id: object,
    expected_actor_id: object,
    expected_actor_kind: object,
) -> tuple[MutationPrecondition, str, str, str]:
    """Type-guard and revalidate the trust-carrying arguments every transition validator
    takes, returning them narrowed.

    `expected_workspace_id`, `expected_actor_id`, and `expected_actor_kind` are what the
    caller's authorization layer established independently of the result being checked;
    they are the values the result is measured against, so a malformed one can never be
    satisfied and fails closed here rather than being compared as-is.
    """
    checked_precondition = _validate_mutation_precondition(precondition)
    workspace_id = _require_str(expected_workspace_id, "expected_workspace_id")
    _validate_workspace_id(workspace_id, "expected_workspace_id")
    actor_id = _require_str(expected_actor_id, "expected_actor_id")
    _validate_identifier(actor_id, "expected_actor_id")
    actor_kind = _require_str(expected_actor_kind, "expected_actor_kind")
    _validate_open_code(actor_kind, "expected_actor_kind")
    return checked_precondition, workspace_id, actor_id, actor_kind


def validate_knowledge_propose_result(
    result: object,
    request: object,
    precondition: object,
    expected_workspace_id: object,
    expected_actor_id: object,
    expected_actor_kind: object,
) -> None:
    """Raise unless `result` is exactly the `knowledge.propose` transition `request` asked
    for, taken against the version `precondition` targeted, by the actor the caller vouched
    for.

    A direct entry point: every argument is type-guarded, and `request`/`precondition` are
    re-validated here (:func:`validate_knowledge_propose_input`,
    :func:`_validate_mutation_precondition`) rather than trusted, so a caller cannot bypass
    their checks by hand-building either one. Both records are bound to
    `request.record_id`, and `precondition.record_version` must be exactly the previous
    version, so a result computed against some other version of the record is rejected.

    The frozen matrix: prior `l1`/`proposed`/`superseded` carrying `proposed` authority and
    no reviewer, becoming `l1`/`candidate`/`current` still carrying `proposed` authority and
    still no reviewer -- putting a record forward for a decision is not itself a decision,
    so nothing about its authority or reviewer may move. The audit event is exactly
    :data:`GOVERNANCE_ACTION_KNOWLEDGE_PROPOSE`, attributed to the trusted
    `expected_actor_id`/`expected_actor_kind` and carrying `request.rationale` verbatim, and
    it asserts no new evidence. The claim itself is untouched
    (:func:`_require_preserved_claim`).
    """
    _require_type(result, KnowledgeProposeResult, "result")
    assert isinstance(result, KnowledgeProposeResult)
    _require_type(request, KnowledgeProposeInput, "request")
    assert isinstance(request, KnowledgeProposeInput)
    validate_knowledge_propose_input(request)
    checked_precondition, workspace_id, actor_id, actor_kind = _validate_transition_arguments(
        precondition, expected_workspace_id, expected_actor_id, expected_actor_kind
    )
    previous, updated = _validate_governance_transition(
        result.previous_record,
        result.updated_record,
        action=GOVERNANCE_ACTION_KNOWLEDGE_PROPOSE,
        request_record_id=request.record_id,
        precondition=checked_precondition,
        rationale=request.rationale,
        expected_workspace_id=workspace_id,
        expected_actor_id=actor_id,
        expected_actor_kind=actor_kind,
        previous_layer=GOVERNANCE_LAYER_CANDIDATE,
        previous_state=GOVERNANCE_STATE_PROPOSED,
        previous_authority=AUTHORITY_LEVEL_PROPOSED,
        previous_reviewer_required=False,
        updated_layer=GOVERNANCE_LAYER_CANDIDATE,
        updated_state=GOVERNANCE_STATE_CANDIDATE,
        updated_authority=AUTHORITY_LEVEL_PROPOSED,
        updated_reviewer=None,
        expected_event_evidence=(),
    )
    _require_preserved_claim(previous, updated)


def validate_candidate_approve_result(
    result: object,
    request: object,
    precondition: object,
    expected_workspace_id: object,
    expected_reviewer_id: object,
    expected_actor_kind: object,
) -> None:
    """Raise unless `result` is exactly the `candidate.approve` transition `request` asked
    for, taken against the version `precondition` targeted, by the reviewer the caller
    vouched for.

    Same direct-entry-point guarantees as :func:`validate_knowledge_propose_result`:
    everything is type-guarded, `request`/`precondition` are revalidated rather than
    trusted, and both records are bound to `request.record_id` against
    `precondition.record_version`.

    The frozen matrix: prior `l1`/`candidate`/`superseded` carrying `proposed` authority and
    no reviewer, becoming `l2`/`accepted`/`current` carrying exactly
    :data:`GOVERNANCE_AUTHORITY_LEVEL_CANONICAL` authority. The new version's `reviewer` and
    the audit event's actor must both be exactly `expected_reviewer_id` -- the identity the
    caller's authorization layer established, never one the result asserts for itself -- so
    a result cannot attribute an approval to a reviewer who did not make it. The audit event
    is exactly :data:`GOVERNANCE_ACTION_CANDIDATE_APPROVE`, carries `request.rationale`
    verbatim, and asserts no new evidence: approving a candidate decides on the evidence
    already there, it does not add any. The claim itself is untouched
    (:func:`_require_preserved_claim`).
    """
    _require_type(result, CandidateApproveResult, "result")
    assert isinstance(result, CandidateApproveResult)
    _require_type(request, CandidateApproveInput, "request")
    assert isinstance(request, CandidateApproveInput)
    validate_candidate_approve_input(request)
    checked_precondition, workspace_id, reviewer_id, actor_kind = _validate_transition_arguments(
        precondition, expected_workspace_id, expected_reviewer_id, expected_actor_kind
    )
    previous, updated = _validate_governance_transition(
        result.previous_record,
        result.updated_record,
        action=GOVERNANCE_ACTION_CANDIDATE_APPROVE,
        request_record_id=request.record_id,
        precondition=checked_precondition,
        rationale=request.rationale,
        expected_workspace_id=workspace_id,
        expected_actor_id=reviewer_id,
        expected_actor_kind=actor_kind,
        previous_layer=GOVERNANCE_LAYER_CANDIDATE,
        previous_state=GOVERNANCE_STATE_CANDIDATE,
        previous_authority=AUTHORITY_LEVEL_PROPOSED,
        previous_reviewer_required=False,
        updated_layer=GOVERNANCE_LAYER_GOVERNED,
        updated_state=GOVERNANCE_STATE_ACCEPTED,
        updated_authority=GOVERNANCE_AUTHORITY_LEVEL_CANONICAL,
        updated_reviewer=reviewer_id,
        expected_event_evidence=(),
    )
    _require_preserved_claim(previous, updated)


def validate_candidate_reject_result(
    result: object,
    request: object,
    precondition: object,
    expected_workspace_id: object,
    expected_reviewer_id: object,
    expected_actor_kind: object,
) -> None:
    """Raise unless `result` is exactly the `candidate.reject` transition `request` asked
    for, taken against the version `precondition` targeted, by the reviewer the caller
    vouched for.

    The frozen matrix: prior `l1`/`candidate`/`superseded` carrying `proposed` authority and
    no reviewer, becoming `l1`/`rejected`/`current` carrying exactly
    :data:`GOVERNANCE_AUTHORITY_LEVEL_REJECTED` authority and exactly `expected_reviewer_id`
    as its reviewer. A rejected record stays in the candidate layer and never rises to `l2`;
    it carries a reviewer because a decision was made, and requiring one is never a
    statement that the decision was favourable. The audit event is exactly
    :data:`GOVERNANCE_ACTION_CANDIDATE_REJECT`, attributed to the same trusted reviewer,
    carries `request.rationale` verbatim, and asserts no new evidence. The claim itself is
    untouched (:func:`_require_preserved_claim`): a rejection records a judgement about a
    claim, it never edits the claim it rejected.
    """
    _require_type(result, CandidateRejectResult, "result")
    assert isinstance(result, CandidateRejectResult)
    _require_type(request, CandidateRejectInput, "request")
    assert isinstance(request, CandidateRejectInput)
    validate_candidate_reject_input(request)
    checked_precondition, workspace_id, reviewer_id, actor_kind = _validate_transition_arguments(
        precondition, expected_workspace_id, expected_reviewer_id, expected_actor_kind
    )
    previous, updated = _validate_governance_transition(
        result.previous_record,
        result.updated_record,
        action=GOVERNANCE_ACTION_CANDIDATE_REJECT,
        request_record_id=request.record_id,
        precondition=checked_precondition,
        rationale=request.rationale,
        expected_workspace_id=workspace_id,
        expected_actor_id=reviewer_id,
        expected_actor_kind=actor_kind,
        previous_layer=GOVERNANCE_LAYER_CANDIDATE,
        previous_state=GOVERNANCE_STATE_CANDIDATE,
        previous_authority=AUTHORITY_LEVEL_PROPOSED,
        previous_reviewer_required=False,
        updated_layer=GOVERNANCE_LAYER_CANDIDATE,
        updated_state=GOVERNANCE_STATE_REJECTED,
        updated_authority=GOVERNANCE_AUTHORITY_LEVEL_REJECTED,
        updated_reviewer=reviewer_id,
        expected_event_evidence=(),
    )
    _require_preserved_claim(previous, updated)


def validate_record_supersede_result(
    result: object,
    request: object,
    precondition: object,
    expected_workspace_id: object,
    expected_reviewer_id: object,
    expected_actor_kind: object,
) -> None:
    """Raise unless `result` is exactly the `record.supersede` transition `request` asked
    for, taken against the version `precondition` targeted, by the reviewer the caller
    vouched for.

    The frozen matrix: prior `l2`/`accepted`/`superseded` carrying exactly
    :data:`GOVERNANCE_AUTHORITY_LEVEL_CANONICAL` authority and whatever reviewer already
    accepted it (required to be present, but not required to be this caller -- the record
    being replaced was approved by whoever approved it), becoming a new
    `l2`/`accepted`/`current` version carrying the same canonical authority and exactly
    `expected_reviewer_id` as its reviewer. The audit event is exactly
    :data:`GOVERNANCE_ACTION_RECORD_SUPERSEDE`, attributed to that same trusted reviewer, and
    carries `request.rationale` verbatim.

    This is the one transition that replaces the claim, so two rules differ from the other
    three. The transition event's evidence must be exactly
    `request.replacement.assertion.evidence` -- complete, ordered, source/span/excerpt
    included -- since a supersession is justified by the evidence behind the new claim. And
    the new version's whole claim is bound to `request.replacement`
    (:func:`_require_supersede_replacement_binding`) rather than to the prior version, by
    exact equality, so nothing from the superseded version leaks forward into it: the prior
    record's sources and evidence are never unioned in. `record_type` and `domain_scope` are
    the exception that must not change -- the replacement's must equal the prior record's,
    and the updated record must carry them -- so supersession can never be used to
    reclassify a record while presenting itself as a content replacement.
    """
    _require_type(result, RecordSupersedeResult, "result")
    assert isinstance(result, RecordSupersedeResult)
    _require_type(request, RecordSupersedeInput, "request")
    assert isinstance(request, RecordSupersedeInput)
    validate_record_supersede_input(request)
    checked_precondition, workspace_id, reviewer_id, actor_kind = _validate_transition_arguments(
        precondition, expected_workspace_id, expected_reviewer_id, expected_actor_kind
    )
    previous, updated = _validate_governance_transition(
        result.previous_record,
        result.updated_record,
        action=GOVERNANCE_ACTION_RECORD_SUPERSEDE,
        request_record_id=request.record_id,
        precondition=checked_precondition,
        rationale=request.rationale,
        expected_workspace_id=workspace_id,
        expected_actor_id=reviewer_id,
        expected_actor_kind=actor_kind,
        previous_layer=GOVERNANCE_LAYER_GOVERNED,
        previous_state=GOVERNANCE_STATE_ACCEPTED,
        previous_authority=GOVERNANCE_AUTHORITY_LEVEL_CANONICAL,
        previous_reviewer_required=True,
        updated_layer=GOVERNANCE_LAYER_GOVERNED,
        updated_state=GOVERNANCE_STATE_ACCEPTED,
        updated_authority=GOVERNANCE_AUTHORITY_LEVEL_CANONICAL,
        updated_reviewer=reviewer_id,
        expected_event_evidence=tuple(request.replacement.assertion.evidence),
    )
    _require_supersede_replacement_binding(previous, updated, request.replacement)


# --- shared projection freshness ----------------------------------------------


def _validate_projection_map(value: object, label: str) -> frozenset[str]:
    """Raise unless `value` is a non-empty open map of `OpenCode` projection name to
    `ProjectionVersion`, and return the set of projection names it declares."""
    mapping = _require_mapping(value, label)
    if not mapping:
        raise ContractSemanticError(
            f"{label} names no projection; a read served from a projection must say which one"
        )
    names: set[str] = set()
    for key, version in mapping.items():
        name = _require_str(key, f"{label} key")
        _validate_open_code(name, f"{label} key")
        version_label = f"{label}[{name!r}]"
        _validate_projection_version(_require_str(version, version_label), version_label)
        names.add(name)
    return frozenset(names)


def validate_projection_freshness(freshness: object, label: str = "freshness") -> datetime:
    """Raise unless `freshness` is a strict, internally coherent `ProjectionFreshness`, and
    return the instant its `as_of` names.

    The one shared freshness rule every projection-served read composes, so `graph.traverse`
    and (later) `context_pack.build` cannot drift on what a staleness statement has to say.
    Enforces a canonical RFC 3339 UTC `as_of`; a non-empty `projection_versions` and
    `projection_watermarks`, each keyed by a valid `OpenCode` with a valid `ProjectionVersion`
    value; *identical key sets* across the two maps, since they are one statement about the
    same set of projections and a version without its watermark (or the reverse) leaves the
    caller unable to tell how far behind the write model that projection actually is; and a
    real `bool` `stale` -- a truthy non-boolean would silently read as a staleness claim.

    A direct entry point: `freshness` need not have come through a tolerant `from_wire`
    decode, so every field access is guarded and a hand-built DTO raises
    `ContractSemanticError`, never a raw `TypeError`/`AttributeError`.
    """
    _require_type(freshness, ProjectionFreshness, label)
    assert isinstance(freshness, ProjectionFreshness)
    as_of = _parse_timestamp(_require_str(freshness.as_of, f"{label}.as_of"), f"{label}.as_of")
    versions = _validate_projection_map(freshness.projection_versions, f"{label}.projection_versions")
    watermarks = _validate_projection_map(freshness.projection_watermarks, f"{label}.projection_watermarks")
    if versions != watermarks:
        raise ContractSemanticError(
            f"{label}.projection_versions and {label}.projection_watermarks must name exactly "
            f"the same projections; versions name {sorted(versions)!r} and watermarks name "
            f"{sorted(watermarks)!r}"
        )
    _require_type(freshness.stale, bool, f"{label}.stale")
    return as_of


# --- graph.traverse ------------------------------------------------------------

GRAPH_DIRECTION_OUTBOUND: Final = "outbound"
GRAPH_DIRECTION_INBOUND: Final = "inbound"
GRAPH_DIRECTION_BOTH: Final = "both"

GRAPH_DIRECTIONS: Final[frozenset[str]] = frozenset(
    {GRAPH_DIRECTION_OUTBOUND, GRAPH_DIRECTION_INBOUND, GRAPH_DIRECTION_BOTH}
)
"""The only known `GraphDirection` values. `GraphDirection` is wire-open, but direction is
trust-sensitive: an unrecognized value fails closed rather than being guessed at."""

GRAPH_DIRECTION_DEFAULT: Final = GRAPH_DIRECTION_OUTBOUND
"""The direction `graph.traverse` applies when `direction` is absent from the input."""

GRAPH_MIN_DEPTH_LIMIT: Final = 0
GRAPH_MAX_DEPTH_LIMIT: Final = 8
GRAPH_DEFAULT_DEPTH_LIMIT: Final = 1
"""The depth `graph.traverse` applies when `depth_limit` is absent from the input."""

GRAPH_ORDERING_BASIS_DEPTH_RECORD_VERSION_ASC: Final = "depth_record_version_asc"
"""The only known `GraphOrderingBasis` value: `nodes` ascending by `(depth, record_id,
version)`, the deterministic breadth-first tie break this contract requires."""

GRAPH_ORDERING_BASES: Final[frozenset[str]] = frozenset(
    {GRAPH_ORDERING_BASIS_DEPTH_RECORD_VERSION_ASC}
)
"""The only known `GraphOrderingBasis` values. `GraphOrderingBasis` is wire-open, but
ordering evidence is trust-sensitive: an unrecognized value fails closed rather than being
guessed at, since a caller cannot verify determinism against a basis it does not recognize."""

GRAPH_MIN_START_REFERENCES: Final = 1
GRAPH_MAX_START_REFERENCES: Final = 64
"""`GraphTraversalInput.start`'s schema `minItems`/`maxItems`. Restated here because tolerant
decoding never enforces a schema ceiling: a hand-built or `from_wire`-decoded input carrying
65 seeds must fail on the semantic path too, not only against strict JSON Schema."""

GRAPH_MIN_RELATION_TYPE_FILTERS: Final = 1
GRAPH_MAX_RELATION_TYPE_FILTERS: Final = 64
"""`GraphTraversalInput.relation_types`'s schema `minItems`/`maxItems`, restated for the same
reason. Absent means every relation type; present means a bounded, non-empty, duplicate-free
set, since an empty filter asks for nothing at all."""

GRAPH_BOUNDARY_REASON_PAGE: Final = "page_boundary"
GRAPH_BOUNDARY_REASON_DEPTH: Final = "depth_boundary"

GRAPH_BOUNDARY_REASONS: Final[frozenset[str]] = frozenset(
    {GRAPH_BOUNDARY_REASON_PAGE, GRAPH_BOUNDARY_REASON_DEPTH}
)
"""The only known `GraphBoundaryReason` values. `GraphBoundaryReason` is wire-open, but a
boundary reason is trust-sensitive: it is the whole justification for an edge naming only one
of its endpoints, so an unrecognized reason fails closed rather than being accepted as some
future justification this build cannot check."""


def _validate_graph_direction(value: str, label: str = "direction") -> None:
    if len(value) > _BOUNDED_VALUE_MAX_LENGTH or not _GRAPH_DIRECTION_RE.fullmatch(value):
        raise ContractSemanticError(f"{label}: {value!r} is not a valid GraphDirection")
    if value not in GRAPH_DIRECTIONS:
        raise ContractSemanticError(
            f"{label} {value!r} is not a recognized GraphDirection; must be one of "
            f"{sorted(GRAPH_DIRECTIONS)!r}"
        )


def resolve_graph_direction(direction: str | None) -> str:
    """Resolve `direction`, defaulting absent to :data:`GRAPH_DIRECTION_DEFAULT`.

    Validates a present `direction` and fails closed on an unrecognized value, mirroring
    :func:`~omnivia_core.contracts.v1.semantics.resolve_governed_record_view`.

    An exported direct entry point, so a present `direction` is type-guarded before the
    pattern check rather than after it: a caller reaching this without a tolerant `from_wire`
    decode in front (a hand-built DTO, an integer, any other non-string) must see a
    `ContractSemanticError`, never the raw `TypeError` a `len()`/regex call on a non-string
    would otherwise raise.
    """
    if direction is None:
        return GRAPH_DIRECTION_DEFAULT
    value = _require_str(direction, "direction")
    _validate_graph_direction(value)
    return value


def _validate_depth_limit(value: int, label: str) -> None:
    if not (GRAPH_MIN_DEPTH_LIMIT <= value <= GRAPH_MAX_DEPTH_LIMIT):
        raise ContractSemanticError(
            f"{label} {value!r} is outside the bounded range "
            f"[{GRAPH_MIN_DEPTH_LIMIT}, {GRAPH_MAX_DEPTH_LIMIT}]"
        )


def validate_graph_traversal_input(input_: object) -> None:
    """Raise unless `input_` is a structurally and semantically valid `graph.traverse` input.

    Rejects an unrecognized `direction` (fail closed; absent is valid and resolves to
    :data:`GRAPH_DIRECTION_DEFAULT`), a `start` list that is empty, longer than
    :data:`GRAPH_MAX_START_REFERENCES`, malformed, or duplicated by `(record_id, version)`,
    a `relation_types` filter that is empty, longer than
    :data:`GRAPH_MAX_RELATION_TYPE_FILTERS`, malformed, or duplicated, an unrecognized `view`
    selector, a malformed `domain_scope`/`as_of`, a `page` that is present but names no
    continuation position to continue from (:func:`_validate_page_metadata`), and a
    `depth_limit`/`node_limit`/`edge_limit` outside its bounded range. The `start` and
    `relation_types` ceilings are re-enforced here rather than left to strict JSON Schema,
    because the tolerant decoder does not apply schema ceilings: a 65-seed input that
    `from_wire` happily decodes must still fail on the semantic path.

    A present `node_limit` must additionally be at least `len(start)`. The two are not
    independent bounds: a first page owes every requested seed at depth 0
    (:func:`validate_graph_traversal_result`) and may return no more nodes than the node
    limit, so `node_limit < len(start)` asks for a result that satisfies neither rule and
    that no traversal could ever return. Equality is accepted -- a limit exactly the size of
    the seed set buys a first page of nothing but seeds, which is a real, answerable request.
    The bounded `PageLimit` maximum still applies unchanged on top of this.

    A direct entry point: `input_` need not have passed through a tolerant `from_wire`
    decode, so every field access is guarded and a hand-built DTO carrying a wrongly typed
    value raises `ContractSemanticError`, never a raw `TypeError`/`AttributeError`.
    """
    _require_type(input_, GraphTraversalInput, "input_")
    assert isinstance(input_, GraphTraversalInput)
    resolve_graph_direction(input_.direction)

    start = _require_sequence(input_.start, "start")
    if not (GRAPH_MIN_START_REFERENCES <= len(start) <= GRAPH_MAX_START_REFERENCES):
        raise ContractSemanticError(
            f"start names {len(start)} record version reference(s), outside the bounded range "
            f"[{GRAPH_MIN_START_REFERENCES}, {GRAPH_MAX_START_REFERENCES}]"
        )
    seen_start: set[tuple[str, str]] = set()
    for index, reference in enumerate(start):
        label = f"start[{index}]"
        key = _validate_record_version_reference(reference, label)
        if key in seen_start:
            raise ContractSemanticError(f"{label} duplicates start reference {key!r}")
        seen_start.add(key)

    if input_.relation_types is not None:
        relation_types = _require_sequence(input_.relation_types, "relation_types")
        if not (
            GRAPH_MIN_RELATION_TYPE_FILTERS
            <= len(relation_types)
            <= GRAPH_MAX_RELATION_TYPE_FILTERS
        ):
            raise ContractSemanticError(
                f"relation_types names {len(relation_types)} relation type(s), outside the "
                f"bounded range [{GRAPH_MIN_RELATION_TYPE_FILTERS}, "
                f"{GRAPH_MAX_RELATION_TYPE_FILTERS}]"
            )
        seen_relation_types: set[str] = set()
        for index, relation_type in enumerate(relation_types):
            label = f"relation_types[{index}]"
            value = _require_str(relation_type, label)
            _validate_open_code(value, label)
            if value in seen_relation_types:
                raise ContractSemanticError(f"{label} duplicates relation type {value!r}")
            seen_relation_types.add(value)

    if input_.domain_scope is not None:
        validate_record_domain_scope(_require_str(input_.domain_scope, "domain_scope"))
    if input_.view is not None:
        _validate_view_selector(_require_str(input_.view, "view"))
    if input_.as_of is not None:
        _parse_timestamp(_require_str(input_.as_of, "as_of"), "as_of")
    if input_.depth_limit is not None:
        _validate_depth_limit(_require_int(input_.depth_limit, "depth_limit"), "depth_limit")
    if input_.node_limit is not None:
        node_limit = _require_int(input_.node_limit, "node_limit")
        validate_page_limit(node_limit)
        if node_limit < len(start):
            raise ContractSemanticError(
                f"node_limit {node_limit!r} is below the {len(start)} requested start seed(s); "
                "a first page owes every seed at depth 0 and may return no more nodes than the "
                "node limit, so a limit that cannot hold the seeds asks for a result no "
                "traversal could return"
            )
    if input_.edge_limit is not None:
        validate_page_limit(_require_int(input_.edge_limit, "edge_limit"))
    if input_.page is not None:
        _validate_page_metadata(input_.page, "page")


def _validate_page_metadata(page: object, label: str) -> str:
    """Raise unless `page` is a shape-valid *request* `PageMetadata` that actually names a
    continuation position, and return its continuation token.

    Present-but-empty page metadata (wire `page: {}`) is rejected on a request, because a
    request's `page` is the position it asks to continue *from*, and one that names nothing
    asks to continue from nowhere. "Start at the beginning" is stated by omitting the field
    entirely, so an empty one is a third, meaningless spelling that a validator could only
    honour by guessing which of the two the caller meant. It fails closed instead.

    A *result* page reads the other way and uses :func:`_validate_result_page_metadata`: it is
    always present, and `{}` there is the positive statement that the read is exhausted.
    """
    _require_type(page, PageMetadata, label)
    assert isinstance(page, PageMetadata)
    if page.continuation_token is None:
        raise ContractSemanticError(
            f"{label} is present but names no continuation_token; a request's page is the "
            "position it continues from, and one that names nothing can be continued from "
            "nowhere -- an absent page is how 'start at the first page' is stated"
        )
    token = _require_str(page.continuation_token, f"{label}.continuation_token")
    _validate_opaque_token(token, f"{label}.continuation_token")
    return token


def _validate_result_page_metadata(page: object, label: str) -> str | None:
    """Raise unless `page` is a shape-valid *result* `PageMetadata`, and return its
    continuation token, or `None` when the read is exhausted.

    A result's `page` is mandatory and states the position this read reached: a token means
    more remains, and `{}` means it does not. Exhaustion is therefore *stated* rather than
    inferred from a field that is not there, which is the one posture every paginated result
    in this contract shares -- a caller never has to know which result type it is holding to
    know what "no next page" looks like.
    """
    _require_type(page, PageMetadata, label)
    assert isinstance(page, PageMetadata)
    if page.continuation_token is None:
        return None
    token = _require_str(page.continuation_token, f"{label}.continuation_token")
    _validate_opaque_token(token, f"{label}.continuation_token")
    return token


def _validate_graph_record_under_view(
    record: GovernedRecord,
    resolved_view: str,
    resolution_instant: datetime,
    label: str,
) -> None:
    """Raise unless `record` is exactly what `resolved_view` permits a traversal to return.

    Applies the same exact state/authority/reviewer/validity matrix `knowledge.search`
    applies to a searched record, to both a node's wrapped record and an edge's relation
    record: a projection must never become the side channel that hands back under
    `graph.traverse` what `knowledge.search` would refuse. The `history` branch is not
    merely the same shape but literally the same code
    (:func:`_validate_record_under_history_view`), so the two operations cannot drift on
    what proves a version was once canonical. `resolved_view` has already been checked
    against the known `GovernedRecordView` values by the caller, so there is no
    unrecognized-view branch to fall through here.
    """
    identity = record.provenance.identity
    if resolved_view == GOVERNED_RECORD_VIEW_CURRENT_CANONICAL:
        _validate_record_under_current_canonical_view(record, resolution_instant, label)
    elif resolved_view == GOVERNED_RECORD_VIEW_CANDIDATES:
        if (
            identity.layer != GOVERNANCE_LAYER_CANDIDATE
            or identity.governance_state != GOVERNANCE_STATE_CANDIDATE
            or identity.currentness != RECORD_CURRENTNESS_CURRENT
        ):
            raise ContractSemanticError(
                f"{label} is not an exact l1/candidate/current record (layer={identity.layer!r}, "
                f"governance_state={identity.governance_state!r}, "
                f"currentness={identity.currentness!r}) under the candidates view"
            )
        if record.authority_level != AUTHORITY_LEVEL_PROPOSED:
            raise ContractSemanticError(
                f"{label}.authority_level must be exactly {AUTHORITY_LEVEL_PROPOSED!r} "
                f"under the candidates view, got {record.authority_level!r}"
            )
        if record.reviewer is not None:
            raise ContractSemanticError(
                f"{label} carries a reviewer but the candidates view requires none"
            )
    elif resolved_view == GOVERNED_RECORD_VIEW_HISTORY:
        _validate_record_under_history_view(record, resolution_instant, label)


def validate_graph_traversal_result(
    result: object,
    request: object,
    expected_workspace_id: object,
    canonical_resolution_time: object,
    authorized_views: object,
) -> None:
    """Raise unless `result` is an internally coherent, workspace-scoped traversal projection
    that agrees with everything `request` asked for and never grants a view's trust beyond
    what `authorized_views` actually allows.

    `request` is mandatory and is the complete, original `GraphTraversalInput` (re-validated
    here via :func:`validate_graph_traversal_input`, so a caller cannot bypass its checks by
    hand-building one): the filters a traversal was asked for are exactly what makes its
    result checkable, and a validator handed only a view string could not tell a honoured
    `domain_scope`/`relation_types` filter from an ignored one.

    `authorized_views` is the caller's already-validated set of `GovernedRecordView` values a
    capability layer (not implemented here -- no capability identifier is ever frozen in this
    module) has authorized for this request; the *view string alone* on `request` never
    widens trust. `current_canonical` (absent, or explicitly named) is always allowed and is
    never gated on `authorized_views`; `candidates` and `history` must each be explicitly
    requested *and* present in `authorized_views`, and an unrecognized `view` fails closed in
    :func:`validate_graph_traversal_input` before it can resolve to anything.

    Enforces, beyond that: every node and relation record belongs to `expected_workspace_id`
    and is exactly what the resolved view permits (see
    :func:`_validate_graph_record_under_view`); a requested `as_of` equals
    `canonical_resolution_time`, as does `freshness.as_of`, and `freshness` itself satisfies
    the strict shared :func:`validate_projection_freshness` rule; the applied depth/node/edge
    limits are in range and never looser than an explicitly requested one, and `nodes`/`edges`
    never exceed the limits the result itself states; node identities are unique, a node's
    `reference` equals its wrapped record's identity, requested `start` seeds are closed over
    (see below), and no node exceeds the applied depth; every returned edge's
    `relation_type` is one the request asked for when `relation_types` is set;
    `relation_reference` identifies the relation record's own identity exactly and no two
    edges name the same relation record version; a requested `domain_scope` is honoured by
    node *and* relation records; nodes are ordered by `(depth, record_id, version)` and edges
    by the complete `(source.record_id, source.version, relation_type, target.record_id,
    target.version, relation_reference.record_id, relation_reference.version)` tuple (both
    failing closed on an unrecognized `ordering_basis`); every edge names at least one
    endpoint, and an absent endpoint carries exactly one recognized `boundary_reason` that
    actually holds here (see below); and a `page` continuation token is never offered without
    one of the applied limits having been reached.

    Boundary closure: both endpoints absent is always rejected. Both present states a *fully
    materialized* edge -- it forbids `boundary_reason` and requires **both** endpoints to name
    nodes this result actually returned, since an end the traversal did not reach is stated by
    omitting that endpoint with a `boundary_reason`, never by naming a record the result never
    returned. Exactly one absent requires a recognized `boundary_reason`, requires the endpoint
    that *is* present to name a returned node, and requires that reason to hold --
    :data:`GRAPH_BOUNDARY_REASON_PAGE` only when `page` offers a continuation token and
    `nodes` reached `applied_node_limit` exactly, :data:`GRAPH_BOUNDARY_REASON_DEPTH` only
    when the present endpoint's returned node sits at `applied_depth_limit`.

    Seed closure: a traversal answers the question it was asked, and depth is measured from
    the seeds it was asked to start from. Four rules state that, each checked directly and
    each failing closed on its own:

    - on a *first* page -- `request.page` absent, which is how "start at the beginning" is
      stated -- **every** identity in `request.start` is returned as a node, and
    - a returned seed sits at depth 0, never at a nonzero depth. Together these two make a
      first page return every seed exactly once at depth 0, closing the hole where a result
      could omit some or all of its seeds and hand back unrelated nodes instead -- including
      the degenerate case of an empty `nodes` list, which `start`'s own `minItems: 1` makes
      impossible to justify on a first page, and which page metadata in the *result* does not
      excuse: offering a next page says nothing about what this one owed;
    - a node that is *not* a requested seed may never sit at depth 0, on any page. Depth 0
      means "this is where you asked me to start", so an unrelated record claiming it is a
      traversal quietly answering from somewhere else -- the mirror of the rule above, and the
      half that no seed-presence check can cover, since a result may return the seeds it owes
      *and* smuggle in a spurious depth-0 node beside them;
    - on a continuation page -- `request.page` present, and it must name a real continuation
      token before it can mean anything (:func:`validate_graph_traversal_input`) -- a
      requested seed may not be returned at all, at any depth. A continuation page states
      that its seeds were already returned by an earlier page, so repeating one double-counts
      it against this page's node limit and contradicts the page it continues from. It is
      *exempt* from the first rule for exactly that reason, not excused from the rest.

    Two consequences follow and need no separate check: every node on a continuation page
    sits at depth >= 1 (depth 0 requires being a seed, and a seed may not appear at all), and
    the node budget is always big enough to hold what a first page owes --
    `applied_node_limit` may be tighter than a requested `node_limit`, but never below
    `len(request.start)` on a first page, mirroring the `node_limit >= len(start)` rule
    :func:`validate_graph_traversal_input` applies to an explicitly requested limit. That
    applied-limit rule *is* checked here, since it also binds a server that chose the limit
    itself when the request named none.

    Page closure: `page` is always present on a result and states the position this traversal
    reached (:func:`_validate_result_page_metadata`). A continuation token means more remains,
    and it is only honest when one of the applied limits was actually reached -- a traversal
    that stopped short of every limit has nothing left to continue. `{}` is the positive
    statement that the traversal is exhausted, which is the same spelling every other
    paginated result in this contract uses, and it never justifies a `page_boundary`: an
    exhausted read offers no next page for a missing endpoint to be waiting on.

    On the *request* side `{}` still fails closed (:func:`_validate_page_metadata`). The two
    readings are not in tension: a request's `page` says where to continue *from*, and there
    is nothing to continue from in an empty one, while a result's `page` says where this read
    *reached*, and "the end" is a real position.

    A direct entry point: every argument is type-guarded, so a hand-built DTO raises
    `ContractSemanticError`, never a raw `TypeError`/`AttributeError`.
    """
    _require_type(result, GraphTraversalResult, "result")
    assert isinstance(result, GraphTraversalResult)
    _require_type(request, GraphTraversalInput, "request")
    assert isinstance(request, GraphTraversalInput)
    validate_graph_traversal_input(request)
    _validate_workspace_id(
        _require_str(expected_workspace_id, "expected_workspace_id"), "expected_workspace_id"
    )
    resolution_instant = _parse_timestamp(
        _require_str(canonical_resolution_time, "canonical_resolution_time"),
        "canonical_resolution_time",
    )
    authorized = _require_set_of_str(authorized_views, "authorized_views")
    for view in authorized:
        _validate_view_selector(view, "authorized_views entry")

    resolved_view = resolve_governed_record_view(request.view)
    if (
        request.view is not None
        and resolved_view != GOVERNED_RECORD_VIEW_CURRENT_CANONICAL
        and resolved_view not in authorized
    ):
        raise ContractSemanticError(
            f"view {resolved_view!r} was explicitly requested but is not present in "
            f"authorized_views {sorted(authorized)!r}; the view string alone never widens trust"
        )

    if request.as_of is not None and _parse_timestamp(request.as_of, "request.as_of") != resolution_instant:
        raise ContractSemanticError(
            f"request.as_of {request.as_of!r} is not the canonical_resolution_time "
            f"{canonical_resolution_time!r}; a traversal must be served at the instant it "
            "was asked for"
        )

    if validate_projection_freshness(result.freshness, "freshness") != resolution_instant:
        raise ContractSemanticError(
            f"freshness.as_of {result.freshness.as_of!r} is not the canonical_resolution_time "
            f"{canonical_resolution_time!r}; the projection this traversal was served from "
            "must be stated as of the instant it resolved at"
        )

    applied_depth_limit = _require_int(result.applied_depth_limit, "applied_depth_limit")
    _validate_depth_limit(applied_depth_limit, "applied_depth_limit")
    applied_node_limit = _require_int(result.applied_node_limit, "applied_node_limit")
    validate_page_limit(applied_node_limit)
    applied_edge_limit = _require_int(result.applied_edge_limit, "applied_edge_limit")
    validate_page_limit(applied_edge_limit)
    for applied, requested, field_name in (
        (applied_depth_limit, request.depth_limit, "depth_limit"),
        (applied_node_limit, request.node_limit, "node_limit"),
        (applied_edge_limit, request.edge_limit, "edge_limit"),
    ):
        if requested is not None and applied > requested:
            raise ContractSemanticError(
                f"applied_{field_name} {applied!r} exceeds the requested {field_name} "
                f"{requested!r}"
            )
    if request.page is None and applied_node_limit < len(request.start):
        raise ContractSemanticError(
            f"applied_node_limit {applied_node_limit!r} is below the {len(request.start)} "
            "requested start seed(s) on a first page; a first page owes every seed at depth 0 "
            "and may return no more nodes than the limit it states, so a server that tightened "
            "the node limit past the seed count has stated a page it cannot legally return "
            "(a continuation page is exempt, since it repeats no seeds)"
        )

    ordering_basis = _require_str(result.ordering_basis, "ordering_basis")
    if ordering_basis not in GRAPH_ORDERING_BASES:
        raise ContractSemanticError(
            f"ordering_basis {ordering_basis!r} is not a recognized GraphOrderingBasis; "
            f"must be one of {sorted(GRAPH_ORDERING_BASES)!r}"
        )

    continuation_token = _validate_result_page_metadata(result.page, "page")

    nodes = _require_sequence(result.nodes, "nodes")
    edges = _require_sequence(result.edges, "edges")
    if len(nodes) > applied_node_limit:
        raise ContractSemanticError(
            f"result carries {len(nodes)} node(s), exceeding its own applied_node_limit "
            f"{applied_node_limit!r}"
        )
    if len(edges) > applied_edge_limit:
        raise ContractSemanticError(
            f"result carries {len(edges)} edge(s), exceeding its own applied_edge_limit "
            f"{applied_edge_limit!r}"
        )

    seed_keys = {(reference.record_id, reference.version) for reference in request.start}
    requested_relation_types = (
        frozenset(request.relation_types) if request.relation_types is not None else None
    )

    node_depths: dict[tuple[str, str], int] = {}
    previous_node_sort_key: tuple[int, str, str] | None = None
    for index, node in enumerate(nodes):
        label = f"nodes[{index}]"
        _require_type(node, GraphNode, label)
        assert isinstance(node, GraphNode)
        key = _validate_record_version_reference(node.reference, f"{label}.reference")
        if key in node_depths:
            raise ContractSemanticError(f"{label} duplicates node identity {key!r}")

        depth = _require_int(node.depth, f"{label}.depth")
        _validate_depth_limit(depth, f"{label}.depth")
        if depth > applied_depth_limit:
            raise ContractSemanticError(
                f"{label}.depth {depth!r} exceeds applied_depth_limit {applied_depth_limit!r}"
            )
        is_seed = key in seed_keys
        if is_seed and request.page is not None:
            raise ContractSemanticError(
                f"{label} repeats requested start seed {key!r} on a continuation page; a "
                "continuation page states that its seeds were already returned by an earlier "
                "page -- that is what carrying request.page means -- so returning one again at "
                "any depth double-counts it against this page's node limit and contradicts the "
                "page it continues from"
            )
        if is_seed and depth != 0:
            raise ContractSemanticError(
                f"{label} is a requested start seed {key!r} but its depth is {depth!r}, not 0"
            )
        if depth == 0 and not is_seed:
            raise ContractSemanticError(
                f"{label} sits at depth 0 but {key!r} is not a requested start seed; depth is "
                "measured from the seeds this traversal was asked to start from, so depth 0 is "
                "exactly those seeds and nothing else"
            )
        node_depths[key] = depth

        sort_key = (depth, key[0], key[1])
        if previous_node_sort_key is not None and sort_key < previous_node_sort_key:
            raise ContractSemanticError(
                f"{label} breaks the deterministic (depth, record_id, version) node order: "
                f"{sort_key!r} sorts before the preceding node's {previous_node_sort_key!r}"
            )
        previous_node_sort_key = sort_key

        _require_type(node.record, GovernedRecord, f"{label}.record")
        validate_governed_record(node.record)
        if node.record.workspace_id != expected_workspace_id:
            raise ContractSemanticError(
                f"{label}.record.workspace_id {node.record.workspace_id!r} does not match the "
                f"selected workspace {expected_workspace_id!r}"
            )
        record_identity = node.record.provenance.identity
        if (record_identity.record_id, record_identity.version) != key:
            raise ContractSemanticError(f"{label}.record identity does not match {label}.reference")
        if request.domain_scope is not None and node.record.domain_scope != request.domain_scope:
            raise ContractSemanticError(
                f"{label}.record.domain_scope {node.record.domain_scope!r} does not match the "
                f"requested domain_scope {request.domain_scope!r}"
            )
        _validate_graph_record_under_view(node.record, resolved_view, resolution_instant, f"{label}.record")

    if request.page is None:
        missing_seeds = sorted(key for key in seed_keys if key not in node_depths)
        if missing_seeds:
            raise ContractSemanticError(
                f"the first page of this traversal returned no node for requested start "
                f"seed(s) {missing_seeds!r}; every identity a traversal was asked to start "
                "from must be returned on its first page, so a result can never quietly "
                "answer a narrower question than the one asked (a continuation page states "
                "that its seeds were already returned by carrying request.page)"
            )

    seen_relation_keys: set[tuple[str, str]] = set()
    previous_edge_sort_key: tuple[str, str, str, str, str, str, str] | None = None
    for index, edge in enumerate(edges):
        label = f"edges[{index}]"
        _require_type(edge, GraphEdge, label)
        assert isinstance(edge, GraphEdge)
        relation_type = _require_str(edge.relation_type, f"{label}.relation_type")
        _validate_open_code(relation_type, f"{label}.relation_type")
        if requested_relation_types is not None and relation_type not in requested_relation_types:
            raise ContractSemanticError(
                f"{label}.relation_type {relation_type!r} was not among the requested "
                f"relation_types {sorted(requested_relation_types)!r}"
            )

        _require_type(edge.record, GovernedRecord, f"{label}.record")
        validate_governed_record(edge.record)
        if edge.record.workspace_id != expected_workspace_id:
            raise ContractSemanticError(
                f"{label}.record.workspace_id {edge.record.workspace_id!r} does not match the "
                f"selected workspace {expected_workspace_id!r}"
            )
        if request.domain_scope is not None and edge.record.domain_scope != request.domain_scope:
            raise ContractSemanticError(
                f"{label}.record.domain_scope {edge.record.domain_scope!r} does not match the "
                f"requested domain_scope {request.domain_scope!r}"
            )
        _validate_graph_record_under_view(edge.record, resolved_view, resolution_instant, f"{label}.record")

        relation_identity = edge.record.provenance.identity
        relation_key = (relation_identity.record_id, relation_identity.version)
        if _validate_record_version_reference(edge.relation_reference, f"{label}.relation_reference") != relation_key:
            raise ContractSemanticError(
                f"{label}.relation_reference does not identify {label}.record's own identity "
                f"{relation_key!r}; the relation record is referenced, never re-identified"
            )
        if relation_key in seen_relation_keys:
            raise ContractSemanticError(f"{label} duplicates relation record identity {relation_key!r}")
        seen_relation_keys.add(relation_key)

        endpoints: dict[str, tuple[str, str] | None] = {}
        for endpoint_name, endpoint in (("source", edge.source), ("target", edge.target)):
            endpoints[endpoint_name] = (
                None
                if endpoint is None
                else _validate_record_version_reference(endpoint, f"{label}.{endpoint_name}")
            )
        present = {name: key for name, key in endpoints.items() if key is not None}
        if not present:
            raise ContractSemanticError(
                f"{label} names neither a source nor a target; an edge with no endpoint at all "
                "states nothing this result can be trusted about"
            )

        if len(present) == 2:
            if edge.boundary_reason is not None:
                raise ContractSemanticError(
                    f"{label} carries boundary_reason {edge.boundary_reason!r} but both endpoints "
                    "are present; there is no boundary to justify"
                )
            unreturned = [
                name for name, endpoint_key in present.items() if endpoint_key not in node_depths
            ]
            if unreturned:
                raise ContractSemanticError(
                    f"{label} names both endpoints, which states a fully materialized edge, "
                    f"but this result returned no node for: {', '.join(unreturned)} "
                    "(dangling edge); an end this traversal did not reach is stated by "
                    "omitting that endpoint with a boundary_reason, never by naming it"
                )
        else:
            ((present_name, present_key),) = present.items()
            absent_name = "target" if present_name == "source" else "source"
            if edge.boundary_reason is None:
                raise ContractSemanticError(
                    f"{label} omits its {absent_name} but states no boundary_reason; a missing "
                    "endpoint must be justified, never implied"
                )
            reason = _require_str(edge.boundary_reason, f"{label}.boundary_reason")
            _validate_open_code(reason, f"{label}.boundary_reason")
            if reason not in GRAPH_BOUNDARY_REASONS:
                raise ContractSemanticError(
                    f"{label}.boundary_reason {reason!r} is not a recognized GraphBoundaryReason; "
                    f"must be one of {sorted(GRAPH_BOUNDARY_REASONS)!r}"
                )
            if present_key not in node_depths:
                raise ContractSemanticError(
                    f"{label} omits its {absent_name} and its {present_name} {present_key!r} is "
                    "not a node this result returned; a boundary edge must anchor to a returned node"
                )
            if reason == GRAPH_BOUNDARY_REASON_PAGE:
                if continuation_token is None:
                    raise ContractSemanticError(
                        f"{label} claims {GRAPH_BOUNDARY_REASON_PAGE!r} but this result offers no "
                        "page continuation token; nothing was left for a further page"
                    )
                if len(nodes) != applied_node_limit:
                    raise ContractSemanticError(
                        f"{label} claims {GRAPH_BOUNDARY_REASON_PAGE!r} but this result carries "
                        f"{len(nodes)} node(s) against applied_node_limit {applied_node_limit!r}; "
                        "the node limit was not reached exactly"
                    )
            elif node_depths[present_key] != applied_depth_limit:
                raise ContractSemanticError(
                    f"{label} claims {GRAPH_BOUNDARY_REASON_DEPTH!r} but its {present_name} sits at "
                    f"depth {node_depths[present_key]!r}, not at applied_depth_limit "
                    f"{applied_depth_limit!r}"
                )

        source_key = endpoints["source"] or ("", "")
        target_key = endpoints["target"] or ("", "")
        edge_sort_key = (
            source_key[0],
            source_key[1],
            relation_type,
            target_key[0],
            target_key[1],
            relation_key[0],
            relation_key[1],
        )
        if previous_edge_sort_key is not None and edge_sort_key < previous_edge_sort_key:
            raise ContractSemanticError(
                f"{label} breaks the deterministic (source, relation_type, target, "
                f"relation_reference) edge order: {edge_sort_key!r} sorts before the preceding "
                f"edge's {previous_edge_sort_key!r}"
            )
        previous_edge_sort_key = edge_sort_key

    if continuation_token is not None:
        node_limit_reached = len(nodes) >= applied_node_limit
        edge_limit_reached = len(edges) >= applied_edge_limit
        if not node_limit_reached and not edge_limit_reached:
            raise ContractSemanticError(
                "page offers a continuation token but neither applied_node_limit nor "
                "applied_edge_limit was reached; a snapshot-stable traversal has nothing "
                "left to continue"
            )

# --- context_pack.build --------------------------------------------------------
#
# A Context Pack is a content-addressed artifact, so everything below is written for one
# question: can a second party, holding only the wire document, recompute this pack's own
# identity and check every claim it makes about how it was built? That rules out opaque
# fingerprints (uncheckable), tolerant field handling on the integrity path (a dropped
# unknown field changes what was hashed), and non-deterministic array order (two equal
# packs would hash differently). Each rule below exists to close one of those.

CONTEXT_PACK_MODE_DETERMINISTIC_VIEW: Final = "deterministic_view"
"""The only `ContextPackMode` v1 recognizes: a regenerated, non-persisted deterministic
view of current canonical knowledge."""

CONTEXT_PACK_MODES: Final[frozenset[str]] = frozenset({CONTEXT_PACK_MODE_DETERMINISTIC_VIEW})
"""The complete set of recognized `ContextPackMode` values. `ContextPackMode` is wire-open
so a compatible minor release can add vocabulary, but mode is trust-sensitive: an
unrecognized mode names a build posture this build cannot verify, so it fails closed rather
than being guessed at."""

CONTEXT_PACK_REJECTED_MODES: Final[frozenset[str]] = frozenset(
    {"immutable_snapshot", "returned_artifact"}
)
"""Mode spellings that are named and rejected rather than merely unrecognized.

`immutable_snapshot` asks for a persisted artifact, and `returned_artifact` was never a
wire mode at all -- it describes an operation posture (synchronous, non-persisting), not
something a caller selects. Both fail exactly as closed as an unknown value; they are
enumerated only so the failure says *why* rather than leaving a caller to guess that a
plausible-looking mode was simply never implemented."""

CONTEXT_PACK_MIN_TOKEN_BUDGET: Final = 1
CONTEXT_PACK_MAX_TOKEN_BUDGET: Final = 10_000_000
"""`ContextPackTokenBudget`'s schema bounds. A budget is strictly positive: a pack built
against no budget can carry no content, so zero states a request no build could answer."""

CONTEXT_PACK_MAX_TOKEN_COUNT: Final = 10_000_000
"""`ContextPackTokenCount`'s schema maximum. Observed counts start at zero, unlike budgets:
an empty pack really did consume nothing."""

CONTEXT_PACK_FORMAT_VERSION: Final = "1.0"
"""The only `reproducibility.pack_format_version` v1 verifies. The checksum rule below is
defined against exactly this artifact format, so a reader must know which format it is
checking before it checks anything."""

CONTEXT_PACK_ARTIFACT_CANONICALIZATION: Final = "rfc8785"
"""The only `reproducibility.artifact_canonicalization` v1 verifies. A checksum means
nothing without the canonical form it was computed over, so an unrecognized value fails
closed rather than being verified under a guessed one."""

CONTEXT_PACK_DIGEST_ALGORITHM: Final = "sha256"
"""The one digest algorithm `ContextPackDigest` admits, and the prefix it is spelled with."""

CONTEXT_PACK_SUMMARIZER_DISABLED: Final = "disabled"
"""The exact `reproducibility.summarizer_version` a build that used no summarizer states.
Required rather than omitted, so "summarized nothing" and "never recorded the summarizer"
are two different documents rather than the same absent field."""

CONTEXT_PACK_AUTHORIZED_CANDIDATE_SET_FORMAT: Final = (
    "omnivia.context-pack.authorized-candidate-set.v1"
)
"""The exact `format` member of the authorized-candidate-set digest preimage.

Inside the hashed document rather than around it: a digest that did not cover the name of
the thing it digests could be replayed against a differently shaped set of the same
members."""

CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE: Final = "evidence"
CONTEXT_PACK_CANDIDATE_PARTITION_RECORDS: Final = "records"
CONTEXT_PACK_CANDIDATE_PARTITION_HISTORY: Final = "history"
CONTEXT_PACK_CANDIDATE_PARTITION_CONTEXT_MODELS: Final = "context_models"

CONTEXT_PACK_GOVERNED_CANDIDATE_PARTITIONS: Final[frozenset[str]] = frozenset(
    {
        CONTEXT_PACK_CANDIDATE_PARTITION_RECORDS,
        CONTEXT_PACK_CANDIDATE_PARTITION_HISTORY,
        CONTEXT_PACK_CANDIDATE_PARTITION_CONTEXT_MODELS,
    }
)
"""The three governed partitions a `(record_id, version)` candidate may sit in.

One governed version belongs to exactly one of them at a given instant, so a repeated
identity *across* two of these is as much a contradiction as a repeat within one."""

CONTEXT_PACK_CANDIDATE_PARTITIONS: Final[frozenset[str]] = frozenset(
    {CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE}
) | CONTEXT_PACK_GOVERNED_CANDIDATE_PARTITIONS
"""Every partition an authorized candidate may name, and the complete set.

Closed rather than open, unlike most codes in this contract: the partition is the only
domain separation the digest preimage carries besides the workspace, so an unrecognized one
names a frontier this layer cannot reason about and fails closed."""

CONTEXT_PACK_NORMALIZED_REQUEST_VIEW: Final = GOVERNED_RECORD_VIEW_CURRENT_CANONICAL
"""The only `reproducibility.normalized_request.view` v1 recognizes.

A Context Pack resolves current canonical knowledge, plus the history and context models
that support it, and `ContextPackBuildInput` carries no view selector at all. The resolved
view is therefore a constant this contract states rather than something a caller chooses --
and stating it explicitly is what makes a pack that claims to have read `candidates` or a
wider `history` view fail rather than pass unnoticed."""

CONTEXT_PACK_REJECTED_INPUT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "view",
        "as_of",
        "page",
        "limit",
        "continuation_token",
        "persist",
        "persistence",
        "expires_at",
        "expiry",
        "retention_policy",
        "snapshot",
        "snapshot_id",
        "async",
        "job",
        "job_id",
    }
)
"""Request controls `context_pack.build` deliberately does not have, refused by name.

`ContextPackBuildInput`'s JSON Schema already excludes every one of these
(`unevaluatedProperties: false`), but the tolerant decoder production actually runs ignores
unknown fields by design -- so a payload asking for a persisted, expiring, paginated,
point-in-time, or asynchronous pack would otherwise decode cleanly with its request quietly
dropped, and the caller would receive a synchronous non-persisted pack believing it had
asked for something else. Silently ignoring a control is the failure mode this set exists
to prevent, so :func:`decode_context_pack_build_input` refuses them *before* the tolerant
decode rather than after it.

Three families, each an operation posture this v1 read does not have: a widened or
historical selection (`view`, `as_of`), a paginated one (`page`, `limit`,
`continuation_token`), and a persisted or deferred one (`persist`, `persistence`,
`expires_at`, `expiry`, `retention_policy`, `snapshot`, `snapshot_id`, `async`, `job`,
`job_id`). The set is frozen: adding a genuinely new control is a schema change, never a
name that quietly stops being refused.

Refused as *top-level members of the request*, and only there -- see
:func:`_reject_context_pack_input_controls` for why going deeper would break the ADR-038
additive-compatibility rule this contract is built on."""

_CONTEXT_PACK_MAX_SECTIONS: Final = 256
_CONTEXT_PACK_MAX_CITATIONS: Final = 2000
_CONTEXT_PACK_MAX_SELECTED_ITEMS: Final = 500
_CONTEXT_PACK_MAX_RECORD_VERSIONS: Final = 1500
_CONTEXT_PACK_MAX_AUTHORIZED_CANDIDATES: Final = 20_000
#: `ContextPackAuthorizedCandidateSetManifest.candidates`'s schema `maxItems`. A frontier is
#: wider than what a pack selects from it -- the selected ceilings are 500 evidence and 1500
#: governed versions -- so the bound is far above both, and exists only so an adversarial
#: manifest fails as a contract error rather than as an unbounded hash.
_CONTEXT_PACK_MAX_STATEMENTS: Final = 256
_CONTEXT_PACK_MAX_SECTION_CITATION_IDS: Final = 256
_CONTEXT_PACK_MIN_CONFLICT_CITATION_IDS: Final = 2
_CONTEXT_PACK_MAX_CONFLICT_CITATION_IDS: Final = 64
_CONTEXT_PACK_MIN_UNCERTAINTY_CITATION_IDS: Final = 1
_CONTEXT_PACK_MAX_UNCERTAINTY_CITATION_IDS: Final = 64
_CONTEXT_PACK_MIN_SCOPES: Final = 1
_CONTEXT_PACK_MAX_SCOPES: Final = 64
_CONTEXT_PACK_MAX_AUTHORITY_ROLES: Final = 64
_CONTEXT_PACK_MAX_AUTHORITY_CAPABILITIES: Final = 256
_CONTEXT_PACK_MAX_SECTION_CONTENT_LENGTH: Final = 16_384
_CONTEXT_PACK_MAX_SECTION_TITLE_LENGTH: Final = 256
_CONTEXT_PACK_MAX_EXCERPT_LENGTH: Final = 4096
_CONTEXT_PACK_MAX_OMISSION_PATH_LENGTH: Final = 1024
_CONTEXT_PACK_MAX_OMISSION_MESSAGE_LENGTH: Final = 2048
"""Every bound above is restated from the JSON Schema rather than delegated to it, for the
reason every other ceiling in this module is: the tolerant decoder never applies a schema
ceiling, so a hand-built or `from_wire`-decoded pack carrying 300 sections must fail on the
semantic path too, not only against strict JSON Schema."""

_CONTEXT_PACK_DIGEST_RE: Final = re.compile(CONTEXT_PACK_DIGEST_PATTERN)
_SCOPE_RE: Final = re.compile(SCOPE_PATTERN)
_CAPABILITY_ID_RE: Final = re.compile(CAPABILITY_ID_PATTERN)
_CONTRACT_VERSION_RE: Final = re.compile(CONTRACT_VERSION_PATTERN)
_EVIDENCE_ID_RE: Final = re.compile(EVIDENCE_ID_PATTERN)
_EVIDENCE_CHECKSUM_RE: Final = re.compile(EVIDENCE_CHECKSUM_PATTERN)
_GOVERNED_RECORD_TYPE_RE: Final = re.compile(GOVERNED_RECORD_TYPE_PATTERN)

_EVIDENCE_CHECKSUM_MAX_LENGTH: Final = 256


class _CitationFacts(NamedTuple):
    """What one validated citation contributes to the pack-wide cross-checks.

    `target` is the exact immutable thing cited, tagged by kind so an evidence identity and
    a record identity can never collide in the same lookup; `locator` is the whole
    `(target, content_pointer, source_span)` tuple two citations may not both carry, which
    is what makes "the same passage cited twice" a duplicate while "the same document cited
    at two different spans" stays two distinct citations.
    """

    citation_id: str
    target: tuple[str, str, str]
    locator: tuple[tuple[str, str, str], str | None, SourceSpan | None]


def _validate_context_pack_mode(value: str, label: str = "mode") -> None:
    """Raise unless `value` is the one `ContextPackMode` this build recognizes."""
    if len(value) > _BOUNDED_VALUE_MAX_LENGTH or not _CONTEXT_PACK_MODE_RE.fullmatch(value):
        raise ContractSemanticError(f"{label}: {value!r} is not a valid ContextPackMode")
    if value in CONTEXT_PACK_REJECTED_MODES:
        raise ContractSemanticError(
            f"{label} {value!r} is not a v1 ContextPackMode: `context_pack.build` is a "
            "synchronous, non-persisting read, so it offers no persisted or deferred build "
            f"posture; the only recognized mode is "
            f"{CONTEXT_PACK_MODE_DETERMINISTIC_VIEW!r}"
        )
    if value not in CONTEXT_PACK_MODES:
        raise ContractSemanticError(
            f"{label} {value!r} is not a recognized ContextPackMode; must be one of "
            f"{sorted(CONTEXT_PACK_MODES)!r}"
        )


def _validate_token_count(value: int, label: str) -> None:
    """Raise unless `value` is a `ContextPackTokenCount` -- an observed, non-negative count."""
    if not (0 <= value <= CONTEXT_PACK_MAX_TOKEN_COUNT):
        raise ContractSemanticError(
            f"{label} {value!r} is outside the bounded range [0, {CONTEXT_PACK_MAX_TOKEN_COUNT}]"
        )


def _validate_token_budget(value: object, label: str) -> int:
    """Raise unless `value` is a `ContextPackTokenBudget`, and return it.

    `bool` is refused ahead of `int` by :func:`_require_int`, deliberately: Python makes
    `True` an integer equal to 1, so a payload carrying `token_budget: true` would otherwise
    pass every range check and be honoured as a one-token budget.
    """
    budget = _require_int(value, label)
    if not (CONTEXT_PACK_MIN_TOKEN_BUDGET <= budget <= CONTEXT_PACK_MAX_TOKEN_BUDGET):
        raise ContractSemanticError(
            f"{label} {budget!r} is outside the bounded range "
            f"[{CONTEXT_PACK_MIN_TOKEN_BUDGET}, {CONTEXT_PACK_MAX_TOKEN_BUDGET}]; a Context "
            "Pack budget is strictly positive, since a pack built against no budget can "
            "carry no content"
        )
    return budget


def _validate_context_pack_digest(value: object, label: str) -> str:
    """Raise unless `value` is a `ContextPackDigest`, and return it.

    Deliberately stricter than :func:`_validate_opaque_token`: this is not a token a client
    round-trips but a value an independent implementation must recompute and compare, so
    exactly one algorithm, one length, and one letter case are admitted. Uppercase hex, a
    truncated digest, and any other algorithm prefix are all rejected rather than normalized.
    """
    digest = _require_str(value, label)
    if not _CONTEXT_PACK_DIGEST_RE.fullmatch(digest):
        raise ContractSemanticError(
            f"{label}: {digest!r} is not a valid ContextPackDigest; expected "
            f"{CONTEXT_PACK_DIGEST_ALGORITHM!r} followed by exactly 64 lowercase hexadecimal "
            "characters"
        )
    return digest


def _validate_scope(value: object, label: str) -> str:
    scope = _require_str(value, label)
    if len(scope) > _BOUNDED_VALUE_MAX_LENGTH or not _SCOPE_RE.fullmatch(scope):
        raise ContractSemanticError(f"{label}: {scope!r} is not a valid Scope")
    return scope


def _validate_capability_id(value: object, label: str) -> str:
    capability_id = _require_str(value, label)
    if len(capability_id) > _BOUNDED_VALUE_MAX_LENGTH or not _CAPABILITY_ID_RE.fullmatch(
        capability_id
    ):
        raise ContractSemanticError(f"{label}: {capability_id!r} is not a valid CapabilityId")
    return capability_id


def _validate_contract_version(value: object, label: str) -> str:
    version = _require_str(value, label)
    if len(version) > 32 or not _CONTRACT_VERSION_RE.fullmatch(version):
        raise ContractSemanticError(f"{label}: {version!r} is not a valid ContractVersion")
    return version


def _validate_evidence_id(value: object, label: str) -> str:
    evidence_id = _require_str(value, label)
    if len(evidence_id) > _BOUNDED_VALUE_MAX_LENGTH or not _EVIDENCE_ID_RE.fullmatch(evidence_id):
        raise ContractSemanticError(f"{label}: {evidence_id!r} is not a valid EvidenceId")
    return evidence_id


def _validate_evidence_checksum(value: object, label: str) -> str:
    checksum = _require_str(value, label)
    if len(checksum) > _EVIDENCE_CHECKSUM_MAX_LENGTH or not _EVIDENCE_CHECKSUM_RE.fullmatch(
        checksum
    ):
        raise ContractSemanticError(f"{label}: {checksum!r} is not a valid EvidenceChecksum")
    return checksum


def _validate_governed_record_type(value: object, label: str) -> str:
    record_type = _require_str(value, label)
    if len(record_type) > _BOUNDED_VALUE_MAX_LENGTH or not _GOVERNED_RECORD_TYPE_RE.fullmatch(
        record_type
    ):
        raise ContractSemanticError(f"{label}: {record_type!r} is not a valid GovernedRecordType")
    return record_type


def _validate_bounded_text(
    value: object, label: str, *, minimum: int, maximum: int
) -> str:
    text = _require_str(value, label)
    if not (minimum <= len(text) <= maximum):
        raise ContractSemanticError(
            f"{label} length {len(text)} is outside the bounded range [{minimum}, {maximum}]"
        )
    return text


def _require_ascending(
    key: tuple[bytes, ...], previous: tuple[bytes, ...] | None, label: str, ordering: str
) -> tuple[bytes, ...]:
    """Raise unless `key` sorts strictly after `previous`, and return `key`.

    *Strictly*, not merely non-decreasing: two entries with equal ordering keys are
    interchangeable, so their relative order carries no information and two documents that
    differ only by swapping them would be the same pack with two different checksums.
    Rejecting the tie is what makes the order a function of the content.

    Keys are UTF-16 code-unit sort keys (:func:`~canonical_json.utf16_sort_key`), the same
    ordering RFC 8785 imposes on object member names. Using anything else here -- Python's
    code-point ordering, most obviously -- would let a pack be canonically ordered by this
    contract's rule and out of order by the one its own checksum is computed under.
    """
    if previous is not None and key <= previous:
        raise ContractSemanticError(
            f"{label} breaks the deterministic ascending {ordering} order; identity-bearing "
            "arrays are ordered by unsigned UTF-16 code unit, and equal keys are rejected "
            "rather than tolerated so the order is a function of the content"
        )
    return key


def _identity_sort_key(*parts: str) -> tuple[bytes, ...]:
    """The ordering key for a compound identity such as `(record_id, version)`."""
    return tuple(utf16_sort_key(part) for part in parts)


# --- context_pack.build: input -------------------------------------------------


def validate_context_pack_build_input(input_: object) -> None:
    """Raise unless `input_` is a structurally and semantically valid `context_pack.build`
    input.

    Five fields and nothing else: a bounded non-empty `query`, the one recognized `mode`, a
    strictly positive `token_budget`, and optional `domain_scope`/`record_type` selection
    filters. There is no view selector, no point-in-time selector, no pagination, and no
    persistence control to validate, because the operation has none --
    :data:`CONTEXT_PACK_REJECTED_INPUT_FIELDS` and
    :func:`decode_context_pack_build_input` are what stop a raw payload from smuggling one
    past the tolerant decoder.

    A direct entry point: `input_` need not have passed through a tolerant `from_wire`
    decode, so every field access is guarded and a hand-built DTO carrying a wrongly typed
    value raises `ContractSemanticError`, never a raw `TypeError`/`AttributeError`.
    """
    _require_type(input_, ContextPackBuildInput, "input_")
    assert isinstance(input_, ContextPackBuildInput)
    _validate_context_pack_mode(_require_str(input_.mode, "mode"))
    _validate_bounded_text(
        input_.query, "query", minimum=_QUERY_MIN_LENGTH, maximum=_QUERY_MAX_LENGTH
    )
    _validate_token_budget(input_.token_budget, "token_budget")
    if input_.domain_scope is not None:
        validate_record_domain_scope(_require_str(input_.domain_scope, "domain_scope"))
    if input_.record_type is not None:
        _validate_governed_record_type(input_.record_type, "record_type")


def _reject_context_pack_input_controls(payload: object, path: str) -> None:
    """Raise if `payload` names any control in :data:`CONTEXT_PACK_REJECTED_INPUT_FIELDS`
    as a *top-level member* of the request.

    Top-level only, deliberately, and this is the one place the reasoning has to be written
    down because the wider scan looks stricter and is in fact wrong.

    ADR-038 requires an older production client to ignore an additive unknown optional
    field rather than fail on it, which is exactly what the tolerant decoder does. A name
    like `future_envelope` is therefore *dropped whole* -- and everything under it goes with
    it. A `future_envelope.snapshot_id` never reaches `ContextPackBuildInput`, never reaches
    :func:`validate_context_pack_build_input`, and cannot influence the five scalar fields
    the v1 DTO has: there is no field for it to land in. Refusing it would make this
    contract reject a document a compatible later minor release is entitled to send, on the
    strength of a name nested inside a member v1 does not read.

    What *can* be silently honoured-in-part is a control at the top level, because that is
    the only depth at which a name competes with a field this operation actually decodes.
    That is what this scan covers, and the reason it runs on the raw payload before the
    tolerant decode has erased the evidence.

    The contrast with :func:`_reject_reserved_governance_fields` is real and intended: there
    the reserved names are *server-owned output* fields that a nested mutation input really
    does carry positions for (a whole `MemoryCreateInput` sits inside `replacement`), so the
    scan must descend -- and exempts only the opaque `content` payload. Here the input has
    no nested contract shape at all: five scalars, and nothing below them.
    """
    if not isinstance(payload, Mapping):
        return
    present = sorted(CONTEXT_PACK_REJECTED_INPUT_FIELDS & set(payload))
    if present:
        raise ContractSemanticError(
            f"{path}: `context_pack.build` accepts no such control(s): {present}. This "
            "operation is a synchronous, non-persisting read of the current canonical view: "
            "it offers no view or point-in-time selector, no pagination, and no persistence, "
            "expiry, retention, snapshot, or job control. The request is refused rather than "
            "honoured in part, because a caller that asked for one of these and received a "
            "pack anyway would be told nothing about what it actually got"
        )


def decode_context_pack_build_input(
    payload: object, path: str = "ContextPackBuildInput"
) -> ContextPackBuildInput:
    """Decode and fully validate `payload` into a semantically valid `ContextPackBuildInput`.

    The order matters and is the whole point: the rejected-control scan
    (:func:`_reject_context_pack_input_controls`) runs on the *raw* payload, before the
    tolerant `from_wire` decode has had a chance to drop the very fields being looked for.
    Running it afterwards would be running it on a document those fields had already been
    erased from.

    That scan covers the request's *top-level* members only. An unknown optional field with
    a reserved-looking name nested under it is dropped with its unknown parent and decodes
    cleanly, exactly as ADR-038 requires of an older client meeting a newer document.
    """
    _reject_context_pack_input_controls(payload, path)
    input_ = ContextPackBuildInput.from_wire(payload, path)
    validate_context_pack_build_input(input_)
    return input_


# --- context_pack.build: content-addressed integrity ---------------------------


def compute_context_pack_artifact_digest(result_wire: object) -> str:
    """Return the `sha256:` digest of one Context Pack result's wire object.

    The rule, exactly:

    1. start from the complete strict `ContextPackBuildResult` wire object;
    2. remove exactly two members -- the top-level `pack_id` and
       `reproducibility.artifact_checksum` -- because those two are what the digest
       *becomes*, and nothing else, so the generation time, the authority context, the
       freshness statement, every policy and configuration version, the budget, the
       sections, the citations, and every selected item are all covered;
    3. canonicalize what remains under RFC 8785 (:func:`~canonical_json.canonicalize`);
    4. SHA-256 the canonical UTF-8 bytes;
    5. spell the result `sha256:` followed by 64 lowercase hex characters.

    Both removed members must actually be present: a document missing either is not a strict
    result, and silently digesting it would produce a value that verifies against nothing.

    **Boundary.** This helper takes an *already strict, trusted* object: a mapping decoded
    by a parser that has by then discarded any duplicated member name, keeping only the last
    occurrence. It cannot recover what that parser dropped, so it can only prove that the
    object it was handed hashes to the stated digest -- not that the document on the wire
    contained exactly this object. Verifying *that* requires the raw text, which is what
    :func:`verify_context_pack_artifact_document` exists for and why the raw path is the one
    an integrity check at a trust boundary must use.
    """
    mapping = _require_mapping(result_wire, "result_wire")
    if "pack_id" not in mapping:
        raise ContractSemanticError(
            "result_wire.pack_id is absent; the digest is defined as the hash of a complete "
            "result with pack_id removed, so a result that never carried one cannot be one"
        )
    reproducibility = mapping.get("reproducibility")
    nested = _require_mapping(reproducibility, "result_wire.reproducibility")
    if "artifact_checksum" not in nested:
        raise ContractSemanticError(
            "result_wire.reproducibility.artifact_checksum is absent; the digest is defined "
            "as the hash of a complete result with it removed"
        )
    reduced = {key: value for key, value in mapping.items() if key != "pack_id"}
    reduced["reproducibility"] = {
        key: value for key, value in nested.items() if key != "artifact_checksum"
    }
    digest = sha256(canonical_bytes(reduced, "ContextPackBuildResult")).hexdigest()
    return f"{CONTEXT_PACK_DIGEST_ALGORITHM}:{digest}"


def _verify_context_pack_digest(result_wire: Mapping[str, object], label: str) -> str:
    """Raise unless `result_wire`'s `pack_id` and `artifact_checksum` are both the digest of
    its own content, and return that digest.

    The two are checked against each other *and* against the computed value, rather than one
    against the other: agreeing with each other only proves a document is internally
    consistent, which a document that lies about both is too.
    """
    reproducibility = _require_mapping(result_wire.get("reproducibility"), f"{label}.reproducibility")

    stated_format = _validate_contract_version(
        reproducibility.get("pack_format_version"), f"{label}.reproducibility.pack_format_version"
    )
    _require_exact(
        stated_format, CONTEXT_PACK_FORMAT_VERSION, f"{label}.reproducibility.pack_format_version"
    )
    canonicalization = _require_str(
        reproducibility.get("artifact_canonicalization"),
        f"{label}.reproducibility.artifact_canonicalization",
    )
    _validate_open_code(canonicalization, f"{label}.reproducibility.artifact_canonicalization")
    if canonicalization != CONTEXT_PACK_ARTIFACT_CANONICALIZATION:
        raise ContractSemanticError(
            f"{label}.reproducibility.artifact_canonicalization {canonicalization!r} is not "
            f"{CONTEXT_PACK_ARTIFACT_CANONICALIZATION!r}; a checksum can only be verified "
            "against the exact canonical form it was computed over, never a guessed one"
        )

    pack_id = _validate_context_pack_digest(result_wire.get("pack_id"), f"{label}.pack_id")
    checksum = _validate_context_pack_digest(
        reproducibility.get("artifact_checksum"), f"{label}.reproducibility.artifact_checksum"
    )
    computed = compute_context_pack_artifact_digest(result_wire)
    if checksum != computed:
        raise ContractSemanticError(
            f"{label}.reproducibility.artifact_checksum {checksum!r} is not the "
            f"RFC 8785 SHA-256 digest of this pack's own content ({computed!r})"
        )
    if pack_id != computed:
        raise ContractSemanticError(
            f"{label}.pack_id {pack_id!r} is not the RFC 8785 SHA-256 digest of this pack's "
            f"own content ({computed!r}); a Context Pack's identity is its content"
        )
    return computed


def verify_context_pack_artifact_document(document: str | bytes) -> ContextPackBuildResult:
    """Verify a raw Context Pack document's integrity and return the decoded result.

    The entry point for an integrity check at a trust boundary, and the only one that can
    honestly perform one. Three things, in this order, none of which the DTO path can do
    once a parser has run:

    - parse with duplicate-member detection (:func:`~canonical_json.parse_json_document`). A
      document naming one member twice has no single value, and an ordinary parser resolves
      that by keeping the last occurrence -- so by the time a mapping exists, the ambiguity
      is unrecoverable and any digest computed from it attests to a document nobody sent;
    - require the raw object to round-trip *exactly* through the generated DTO. The
      production decoder ignores unknown fields by design (ADR-038), which is right for
      ordinary decoding and fatal here: an unknown additive field silently dropped before
      hashing means the bytes that were verified are not the bytes that arrived. Comparing
      `to_wire()` against the parsed document catches that recursively, at every depth, for
      unknown members and for anything else the strict shape does not admit;
    - only then compute and compare the digest (:func:`_verify_context_pack_digest`), and
      only after the pack's own declared format version and canonicalization have been
      checked -- verifying a checksum under a canonicalization the document did not claim
      would prove nothing about it.

    Ordinary, non-integrity decoding of a Context Pack stays tolerant, exactly as ADR-038
    requires: `ContextPackBuildResult.from_wire` is unchanged, and a caller that is not
    checking integrity should keep using it.

    **Integrity only, and the name means it.** This function proves that the bytes it was
    handed are exactly the bytes the stated `pack_id` and `artifact_checksum` were computed
    over. It proves nothing about *what those bytes say*: it does not check the authority
    context, the selected partitions, the citations, the resolution-time closure, or the
    authorized candidate set, and in particular it never verifies
    `authorization_context.authorized_candidate_set_checksum`, since doing that requires an
    independent manifest this function is not given (see
    :func:`compute_authorized_candidate_set_checksum`). A self-consistent artifact built by a
    dishonest producer passes here by construction. For the full rule set, including
    candidate-set verification, use :func:`validate_context_pack_build_result_document`.
    """
    parsed = parse_json_document(document)
    mapping = _require_mapping(parsed, "document")
    result = ContextPackBuildResult.from_wire(mapping, "ContextPackBuildResult")
    if result.to_wire() != mapping:
        raise ContractSemanticError(
            "document does not round-trip exactly through the strict v1 "
            "ContextPackBuildResult shape: it carries member(s) the strict shape does not "
            "declare, or spells one in a way the strict shape does not preserve. The "
            "tolerant decoder would drop them, so the bytes verified would not be the bytes "
            "received, and the digest would attest to a document that was never sent"
        )
    _verify_context_pack_digest(mapping, "document")
    return result


# --- context_pack.build: the authorized candidate set --------------------------
#
# `authorization_context.authorized_candidate_set_checksum` is the one claim in a pack that
# an integrity check cannot touch: it is a digest of something that never appears in the
# artifact. Left there, it is a number a producer writes down about itself -- and a producer
# that ranked material the caller was not entitled to see can write down whatever digest
# makes its pack verify. The only way to check it is to recompute it from an independent
# statement of what the frontier actually was, which is what this section is for.


class _AuthorizedCandidateSet(NamedTuple):
    """One validated authorized candidate set, reduced to what a verifier needs.

    `checksum` is the digest :func:`compute_authorized_candidate_set_checksum` returns;
    `members` is the same frontier as `(partition, first component, second component)`
    triples, which is the form a selected-item membership test asks a question of. Both come
    out of one validation pass so the digest a pack is checked against and the set its
    selections are checked against can never be two different readings of one manifest.
    """

    checksum: str
    members: frozenset[tuple[str, str, str]]


class _CandidateIdentity(NamedTuple):
    """One candidate's exact digest-preimage object, the key it sorts under, and its triple.

    `members` is the object that goes into the preimage verbatim -- three members, in
    declaration order, and nothing else. `sort_key` is `(partition, remaining components)`
    under UTF-16 code-unit ordering, and `triple` is the same three strings positionally, for
    duplicate detection and membership tests. All three are built together so they can never
    disagree about which strings a candidate is.
    """

    sort_key: tuple[bytes, ...]
    triple: tuple[str, str, str]
    members: dict[str, str]


def _validate_candidate_record_id(value: object, label: str) -> str:
    """Return one candidate's `record_id`, or raise -- exactly a `RecordId`."""
    record_id = _require_str(value, label)
    _validate_record_id(record_id, label)
    return record_id


def _validate_candidate_record_version(value: object, label: str) -> str:
    """Return one candidate's `version`, or raise -- exactly a `RecordVersion`."""
    version = _require_str(value, label)
    _validate_record_version(version, label)
    return version


def _describe_authorized_candidate(candidate: object, label: str) -> _CandidateIdentity:
    """Raise unless `candidate` is one well-formed authorized candidate, and return it.

    Every identity component is held to the *exact* domain of the item it names --
    `EvidenceId` and `EvidenceChecksum` for an L0 artifact, `RecordId` and `RecordVersion`
    for a governed version -- through the same validators the artifacts and records
    themselves pass. A preimage naming identities wider than the items it is a frontier of
    could attest to a candidate no artifact and no record could ever be, which is exactly
    the membership claim a verifier reads this digest to check.
    """
    _require_type(
        candidate,
        (ContextPackAuthorizedEvidenceCandidate, ContextPackAuthorizedRecordCandidate),
        label,
    )
    assert isinstance(
        candidate,
        (ContextPackAuthorizedEvidenceCandidate, ContextPackAuthorizedRecordCandidate),
    )
    partition = _require_str(candidate.partition, f"{label}.partition")
    if partition not in CONTEXT_PACK_CANDIDATE_PARTITIONS:
        raise ContractSemanticError(
            f"{label}.partition {partition!r} is not one of "
            f"{sorted(CONTEXT_PACK_CANDIDATE_PARTITIONS)!r}; the partition is the only domain "
            "separation this digest carries besides the workspace, so an unrecognized one "
            "names a frontier this contract cannot reason about and fails closed"
        )
    if isinstance(candidate, ContextPackAuthorizedEvidenceCandidate):
        if partition != CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE:
            raise ContractSemanticError(
                f"{label}.partition {partition!r} names a governed partition but the candidate "
                "carries an evidence identity; an L0 artifact is only ever authorized in "
                f"{CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE!r}"
            )
        evidence_id = _validate_evidence_id(candidate.evidence_id, f"{label}.evidence_id")
        content_checksum = _validate_evidence_checksum(
            candidate.content_checksum, f"{label}.content_checksum"
        )
        return _CandidateIdentity(
            _identity_sort_key(partition, evidence_id, content_checksum),
            (partition, evidence_id, content_checksum),
            {
                "partition": partition,
                "evidence_id": evidence_id,
                "content_checksum": content_checksum,
            },
        )

    assert isinstance(candidate, ContextPackAuthorizedRecordCandidate)
    if partition not in CONTEXT_PACK_GOVERNED_CANDIDATE_PARTITIONS:
        raise ContractSemanticError(
            f"{label}.partition {partition!r} names the L0 evidence partition but the "
            "candidate carries a governed identity; a governed version is authorized in one "
            f"of {sorted(CONTEXT_PACK_GOVERNED_CANDIDATE_PARTITIONS)!r}"
        )
    record_id = _validate_candidate_record_id(candidate.record_id, f"{label}.record_id")
    version = _validate_candidate_record_version(candidate.version, f"{label}.version")
    return _CandidateIdentity(
        _identity_sort_key(partition, record_id, version),
        (partition, record_id, version),
        {"partition": partition, "record_id": record_id, "version": version},
    )


def compute_authorized_candidate_set_checksum(manifest: object) -> str:
    """Return the `sha256:` digest of one authorized candidate set.

    **What the frozen set is.** The complete candidate frontier *after* retrieval, after the
    request's own scope, and after workspace, scope, purpose, capability, policy, ACL, and
    sensitivity authorization -- and *before* the first ranking, reranking, selection, or
    budget decision. It is neither the whole workspace nor merely what the pack selected: a
    digest of the workspace would say nothing about what this build was allowed to see, and a
    digest of the selected items would be a restatement of the pack rather than a check on
    it. Nothing may enter a pack after this frontier is frozen, which is exactly why every
    selected identity has to be a member of it
    (:func:`validate_context_pack_build_result` enforces that; this function computes the
    digest the enforcement compares against).

    **What is in the preimage, and what is deliberately not.** Workspace domain separation
    plus immutable identities, and nothing else. Content, excerpts, provenance, spans, scores,
    distances, ranks, tie-breaks, selection flags, citations, sections, query and
    normalization state, authority, policy and configuration versions, and projection
    versions are all excluded: the digest must depend on *which authorized material existed*
    and on nothing a later ranking or selection step could change, or two honest builds over
    the same frontier would disagree. Unauthorized, request-filtered, tombstoned, and
    invalid-at-resolution items are simply absent from the set rather than marked in it.

    **Collisions are refused, never collapsed.** An exactly repeated tuple; one `evidence_id`
    paired with two different content checksums; one governed `(record_id, version)` repeated
    within a partition, or claimed by two different governed partitions. Each is a
    contradiction about what the frontier was, and a digest that silently deduplicated one
    would attest to a set nobody assembled. Two *different* versions of one record are two
    ordinary candidates whenever both were independently eligible, and an unknown partition
    fails closed.

    **Identities.** Exactly the domains of the items being named: `EvidenceId` and
    `EvidenceChecksum` for an L0 artifact, `RecordId` and `RecordVersion` for a governed
    version, validated by the same helpers those items are validated by. A frontier is a
    statement about which real items were authorized, so admitting an identity no artifact
    and no record could carry would let the digest attest to a membership that cannot exist.

    **Ordering.** The candidates are a set, so a flat array is sorted internally by
    `(partition, remaining identity components in declaration order)`, comparing each
    component by unsigned UTF-16 code unit with no Unicode normalization -- the same ordering
    RFC 8785 imposes on object member names. RFC 8785 sorts object members but says nothing
    about array elements, so without this sort a shuffled manifest would digest differently;
    with it the resulting partition order is `context_models`, `evidence`, `history`,
    `records`.

    The UTF-16 rule is normative because the sort and the canonicalization it feeds must be
    the same ordering; it is not a claim that a v1 identity can exercise the difference. All
    four v1 identity alphabets above are ASCII or printable ASCII, where UTF-16 code-unit
    order and code-point order coincide, so no valid candidate distinguishes them. Where the
    two genuinely diverge is object *member* names under RFC 8785, which the canonicalizer
    orders for arbitrary Unicode -- :func:`~canonical_json.utf16_sort_key` is where that
    divergence lives and is proved.

    **The preimage and the digest.**

        {"format": CONTEXT_PACK_AUTHORIZED_CANDIDATE_SET_FORMAT,
         "workspace_id": "<WorkspaceId>",
         "candidates": [<sorted exact identity objects>]}

    hashed as `"sha256:" + lowercase_hex(SHA-256(UTF-8(JCS(preimage))))`. The empty set is
    valid and has a well-defined digest; for workspace `ws-1` the canonical bytes are
    `{"candidates":[],"format":"omnivia.context-pack.authorized-candidate-set.v1","workspace_id":"ws-1"}`.

    A direct entry point: every argument is type-guarded, so a hand-built manifest raises
    `ContractSemanticError` and never a raw `TypeError`/`AttributeError`.
    """
    return _summarize_authorized_candidate_set(manifest).checksum


def _summarize_authorized_candidate_set(manifest: object) -> _AuthorizedCandidateSet:
    """Validate one candidate-set manifest and return its digest and its membership set.

    The whole rule set :func:`compute_authorized_candidate_set_checksum` documents lives
    here, so the digest a pack's stated checksum is compared against and the membership every
    selected item is tested for come from one reading of one manifest rather than two.
    """
    _require_type(manifest, ContextPackAuthorizedCandidateSetManifest, "manifest")
    assert isinstance(manifest, ContextPackAuthorizedCandidateSetManifest)

    stated_format = _require_str(manifest.format, "manifest.format")
    _require_exact(
        stated_format, CONTEXT_PACK_AUTHORIZED_CANDIDATE_SET_FORMAT, "manifest.format"
    )
    workspace_id = _require_str(manifest.workspace_id, "manifest.workspace_id")
    _validate_workspace_id(workspace_id, "manifest.workspace_id")

    candidates = _require_sequence(manifest.candidates, "manifest.candidates")
    if len(candidates) > _CONTEXT_PACK_MAX_AUTHORIZED_CANDIDATES:
        raise ContractSemanticError(
            f"manifest.candidates carries {len(candidates)} entries, exceeding the maximum of "
            f"{_CONTEXT_PACK_MAX_AUTHORIZED_CANDIDATES}"
        )

    identities: list[_CandidateIdentity] = []
    seen_tuples: set[tuple[str, str, str]] = set()
    evidence_checksums: dict[str, str] = {}
    governed_partitions: dict[tuple[str, str], str] = {}
    for index, candidate in enumerate(candidates):
        label = f"manifest.candidates[{index}]"
        identity = _describe_authorized_candidate(candidate, label)
        tuple_key = identity.triple
        if tuple_key in seen_tuples:
            raise ContractSemanticError(
                f"{label} repeats the candidate {tuple_key!r}; the frontier is a set, and a "
                "repeated tuple states nothing a single one does not"
            )
        seen_tuples.add(tuple_key)
        if isinstance(candidate, ContextPackAuthorizedEvidenceCandidate):
            evidence_id = identity.members["evidence_id"]
            checksum = identity.members["content_checksum"]
            previous_checksum = evidence_checksums.get(evidence_id)
            if previous_checksum is not None:
                raise ContractSemanticError(
                    f"{label} pairs evidence_id {evidence_id!r} with content checksum "
                    f"{checksum!r}, but the frontier already pairs it with "
                    f"{previous_checksum!r}; one artifact cannot have been authorized in two "
                    "content states at one instant"
                )
            evidence_checksums[evidence_id] = checksum
        else:
            governed_key = (identity.members["record_id"], identity.members["version"])
            previous_partition = governed_partitions.get(governed_key)
            if previous_partition is not None:
                raise ContractSemanticError(
                    f"{label} authorizes governed version {governed_key!r} in partition "
                    f"{identity.members['partition']!r}, but the frontier already authorizes "
                    f"it in {previous_partition!r}; one version is current, historical, or a "
                    "context model at a given instant, never two of them"
                )
            governed_partitions[governed_key] = identity.members["partition"]
        identities.append(identity)

    identities.sort(key=lambda item: item.sort_key)
    preimage = {
        "format": CONTEXT_PACK_AUTHORIZED_CANDIDATE_SET_FORMAT,
        "workspace_id": workspace_id,
        "candidates": [item.members for item in identities],
    }
    digest = sha256(
        canonical_bytes(preimage, "ContextPackAuthorizedCandidateSetManifest")
    ).hexdigest()
    return _AuthorizedCandidateSet(
        checksum=f"{CONTEXT_PACK_DIGEST_ALGORITHM}:{digest}",
        members=frozenset(item.triple for item in identities),
    )


# --- context_pack.build: selected partitions -----------------------------------


#: The `RecordTemporalMetadata` members `canonical_resolution_time` bounds, in declaration
#: order. `valid_from`, `valid_until`, and `superseded_at` are deliberately absent: those
#: three describe a window and a replacement rather than an act, and each already has its own
#: rule (:func:`_validate_record_valid_at_canonical_resolution_time` and the per-partition
#: supersession checks) that a blanket upper bound would contradict -- a version superseded
#: only *after* a pack resolved is exactly what that pack was entitled to select.
_BOUNDED_TEMPORAL_INSTANTS: Final[tuple[str, ...]] = (
    "event_at",
    "observed_at",
    "ingested_at",
    "recorded_at",
)


def _reject_future_instant(
    value: object,
    label: str,
    *,
    resolution_instant: datetime,
    resolution_time: str,
) -> None:
    """Raise unless the timestamp at `label` is at or before `resolution_instant`.

    The single comparison the pack's whole resolution-time closure is built from. Inclusive
    by definition: an act occurring exactly at the instant a pack resolved at had happened by
    it, so equality passes and only a strictly later instant is refused.
    """
    text = _require_str(value, label)
    if _parse_timestamp(text, label) > resolution_instant:
        raise ContractSemanticError(
            f"{label} is {text!r}, after the canonical-resolution time {resolution_time!r}; a "
            "pack cannot present an act or observation that had not happened at the instant "
            "it resolved at"
        )


def _reject_future_source(
    source: object,
    label: str,
    *,
    resolution_instant: datetime,
    resolution_time: str,
) -> None:
    """Bound one `SourceReference`'s optional `retrieved_at`."""
    _require_type(source, SourceReference, label)
    assert isinstance(source, SourceReference)
    if source.retrieved_at is not None:
        _reject_future_instant(
            source.retrieved_at,
            f"{label}.retrieved_at",
            resolution_instant=resolution_instant,
            resolution_time=resolution_time,
        )


def _reject_future_evidence_references(
    references: object,
    label: str,
    *,
    resolution_instant: datetime,
    resolution_time: str,
) -> None:
    """Bound every `EvidenceReference`'s source retrieval instant in one list."""
    for index, reference in enumerate(_require_sequence(references, label)):
        entry_label = f"{label}[{index}]"
        _require_type(reference, EvidenceReference, entry_label)
        assert isinstance(reference, EvidenceReference)
        _reject_future_source(
            reference.source,
            f"{entry_label}.source",
            resolution_instant=resolution_instant,
            resolution_time=resolution_time,
        )


def _reject_future_temporal(
    temporal: object,
    label: str,
    *,
    resolution_instant: datetime,
    resolution_time: str,
) -> None:
    """Bound the four event instants of one `RecordTemporalMetadata`."""
    _require_type(temporal, RecordTemporalMetadata, label)
    assert isinstance(temporal, RecordTemporalMetadata)
    for name in _BOUNDED_TEMPORAL_INSTANTS:
        value = getattr(temporal, name)
        if value is not None:
            _reject_future_instant(
                value,
                f"{label}.{name}",
                resolution_instant=resolution_instant,
                resolution_time=resolution_time,
            )


def _reject_future_provenance(
    history: object,
    label: str,
    *,
    resolution_instant: datetime,
    resolution_time: str,
) -> None:
    """Raise if any provenance entry in `history`, or any source it cites, reaches past
    `resolution_instant`.

    An item's own audit trail is a second, independent set of instants from its `temporal`
    envelope, and it nests: an entry records when an act occurred, and each piece of evidence
    that entry cites records when its source was retrieved. Both levels are bounded, because
    an entry stamped in the past that cites material retrieved in the future is the same
    impossibility one level down.
    """
    for index, entry in enumerate(_require_sequence(history, label)):
        entry_label = f"{label}[{index}]"
        _require_type(entry, ProvenanceEntry, entry_label)
        assert isinstance(entry, ProvenanceEntry)
        _reject_future_instant(
            entry.occurred_at,
            f"{entry_label}.occurred_at",
            resolution_instant=resolution_instant,
            resolution_time=resolution_time,
        )
        if entry.evidence is not None:
            _reject_future_evidence_references(
                entry.evidence,
                f"{entry_label}.evidence",
                resolution_instant=resolution_instant,
                resolution_time=resolution_time,
            )


def _reject_future_evidence_instants(
    artifact: EvidenceArtifact,
    label: str,
    *,
    resolution_instant: datetime,
    resolution_time: str,
) -> None:
    """Close the resolution-time bound over every instant a selected L0 artifact carries.

    `temporal.event_at`, `observed_at`, `ingested_at`, and `recorded_at`; the retrieval
    instant of the source it was captured from; and, for every entry in its own
    `provenance_history`, the instant that act occurred and the retrieval instant of every
    piece of evidence the entry cites.

    Complete rather than representative, and that is the point: the top-level rules each read
    one instant off the `temporal` envelope, so an artifact recorded at T0 could carry a
    source retrieved a year later, or an audit entry citing evidence retrieved a year later,
    and still pass every one of them. A pack that selects such an item presents, as of the
    moment it resolved at, an observation or an act that had not happened yet -- the
    deterministic-view guarantee read backwards.

    Applied only to *selection into a pack*, and only after the intrinsic artifact rules have
    run (:func:`~semantics_evidence.validate_evidence_artifact`): this is what a Context Pack
    may present at one instant, not a new rule about what a valid artifact is. The rejection
    is a refusal, never a repair -- provenance is append-only, so this contract does not
    rewrite, truncate, or filter an item to make it selectable.
    """
    _reject_future_temporal(
        artifact.temporal,
        f"{label}.temporal",
        resolution_instant=resolution_instant,
        resolution_time=resolution_time,
    )
    _reject_future_source(
        artifact.source,
        f"{label}.source",
        resolution_instant=resolution_instant,
        resolution_time=resolution_time,
    )
    _reject_future_provenance(
        artifact.provenance_history,
        f"{label}.provenance_history",
        resolution_instant=resolution_instant,
        resolution_time=resolution_time,
    )


def _reject_future_record_instants(
    provenance: RecordProvenance,
    label: str,
    *,
    resolution_instant: datetime,
    resolution_time: str,
) -> None:
    """Close the resolution-time bound over every instant a selected governed record carries.

    The governed counterpart of :func:`_reject_future_evidence_instants`, and the same rule
    at every depth `RecordProvenance` reaches: the four `temporal` event instants; the
    retrieval instant of every declared source; every history entry's `occurred_at` and the
    retrieval instant of every piece of evidence it cites; the assertion's `asserted_at` and
    the retrieval instant of every piece of evidence *it* cites; and the extraction's
    `extracted_at`.

    `assertion.proposed_valid_from` and `proposed_valid_until` are deliberately not bounded.
    They are *proposed effective dates* rather than acts: asserting today that something takes
    effect next year is an ordinary claim about the future, and refusing it would make a
    forward-dated proposal unselectable for reasons that have nothing to do with when the pack
    resolved. Existing validity and supersession semantics are untouched for the same reason,
    and no causal or ordering rule is added between these instants -- the only claim made here
    is that none of them is later than the instant the pack resolved at.
    """
    _reject_future_temporal(
        provenance.temporal,
        f"{label}.temporal",
        resolution_instant=resolution_instant,
        resolution_time=resolution_time,
    )
    for index, source in enumerate(_require_sequence(provenance.sources, f"{label}.sources")):
        _reject_future_source(
            source,
            f"{label}.sources[{index}]",
            resolution_instant=resolution_instant,
            resolution_time=resolution_time,
        )
    _reject_future_provenance(
        provenance.history,
        f"{label}.history",
        resolution_instant=resolution_instant,
        resolution_time=resolution_time,
    )
    assertion = provenance.assertion
    if assertion is not None:
        _require_type(assertion, CandidateAssertion, f"{label}.assertion")
        _reject_future_instant(
            assertion.asserted_at,
            f"{label}.assertion.asserted_at",
            resolution_instant=resolution_instant,
            resolution_time=resolution_time,
        )
        _reject_future_evidence_references(
            assertion.evidence,
            f"{label}.assertion.evidence",
            resolution_instant=resolution_instant,
            resolution_time=resolution_time,
        )
    extraction = provenance.extraction
    if extraction is not None:
        _require_type(extraction, CandidateExtractionMetadata, f"{label}.extraction")
        _reject_future_instant(
            extraction.extracted_at,
            f"{label}.extraction.extracted_at",
            resolution_instant=resolution_instant,
            resolution_time=resolution_time,
        )


def _validate_selected_evidence(
    artifact: object,
    label: str,
    *,
    expected_workspace_id: str,
    resolution_instant: datetime,
    resolution_time: str,
) -> tuple[str, str]:
    """Raise unless `artifact` is L0 evidence this pack was entitled to select, and return
    its exact `(evidence_id, content_checksum)` identity.

    Composes the intrinsic evidence rule
    (:func:`~semantics_evidence.validate_evidence_artifact`: identity, checksum and media
    type shape, temporal coherence, tombstone/provenance agreement) and adds only what
    selection *into a pack* means on top of it:

    - the artifact belongs to the selected workspace;
    - it is not tombstoned. A tombstoned artifact's history is never erased, but citing it
      as live supporting material is exactly what the tombstone withdrew;
    - every instant it carries is at or before the canonical-resolution instant
      (:func:`_reject_future_evidence_instants`): the four `temporal` event instants,
      `recorded_at` among them, so a pack cannot cite material that did not yet exist when it
      resolved; its source's `retrieved_at`; and, for every entry in its own
      `provenance_history`, that entry's `occurred_at` together with the `retrieved_at` of
      every source the entry's evidence cites. Complete rather than representative -- each of
      the other rules here reads a single instant off the `temporal` envelope, and the audit
      trail and its nested source retrievals are a second and third independent set that
      would otherwise be free to record acts that had not happened when the pack resolved;
    - its validity window, where it has one, contains that instant
      (:func:`_validate_record_valid_at_canonical_resolution_time` -- the same rule governed
      records are held to, since "valid then" means the same thing for both);
    - it had not been superseded at or before that instant.

    Deliberately *not* applied: the request's `domain_scope`/`record_type` filters.
    `EvidenceArtifact` carries neither field by design -- it is raw L0 material, not
    governed knowledge -- so a filter applied here could only be invented.
    """
    _require_type(artifact, EvidenceArtifact, label)
    assert isinstance(artifact, EvidenceArtifact)
    validate_evidence_artifact(artifact)
    if artifact.workspace_id != expected_workspace_id:
        raise ContractSemanticError(
            f"{label}.workspace_id {artifact.workspace_id!r} does not match the selected "
            f"workspace {expected_workspace_id!r}"
        )
    if artifact.tombstoned:
        raise ContractSemanticError(
            f"{label} is tombstoned; a tombstoned artifact's history is preserved, but "
            "selecting it into a pack presents it as live supporting material, which is "
            "exactly what tombstoning withdrew"
        )
    temporal = artifact.temporal
    _reject_future_evidence_instants(
        artifact,
        label,
        resolution_instant=resolution_instant,
        resolution_time=resolution_time,
    )
    _validate_record_valid_at_canonical_resolution_time(temporal, resolution_instant, label)
    if temporal.superseded_at is not None:
        superseded_at = _parse_timestamp(
            _require_str(temporal.superseded_at, f"{label}.temporal.superseded_at"),
            f"{label}.temporal.superseded_at",
        )
        if superseded_at <= resolution_instant:
            raise ContractSemanticError(
                f"{label} was superseded at {temporal.superseded_at!r}, at or before the "
                f"canonical-resolution time {resolution_time!r}"
            )
    return (
        _validate_evidence_id(artifact.evidence_id, f"{label}.evidence_id"),
        _validate_evidence_checksum(artifact.content_checksum, f"{label}.content_checksum"),
    )


def _validate_selected_record(
    record: object,
    label: str,
    *,
    expected_workspace_id: str,
    resolution_instant: datetime,
    resolution_time: str,
    request: ContextPackBuildInput,
    layer: str,
    historical: bool,
    partition: str,
) -> tuple[str, str]:
    """Raise unless `record` is a governed record this pack was entitled to select into
    `partition`, and return its exact `(record_id, version)` identity.

    Everything shared by all three governed partitions is here, and everything that differs
    between them is a parameter, so `records`, `history`, and `context_models` cannot drift
    into three subtly different notions of canonical:

    - the record is individually valid
      (:func:`~semantics.validate_governed_record`) and belongs to the selected workspace;
    - it satisfies the request's `domain_scope` and `record_type` filters, where set. Unlike
      L0 evidence, a governed record carries both fields, so an unfiltered record here is a
      request the pack answered more broadly than it was asked;
    - every instant its provenance carries is at or before the canonical-resolution instant
      (:func:`_reject_future_record_instants`, the governed counterpart of the rule the
      evidence partition is held to): the four `temporal` event instants including
      `recorded_at`, every declared source's `retrieved_at`, every history entry's
      `occurred_at` and the `retrieved_at` of every source that entry's evidence cites, the
      assertion's `asserted_at` and the `retrieved_at` of every source *its* evidence cites,
      and the extraction's `extracted_at`. An audit trail reaching past the instant a pack
      resolved at is the deterministic-view guarantee read backwards, whichever partition it
      sits in. The assertion's `proposed_valid_from`/`proposed_valid_until` are excluded on
      purpose: a proposed effective date in the future is an ordinary claim, not an act;
    - and it is exactly what its partition permits: `history` is the shared historical rule
      (:func:`_validate_record_under_history_view`, literally the same code
      `knowledge.search` and `graph.traverse` apply); `records` and `context_models` are the
      shared current-canonical rule
      (:func:`_validate_record_under_current_canonical_view`) at `l2` and `l3` respectively,
      plus an explicit restatement that a current version records no supersession.

    That last restatement is defence in depth rather than the only line:
    :func:`~semantics.validate_record_currentness_consistency`, composed by
    `validate_governed_record`, already refuses a `current` record carrying a
    `superseded_at`. It is stated here anyway so this function states the complete selection
    bar on its own, and so loosening that universal rule cannot silently widen what a pack
    may present as current.
    """
    _require_type(record, GovernedRecord, label)
    assert isinstance(record, GovernedRecord)
    validate_governed_record(record)
    # `content` is opaque application payload that `validate_governed_record` deliberately
    # never inspects, so nothing above has established it is even a mapping. A Context Pack
    # is the one place that matters: the digest re-encodes the whole result, so a hand-built
    # record carrying `None` here would surface as a raw `AttributeError` from the encoder
    # rather than as a `ContractSemanticError` from this layer.
    _require_mapping(record.content, f"{label}.content")
    if record.workspace_id != expected_workspace_id:
        raise ContractSemanticError(
            f"{label}.workspace_id {record.workspace_id!r} does not match the selected "
            f"workspace {expected_workspace_id!r}"
        )
    if request.domain_scope is not None and record.domain_scope != request.domain_scope:
        raise ContractSemanticError(
            f"{label}.domain_scope {record.domain_scope!r} does not match the requested "
            f"domain_scope {request.domain_scope!r}"
        )
    if request.record_type is not None and record.record_type != request.record_type:
        raise ContractSemanticError(
            f"{label}.record_type {record.record_type!r} does not match the requested "
            f"record_type {request.record_type!r}"
        )

    temporal = record.provenance.temporal
    _reject_future_record_instants(
        record.provenance,
        f"{label}.provenance",
        resolution_instant=resolution_instant,
        resolution_time=resolution_time,
    )

    if historical:
        _validate_record_under_history_view(record, resolution_instant, label)
    else:
        _validate_record_under_current_canonical_view(
            record,
            resolution_instant,
            label,
            layer=layer,
            view_label=f"the Context Pack {partition!r} partition",
        )
        if temporal.superseded_at is not None:
            raise ContractSemanticError(
                f"{label} records a superseded_at instant {temporal.superseded_at!r} but is "
                f"offered in the current {partition!r} partition"
            )
    identity = record.provenance.identity
    return (
        _require_str(identity.record_id, f"{label}.provenance.identity.record_id"),
        _require_str(identity.version, f"{label}.provenance.identity.version"),
    )


# --- context_pack.build: citations and sections --------------------------------


def _validate_source_span(span: object, label: str) -> None:
    """Raise unless `span` is a shape-valid `records.SourceSpan`.

    Reuses `records.SourceSpan` rather than a Context-Pack-specific string, so a span in a
    citation means exactly what a span in a record's evidence means, including the offset
    ordering rule: a span that ends before it starts addresses nothing.
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
            raise ContractSemanticError(f"{label}.start_offset must not be negative")
    end = None
    if span.end_offset is not None:
        end = _require_int(span.end_offset, f"{label}.end_offset")
        if end < 0:
            raise ContractSemanticError(f"{label}.end_offset must not be negative")
    if start is not None and end is not None and start > end:
        raise ContractSemanticError(
            f"{label}.start_offset {start!r} is after {label}.end_offset {end!r}"
        )


def _describe_context_pack_citation(citation: object, label: str) -> _CitationFacts:
    """Raise unless `citation` is a shape-valid citation, and return what the pack-wide
    cross-checks need from it.

    Which of the two things a citation points at is settled *structurally* in v1 -- the wire
    shape is a `oneOf` of two distinct object types, so "both" and "neither" are decode
    failures rather than semantic ones. What is left for this function is the shape of the
    target itself, the bounded opaque locator, and the reusable `records.SourceSpan`.

    `content_pointer` and `source_span` are bounded opaque locators here and nothing more:
    v1 states their limits and declines to state their syntax or how they resolve against a
    particular target, because that is target-specific and this provider-neutral layer would
    be guessing.
    """
    _require_type(
        citation, (ContextPackEvidenceCitation, ContextPackRecordCitation), label
    )
    assert isinstance(citation, (ContextPackEvidenceCitation, ContextPackRecordCitation))
    citation_id = _require_str(citation.citation_id, f"{label}.citation_id")
    _validate_identifier(citation_id, f"{label}.citation_id")

    target: tuple[str, str, str]
    if isinstance(citation, ContextPackEvidenceCitation):
        reference = citation.evidence_reference
        _require_type(reference, ContextPackEvidenceReference, f"{label}.evidence_reference")
        assert isinstance(reference, ContextPackEvidenceReference)
        target = (
            "evidence",
            _validate_evidence_id(reference.evidence_id, f"{label}.evidence_reference.evidence_id"),
            _validate_evidence_checksum(
                reference.content_checksum, f"{label}.evidence_reference.content_checksum"
            ),
        )
    else:
        record_id, version = _validate_record_version_reference(
            citation.record_reference, f"{label}.record_reference"
        )
        target = ("record", record_id, version)

    if citation.content_pointer is not None:
        _validate_bounded_text(
            citation.content_pointer,
            f"{label}.content_pointer",
            minimum=0,
            maximum=_LOCATOR_MAX_LENGTH,
        )
    if citation.source_span is not None:
        _validate_source_span(citation.source_span, f"{label}.source_span")
    if citation.excerpt is not None:
        _validate_bounded_text(
            citation.excerpt,
            f"{label}.excerpt",
            minimum=0,
            maximum=_CONTEXT_PACK_MAX_EXCERPT_LENGTH,
        )
    return _CitationFacts(
        citation_id=citation_id,
        target=target,
        locator=(target, citation.content_pointer, citation.source_span),
    )


def validate_context_pack_citation(citation: object, label: str = "citation") -> None:
    """Raise unless `citation` is a shape-valid `ContextPackCitation`.

    A direct entry point over :func:`_describe_context_pack_citation`, for a caller
    validating one citation outside the context of a whole pack. It checks everything
    intrinsic to the citation and nothing that needs the pack: whether the target was
    actually selected, whether the citation is used by a section, and whether it duplicates
    another are all pack-wide facts, checked by
    :func:`validate_context_pack_build_result`.
    """
    _describe_context_pack_citation(citation, label)


def _validate_citation_id_list(
    values: object,
    label: str,
    citation_ids: AbstractSet[str],
    *,
    minimum: int,
    maximum: int,
) -> tuple[str, ...]:
    """Raise unless `values` is a bounded, duplicate-free, ascending list of citation
    identifiers that all resolve, and return it.

    The one rule behind every citation-id list on a pack -- a section's, a conflict's, an
    uncertainty's -- so the three cannot disagree on what "resolves" or "in order" means.
    Resolution is against the citations this pack actually returned: an identifier naming
    nothing is a dangling reference, and a list is not a place to introduce one.
    """
    items = _require_sequence(values, label)
    if not (minimum <= len(items) <= maximum):
        raise ContractSemanticError(
            f"{label} names {len(items)} citation id(s), outside the bounded range "
            f"[{minimum}, {maximum}]"
        )
    previous: tuple[bytes, ...] | None = None
    resolved: list[str] = []
    for index, value in enumerate(items):
        item_label = f"{label}[{index}]"
        citation_id = _require_str(value, item_label)
        _validate_identifier(citation_id, item_label)
        previous = _require_ascending(
            _identity_sort_key(citation_id), previous, item_label, "citation_id"
        )
        if citation_id not in citation_ids:
            raise ContractSemanticError(
                f"{item_label} {citation_id!r} does not resolve to a citation this pack "
                "returned"
            )
        resolved.append(citation_id)
    return tuple(resolved)


def _validate_sections(
    sections: object, citation_ids: AbstractSet[str]
) -> tuple[int, set[str]]:
    """Raise unless `sections` is a valid, deterministically ordered section list, and
    return `(total token count, the set of citation ids the sections actually use)`.

    Every section is non-empty, cited, and positively counted. The last of those is worth
    stating on its own: a section whose `token_count` is zero while its `content` is a
    non-empty string is claiming that model-facing text costs nothing, which is either a
    tokenizer that was never run or an accounting error -- and either way it lets a pack
    carry content the budget never paid for. Since `content`'s own `minLength` already makes
    every present section non-empty, a positive token count is simply the same fact told
    consistently.
    """
    items = _require_sequence(sections, "sections")
    if len(items) > _CONTEXT_PACK_MAX_SECTIONS:
        raise ContractSemanticError(
            f"sections carries {len(items)} entries, exceeding the maximum of "
            f"{_CONTEXT_PACK_MAX_SECTIONS}"
        )
    tokens_total = 0
    used: set[str] = set()
    seen_ids: set[str] = set()
    previous: tuple[bytes, ...] | None = None
    for index, section in enumerate(items):
        label = f"sections[{index}]"
        _require_type(section, ContextPackSection, label)
        assert isinstance(section, ContextPackSection)
        section_id = _require_str(section.section_id, f"{label}.section_id")
        _validate_identifier(section_id, f"{label}.section_id")
        if section_id in seen_ids:
            raise ContractSemanticError(f"{label} duplicates section_id {section_id!r}")
        seen_ids.add(section_id)
        previous = _require_ascending(
            _identity_sort_key(section_id), previous, label, "section_id"
        )

        _validate_open_code(_require_str(section.kind, f"{label}.kind"), f"{label}.kind")
        if section.title is not None:
            _validate_bounded_text(
                section.title,
                f"{label}.title",
                minimum=1,
                maximum=_CONTEXT_PACK_MAX_SECTION_TITLE_LENGTH,
            )
        _validate_bounded_text(
            section.content,
            f"{label}.content",
            minimum=1,
            maximum=_CONTEXT_PACK_MAX_SECTION_CONTENT_LENGTH,
        )
        used.update(
            _validate_citation_id_list(
                section.citation_ids,
                f"{label}.citation_ids",
                citation_ids,
                minimum=1,
                maximum=_CONTEXT_PACK_MAX_SECTION_CITATION_IDS,
            )
        )
        token_count = _require_int(section.token_count, f"{label}.token_count")
        _validate_token_count(token_count, f"{label}.token_count")
        if token_count <= 0:
            raise ContractSemanticError(
                f"{label}.token_count is {token_count!r} but the section carries non-empty "
                "model-facing content; a section that costs nothing to send is content the "
                "budget never paid for"
            )
        tokens_total += token_count
    return tokens_total, used


# --- context_pack.build: reproducibility ---------------------------------------


def _validate_authorization_context(
    context: object,
    *,
    expected_workspace_id: str,
    expected_authority: GrantedAuthority,
    expected_scopes: AbstractSet[str],
    expected_purpose: str,
    expected_policy_versions: Mapping[str, str],
    expected_candidate_set_checksum: str,
) -> None:
    """Raise unless the recorded authority context is exactly the one the caller vouched for.

    Every comparison is exact, and every one is against a value the *caller* supplied rather
    than against something the artifact asserts about itself: a pack that could vouch for
    its own authority context would be a pack that could mint one. This is historical
    reproducibility context, never a live grant -- which is precisely why it has to be
    checkable, since an audit record nobody can verify records nothing.

    Roles, capabilities, and scopes are compared as membership sets while the *stored*
    arrays are required to be duplicate-free and in ascending order. The two requirements do
    different jobs and both are needed: order and uniqueness make the artifact deterministic
    (a set that could be written in any order would hash differently every build), while set
    comparison against the expectation means a caller does not have to pre-sort what it
    vouches for in order to be believed. Capabilities sort by the whole `(id, version)` pair,
    since a capability held at one version is not the same grant as the same id at another.

    Both stored arrays are bounded at their own schema `maxItems`
    (:data:`_CONTEXT_PACK_MAX_AUTHORITY_ROLES`,
    :data:`_CONTEXT_PACK_MAX_AUTHORITY_CAPABILITIES`) before anything is compared, for the
    reason every other ceiling in this module is restated rather than delegated: the
    tolerant decoder applies no schema cardinality, so an over-long stored authority context
    would otherwise pass here while failing strict JSON Schema. The bound is on the *stored*
    context, which is what the artifact carries and what a rebuild must reproduce; the
    caller's expectation is a set it vouches for, not a wire array, and the exact-equality
    comparison below already refuses any stored context that does not match it.

    `pre_ranking_authorization_enforced` must be literally `true`. It attests that ACL and
    sensitivity filtering ran over the candidate set *before* ranking and selection: a pack
    that filtered afterwards ranked material the caller was never entitled to see, and
    leaked its existence through what it chose to leave out.

    `authorized_candidate_set_checksum` is compared against `expected_candidate_set_checksum`,
    which the caller's entry point *recomputed* from an independently supplied manifest
    (:func:`compute_authorized_candidate_set_checksum`) rather than read out of this artifact.
    That distinction is the whole value of the field: a checksum copied from the pack and
    compared with itself agrees by construction, including for a pack whose ranking ran over
    material the caller was never entitled to see.
    """
    _require_type(context, ContextPackAuthorizationContext, "reproducibility.authorization_context")
    assert isinstance(context, ContextPackAuthorizationContext)
    label = "reproducibility.authorization_context"

    workspace_id = _require_str(context.workspace_id, f"{label}.workspace_id")
    _validate_workspace_id(workspace_id, f"{label}.workspace_id")
    _require_exact(workspace_id, expected_workspace_id, f"{label}.workspace_id")

    authority = context.authority
    _require_type(authority, GrantedAuthority, f"{label}.authority")
    assert isinstance(authority, GrantedAuthority)
    principal_id = _require_str(authority.principal_id, f"{label}.authority.principal_id")
    _validate_identifier(principal_id, f"{label}.authority.principal_id")
    _require_exact(
        principal_id,
        _require_str(expected_authority.principal_id, "expected_authority.principal_id"),
        f"{label}.authority.principal_id",
    )

    roles = _require_sequence(authority.roles, f"{label}.authority.roles")
    if len(roles) > _CONTEXT_PACK_MAX_AUTHORITY_ROLES:
        raise ContractSemanticError(
            f"{label}.authority.roles carries {len(roles)} entries, exceeding "
            f"`GrantedAuthority.roles`'s maximum of {_CONTEXT_PACK_MAX_AUTHORITY_ROLES}"
        )
    previous: tuple[bytes, ...] | None = None
    stored_roles: set[str] = set()
    for index, value in enumerate(roles):
        role_label = f"{label}.authority.roles[{index}]"
        role = _require_str(value, role_label)
        _validate_identifier(role, role_label)
        if role in stored_roles:
            raise ContractSemanticError(f"{role_label} duplicates role {role!r}")
        previous = _require_ascending(_identity_sort_key(role), previous, role_label, "role")
        stored_roles.add(role)
    expected_roles = {
        _require_str(role, "expected_authority.roles entry") for role in expected_authority.roles
    }
    if stored_roles != expected_roles:
        raise ContractSemanticError(
            f"{label}.authority.roles {sorted(stored_roles)!r} is not exactly the expected "
            f"role set {sorted(expected_roles)!r}"
        )

    capabilities = _require_sequence(authority.capabilities, f"{label}.authority.capabilities")
    if len(capabilities) > _CONTEXT_PACK_MAX_AUTHORITY_CAPABILITIES:
        raise ContractSemanticError(
            f"{label}.authority.capabilities carries {len(capabilities)} entries, exceeding "
            "`GrantedAuthority.capabilities`'s maximum of "
            f"{_CONTEXT_PACK_MAX_AUTHORITY_CAPABILITIES}"
        )
    previous = None
    stored_capabilities: set[tuple[str, str]] = set()
    stored_capability_ids: set[str] = set()
    for index, value in enumerate(capabilities):
        capability_label = f"{label}.authority.capabilities[{index}]"
        _require_type(value, CapabilityRef, capability_label)
        assert isinstance(value, CapabilityRef)
        capability_id = _validate_capability_id(value.id, f"{capability_label}.id")
        key = (
            capability_id,
            _validate_contract_version(value.version, f"{capability_label}.version"),
        )
        # One grant per capability id, whatever version it carries. A
        # `GrantedAuthority` on a response envelope is held to exactly this by
        # `validate_granted_authority`, and a type whose validity depended on
        # where it sat would let a pack attest an authority the envelope that
        # delivered it could never have stated -- the same document legal in one
        # position and illegal in the other.
        #
        # This subsumes the `(id, version)` duplicate that used to be rejected
        # here: a repeated pair repeats its id, so that check could no longer
        # fire and is gone rather than left reading as protection.
        if capability_id in stored_capability_ids:
            raise ContractSemanticError(
                f"{capability_label} repeats capability id {capability_id!r}; a granted "
                "authority names each capability once, at a single version"
            )
        previous = _require_ascending(
            _identity_sort_key(*key), previous, capability_label, "(id, version)"
        )
        stored_capability_ids.add(capability_id)
        stored_capabilities.add(key)
    expected_capabilities = {
        (
            _require_str(ref.id, "expected_authority.capabilities entry id"),
            _require_str(ref.version, "expected_authority.capabilities entry version"),
        )
        for ref in expected_authority.capabilities
    }
    if stored_capabilities != expected_capabilities:
        raise ContractSemanticError(
            f"{label}.authority.capabilities {sorted(stored_capabilities)!r} is not exactly "
            f"the expected capability set {sorted(expected_capabilities)!r}"
        )

    scopes = _require_sequence(context.scopes, f"{label}.scopes")
    if not (_CONTEXT_PACK_MIN_SCOPES <= len(scopes) <= _CONTEXT_PACK_MAX_SCOPES):
        raise ContractSemanticError(
            f"{label}.scopes names {len(scopes)} scope(s), outside the bounded range "
            f"[{_CONTEXT_PACK_MIN_SCOPES}, {_CONTEXT_PACK_MAX_SCOPES}]"
        )
    previous = None
    stored_scopes: set[str] = set()
    for index, value in enumerate(scopes):
        scope_label = f"{label}.scopes[{index}]"
        scope = _validate_scope(value, scope_label)
        if scope in stored_scopes:
            raise ContractSemanticError(f"{scope_label} duplicates scope {scope!r}")
        previous = _require_ascending(_identity_sort_key(scope), previous, scope_label, "scope")
        stored_scopes.add(scope)
    if stored_scopes != set(expected_scopes):
        raise ContractSemanticError(
            f"{label}.scopes {sorted(stored_scopes)!r} is not exactly the expected scope set "
            f"{sorted(expected_scopes)!r}"
        )

    purpose = _require_str(context.purpose, f"{label}.purpose")
    _validate_purpose(purpose, f"{label}.purpose")
    _require_exact(purpose, expected_purpose, f"{label}.purpose")

    policy_versions = _require_mapping(context.policy_versions, f"{label}.policy_versions")
    if not policy_versions:
        raise ContractSemanticError(
            f"{label}.policy_versions names no policy; a pack that states no policy version "
            "states nothing checkable about what filtered it"
        )
    stored_policies: dict[str, str] = {}
    for name, version in policy_versions.items():
        policy_name = _require_str(name, f"{label}.policy_versions key")
        _validate_open_code(policy_name, f"{label}.policy_versions key")
        version_label = f"{label}.policy_versions[{policy_name!r}]"
        policy_version = _require_str(version, version_label)
        _validate_opaque_token(policy_version, version_label)
        stored_policies[policy_name] = policy_version
    if stored_policies != dict(expected_policy_versions):
        raise ContractSemanticError(
            f"{label}.policy_versions {sorted(stored_policies.items())!r} is not exactly the "
            f"expected policy versions {sorted(dict(expected_policy_versions).items())!r}"
        )

    _require_type(
        context.pre_ranking_authorization_enforced,
        bool,
        f"{label}.pre_ranking_authorization_enforced",
    )
    if not context.pre_ranking_authorization_enforced:
        raise ContractSemanticError(
            f"{label}.pre_ranking_authorization_enforced must always be true: authorization "
            "applied only after ranking has already ranked material the caller was never "
            "entitled to see, and leaks its existence through what was left out"
        )
    candidate_set_checksum = _validate_context_pack_digest(
        context.authorized_candidate_set_checksum, f"{label}.authorized_candidate_set_checksum"
    )
    if candidate_set_checksum != expected_candidate_set_checksum:
        raise ContractSemanticError(
            f"{label}.authorized_candidate_set_checksum {candidate_set_checksum!r} is not the "
            "digest of the authorized candidate set this validation was given "
            f"({expected_candidate_set_checksum!r}); the pack states it ranked over one "
            "frontier and the caller's own manifest states another"
        )


def _validate_normalized_request(
    normalized: object,
    request: ContextPackBuildInput,
    budget: int,
    *,
    expected_normalized_query: str | None,
    expected_normalization_version: str | None,
) -> None:
    """Raise unless the recorded normalized request binds exactly to the validated request.

    The normalized request is the single normalized form of what was asked -- the original
    caller query stays on the result's own `query`, and nothing else restates it. Query
    normalization is server-owned and versioned, so this contract requires only that the
    normalized query is present, bounded, and non-empty, and that the normalization that
    produced it is named: inventing a normalization algorithm here would freeze one this
    layer has no business owning, while accepting an empty normalized query would leave a
    build with no request left to reproduce.

    A caller that *does* know what those two values should be may say so, and then they are
    bound exactly (`expected_normalized_query`, `expected_normalization_version`). That is
    the only way this layer can check them at all: absent an expectation, a producer's
    assertion about its own normalization is unfalsifiable here, and the shape check is
    honestly all that is left.

    Everything a normalization *cannot* legitimately change is bound exactly: the mode, the
    budget, both selection filters (present exactly when the request carried one, absent
    exactly when it did not), and the resolved view -- which is always
    :data:`CONTEXT_PACK_NORMALIZED_REQUEST_VIEW`, since the input carries no view selector
    at all and a pack claiming to have resolved a wider one is claiming to have read
    something it was never asked for.
    """
    _require_type(normalized, ContextPackNormalizedRequest, "reproducibility.normalized_request")
    assert isinstance(normalized, ContextPackNormalizedRequest)
    label = "reproducibility.normalized_request"

    normalized_query = _validate_bounded_text(
        normalized.normalized_query,
        f"{label}.normalized_query",
        minimum=1,
        maximum=_QUERY_MAX_LENGTH,
    )
    if expected_normalized_query is not None:
        _require_exact(normalized_query, expected_normalized_query, f"{label}.normalized_query")
    mode = _require_str(normalized.mode, f"{label}.mode")
    _validate_context_pack_mode(mode, f"{label}.mode")
    _require_exact(mode, request.mode, f"{label}.mode")

    view = _require_str(normalized.view, f"{label}.view")
    _validate_view_selector(view, f"{label}.view")
    if view != CONTEXT_PACK_NORMALIZED_REQUEST_VIEW:
        raise ContractSemanticError(
            f"{label}.view must be exactly {CONTEXT_PACK_NORMALIZED_REQUEST_VIEW!r}, got "
            f"{view!r}; `context_pack.build` carries no view selector, so a resolved view "
            "other than the current canonical one is a read nobody asked for"
        )

    _require_exact(
        _validate_token_budget(normalized.token_budget, f"{label}.token_budget"),
        budget,
        f"{label}.token_budget",
    )
    normalization_version = _require_str(
        normalized.normalization_version, f"{label}.normalization_version"
    )
    _validate_identifier(normalization_version, f"{label}.normalization_version")
    if expected_normalization_version is not None:
        _require_exact(
            normalization_version,
            expected_normalization_version,
            f"{label}.normalization_version",
        )

    if normalized.domain_scope is not None:
        validate_record_domain_scope(_require_str(normalized.domain_scope, f"{label}.domain_scope"))
    _require_exact(normalized.domain_scope, request.domain_scope, f"{label}.domain_scope")
    if normalized.record_type is not None:
        _validate_governed_record_type(normalized.record_type, f"{label}.record_type")
    _require_exact(normalized.record_type, request.record_type, f"{label}.record_type")


def _validate_reproducibility(
    reproducibility: object,
    *,
    request: ContextPackBuildInput,
    budget: int,
    selected_evidence: tuple[tuple[str, str], ...],
    selected_records: tuple[tuple[str, str], ...],
    expected_workspace_id: str,
    expected_authority: GrantedAuthority,
    expected_scopes: AbstractSet[str],
    expected_purpose: str,
    expected_policy_versions: Mapping[str, str],
    expected_candidate_set_checksum: str,
    canonical_resolution_time: str,
    resolution_instant: datetime,
    response_freshness: object,
    expected: _ExpectedReproducibility,
) -> None:
    """Raise unless the reproducibility record is complete, exact, and internally coherent.

    The claim this record makes is strong -- with all of it unchanged, a rebuild reproduces
    the identical pack -- so every part of it is checked against something outside itself:

    - `pack_format_version` is exactly :data:`CONTEXT_PACK_FORMAT_VERSION` and
      `artifact_canonicalization` exactly
      :data:`CONTEXT_PACK_ARTIFACT_CANONICALIZATION`. Both are frozen because the checksum
      rule is defined against exactly one artifact format and one canonical form;
    - `evidence_versions` and `record_versions` are *exactly* the identities this pack
      selected -- same members, same order, no addition, omission, or duplicate. A superset
      would let a pack claim to have consulted material it never returned; a subset would
      let it return material it never declared. `record_versions` is the *sorted union* of
      all three governed partitions rather than their concatenation: the union is what the
      pack consulted, and which partition a version was returned in is already stated by
      the partition it sits in, so ordering by identity keeps one fact in one place;
    - `freshness` satisfies the shared strict rule
      (:func:`validate_projection_freshness`) and equals the response envelope's own
      freshness exactly, so a pack cannot state one staleness to the envelope and another to
      the reader;
    - every version, tokenizer, and model identifier is shape-valid, and is additionally
      bound to an exact caller-supplied value wherever `expected` carries one.
      `summarizer_version` is required rather than optional, spelled
      :data:`CONTEXT_PACK_SUMMARIZER_DISABLED` when no summarizer ran, so "summarized
      nothing" and "never recorded it" stay distinguishable; `model_versions` is likewise
      required and may be empty, which is how a build that used no model says so
      explicitly -- and `expected.model_versions` of `{}` is how a caller says it expects
      exactly that, distinctly from supplying no expectation at all;
    - the three instants -- `canonical_resolution_time`, `generated_at`, and
      `freshness.as_of` -- are all the *same string*, equal to the resolution time the
      caller passed in. Equality of instants would be the weaker rule and the wrong one
      here: this artifact is content-addressed, so two spellings of the same moment are two
      different digests, and pinning the spelling is what makes the identity a function of
      the build rather than of how a server happened to format a timestamp. `generated_at`
      equalling `canonical_resolution_time` is the same choice restated as a definition: a
      deterministic build is logically complete at the instant it resolved at, and a
      wall-clock generation time would make two identical builds hash differently.
    """
    _require_type(reproducibility, ContextPackReproducibility, "reproducibility")
    assert isinstance(reproducibility, ContextPackReproducibility)

    _require_exact(
        _validate_contract_version(
            reproducibility.pack_format_version, "reproducibility.pack_format_version"
        ),
        CONTEXT_PACK_FORMAT_VERSION,
        "reproducibility.pack_format_version",
    )
    canonicalization = _require_str(
        reproducibility.artifact_canonicalization, "reproducibility.artifact_canonicalization"
    )
    _validate_open_code(canonicalization, "reproducibility.artifact_canonicalization")
    _require_exact(
        canonicalization,
        CONTEXT_PACK_ARTIFACT_CANONICALIZATION,
        "reproducibility.artifact_canonicalization",
    )

    for field_name in _CONTEXT_PACK_EXPECTED_IDENTIFIER_FIELDS:
        value_label = f"reproducibility.{field_name}"
        stated = _require_str(getattr(reproducibility, field_name), value_label)
        _validate_identifier(stated, value_label)
        expectation: str | None = getattr(expected, field_name)
        if expectation is not None:
            _require_exact(stated, expectation, value_label)

    model_versions = _require_mapping(reproducibility.model_versions, "reproducibility.model_versions")
    stated_model_versions: dict[str, str] = {}
    for name, version in model_versions.items():
        role = _require_str(name, "reproducibility.model_versions key")
        _validate_open_code(role, "reproducibility.model_versions key")
        version_label = f"reproducibility.model_versions[{role!r}]"
        model_version = _require_str(version, version_label)
        _validate_identifier(model_version, version_label)
        stated_model_versions[role] = model_version
    if expected.model_versions is not None and stated_model_versions != dict(
        expected.model_versions
    ):
        raise ContractSemanticError(
            f"reproducibility.model_versions {sorted(stated_model_versions.items())!r} is not "
            "exactly the expected model versions "
            f"{sorted(dict(expected.model_versions).items())!r}"
        )

    _validate_normalized_request(
        reproducibility.normalized_request,
        request,
        budget,
        expected_normalized_query=expected.normalized_query,
        expected_normalization_version=expected.normalization_version,
    )
    _validate_authorization_context(
        reproducibility.authorization_context,
        expected_workspace_id=expected_workspace_id,
        expected_authority=expected_authority,
        expected_scopes=expected_scopes,
        expected_purpose=expected_purpose,
        expected_policy_versions=expected_policy_versions,
        expected_candidate_set_checksum=expected_candidate_set_checksum,
    )

    evidence_versions = _require_sequence(
        reproducibility.evidence_versions, "reproducibility.evidence_versions"
    )
    if len(evidence_versions) > _CONTEXT_PACK_MAX_SELECTED_ITEMS:
        raise ContractSemanticError(
            f"reproducibility.evidence_versions carries {len(evidence_versions)} entries, "
            f"exceeding the maximum of {_CONTEXT_PACK_MAX_SELECTED_ITEMS}"
        )
    stated_evidence: list[tuple[str, str]] = []
    for index, reference in enumerate(evidence_versions):
        label = f"reproducibility.evidence_versions[{index}]"
        _require_type(reference, ContextPackEvidenceReference, label)
        assert isinstance(reference, ContextPackEvidenceReference)
        stated_evidence.append(
            (
                _validate_evidence_id(reference.evidence_id, f"{label}.evidence_id"),
                _validate_evidence_checksum(
                    reference.content_checksum, f"{label}.content_checksum"
                ),
            )
        )
    if tuple(stated_evidence) != selected_evidence:
        raise ContractSemanticError(
            f"reproducibility.evidence_versions {stated_evidence!r} is not exactly the "
            f"evidence this pack selected, in ascending (evidence_id, content_checksum) "
            f"order {list(selected_evidence)!r}"
        )

    record_versions = _require_sequence(
        reproducibility.record_versions, "reproducibility.record_versions"
    )
    if len(record_versions) > _CONTEXT_PACK_MAX_RECORD_VERSIONS:
        raise ContractSemanticError(
            f"reproducibility.record_versions carries {len(record_versions)} entries, "
            f"exceeding the maximum of {_CONTEXT_PACK_MAX_RECORD_VERSIONS}"
        )
    stated_records = tuple(
        _validate_record_version_reference(reference, f"reproducibility.record_versions[{index}]")
        for index, reference in enumerate(record_versions)
    )
    if stated_records != selected_records:
        raise ContractSemanticError(
            f"reproducibility.record_versions {list(stated_records)!r} is not exactly the "
            f"union of the governed records this pack selected, in ascending "
            f"(record_id, version) order {list(selected_records)!r}"
        )

    _require_type(response_freshness, ProjectionFreshness, "response_freshness")
    assert isinstance(response_freshness, ProjectionFreshness)
    validate_projection_freshness(response_freshness, "response_freshness")
    validate_projection_freshness(reproducibility.freshness, "reproducibility.freshness")
    if reproducibility.freshness != response_freshness:
        raise ContractSemanticError(
            "reproducibility.freshness is not exactly the response's own freshness; a pack "
            "may not state one staleness to the envelope and another to the reader"
        )

    resolution = _require_str(
        reproducibility.canonical_resolution_time, "reproducibility.canonical_resolution_time"
    )
    _parse_timestamp(resolution, "reproducibility.canonical_resolution_time")
    _require_exact(
        resolution, canonical_resolution_time, "reproducibility.canonical_resolution_time"
    )
    generated_at = _require_str(reproducibility.generated_at, "reproducibility.generated_at")
    _parse_timestamp(generated_at, "reproducibility.generated_at")
    if generated_at != resolution:
        raise ContractSemanticError(
            f"reproducibility.generated_at {generated_at!r} is not "
            f"reproducibility.canonical_resolution_time {resolution!r}; a deterministic "
            "build is logically complete at the instant it resolved at, and a wall-clock "
            "generation time would give two otherwise identical builds two identities"
        )
    freshness_as_of = _require_str(
        reproducibility.freshness.as_of, "reproducibility.freshness.as_of"
    )
    if freshness_as_of != resolution:
        raise ContractSemanticError(
            f"reproducibility.freshness.as_of {freshness_as_of!r} is not "
            f"reproducibility.canonical_resolution_time {resolution!r}; the projection this "
            "pack was served from must be stated as of the instant it resolved at"
        )
    # Parsed once here so a caller reading this function alone can see that the pinned
    # spelling really is the instant every selected item was judged against.
    if _parse_timestamp(resolution, "reproducibility.canonical_resolution_time") != resolution_instant:
        raise ContractSemanticError(
            f"reproducibility.canonical_resolution_time {resolution!r} does not name the "
            f"canonical_resolution_time {canonical_resolution_time!r} this result was "
            "validated against"
        )


# --- context_pack.build: whole-result validation -------------------------------


def _validate_expected_authority(authority: object) -> GrantedAuthority:
    _require_type(authority, GrantedAuthority, "expected_authority")
    assert isinstance(authority, GrantedAuthority)
    _validate_identifier(
        _require_str(authority.principal_id, "expected_authority.principal_id"),
        "expected_authority.principal_id",
    )
    for index, role in enumerate(_require_sequence(authority.roles, "expected_authority.roles")):
        label = f"expected_authority.roles[{index}]"
        _validate_identifier(_require_str(role, label), label)
    for index, capability in enumerate(
        _require_sequence(authority.capabilities, "expected_authority.capabilities")
    ):
        label = f"expected_authority.capabilities[{index}]"
        _require_type(capability, CapabilityRef, label)
        assert isinstance(capability, CapabilityRef)
        _validate_capability_id(capability.id, f"{label}.id")
        _validate_contract_version(capability.version, f"{label}.version")
    return authority


def _validate_expected_scopes(scopes: object) -> frozenset[str]:
    values = (
        scopes
        if isinstance(scopes, AbstractSet)
        else _require_sequence(scopes, "expected_scopes")
    )
    resolved: set[str] = set()
    for index, value in enumerate(values):
        resolved.add(_validate_scope(value, f"expected_scopes[{index}]"))
    if not resolved:
        raise ContractSemanticError(
            "expected_scopes is empty; a pack always records at least one scope in force, so "
            "an empty expectation could never be satisfied"
        )
    return frozenset(resolved)


class _ExpectedReproducibility(NamedTuple):
    """The producer assertions a caller has chosen to bind, already shape-validated.

    Every field is a *caller* expectation, so `None` means "no expectation supplied" and
    the corresponding assertion is shape-checked but not bound -- which is what keeps
    :func:`validate_context_pack_build_result`'s existing signature working unchanged.

    The authorized-candidate-set checksum is deliberately *not* here. It is not a producer
    assertion a caller may optionally bind: it is recomputed from a mandatory manifest and
    always compared, so treating it as one more optional expectation would put the one field
    that must never be taken on trust in the collection reserved for fields that are.

    This exists only to keep the private helpers' signatures readable: the public entry
    points take these as eleven explicit keyword-only arguments, because a caller writing
    `expected_tokenizer_id="tok-1"` gets a name and a type from the signature, while a
    configuration object would hand them a mapping key to spell correctly and no type at
    all. It is threaded, not exposed.
    """

    normalized_query: str | None = None
    normalization_version: str | None = None
    builder_version: str | None = None
    retrieval_version: str | None = None
    ranking_version: str | None = None
    reranking_version: str | None = None
    selection_version: str | None = None
    tokenizer_id: str | None = None
    tokenizer_version: str | None = None
    summarizer_version: str | None = None
    model_versions: Mapping[str, str] | None = None


#: The `reproducibility.<name>` assertions that are plain `Identifier`s, and therefore
#: shape-checked and bound the same way. Ordered as the record declares them.
_CONTEXT_PACK_EXPECTED_IDENTIFIER_FIELDS: Final[tuple[str, ...]] = (
    "builder_version",
    "retrieval_version",
    "ranking_version",
    "reranking_version",
    "selection_version",
    "tokenizer_id",
    "tokenizer_version",
    "summarizer_version",
)


def _validate_expected_reproducibility(
    *,
    normalized_query: object,
    normalization_version: object,
    builder_version: object,
    retrieval_version: object,
    ranking_version: object,
    reranking_version: object,
    selection_version: object,
    tokenizer_id: object,
    tokenizer_version: object,
    summarizer_version: object,
    model_versions: object,
) -> _ExpectedReproducibility:
    """Shape-check each supplied expectation and return them together.

    An expectation is validated to the same shape the artifact's own field is held to,
    before any comparison: a caller who vouches for a malformed value has made a mistake
    about what it is vouching for, and saying so directly is more useful than an inequality
    against a value that could never have appeared.
    """
    def identifier(value: object, name: str) -> str | None:
        if value is None:
            return None
        label = f"expected_{name}"
        text = _require_str(value, label)
        _validate_identifier(text, label)
        return text

    expected_normalized_query: str | None = None
    if normalized_query is not None:
        expected_normalized_query = _validate_bounded_text(
            normalized_query,
            "expected_normalized_query",
            minimum=1,
            maximum=_QUERY_MAX_LENGTH,
        )

    expected_model_versions: Mapping[str, str] | None = None
    if model_versions is not None:
        mapping = _require_mapping(model_versions, "expected_model_versions")
        collected: dict[str, str] = {}
        for role, version in mapping.items():
            role_name = _require_str(role, "expected_model_versions key")
            _validate_open_code(role_name, "expected_model_versions key")
            version_label = f"expected_model_versions[{role_name!r}]"
            model_version = _require_str(version, version_label)
            _validate_identifier(model_version, version_label)
            collected[role_name] = model_version
        expected_model_versions = collected

    return _ExpectedReproducibility(
        normalized_query=expected_normalized_query,
        normalization_version=identifier(normalization_version, "normalization_version"),
        builder_version=identifier(builder_version, "builder_version"),
        retrieval_version=identifier(retrieval_version, "retrieval_version"),
        ranking_version=identifier(ranking_version, "ranking_version"),
        reranking_version=identifier(reranking_version, "reranking_version"),
        selection_version=identifier(selection_version, "selection_version"),
        tokenizer_id=identifier(tokenizer_id, "tokenizer_id"),
        tokenizer_version=identifier(tokenizer_version, "tokenizer_version"),
        summarizer_version=identifier(summarizer_version, "summarizer_version"),
        model_versions=expected_model_versions,
    )


def _validate_expected_authorized_candidate_set(
    manifest: object, *, expected_workspace_id: str
) -> _AuthorizedCandidateSet:
    """Validate the caller's candidate-set manifest and reduce it to digest plus membership.

    The workspace is bound here rather than left to the digest comparison alone. Two
    workspaces that authorized identical identities already produce different digests, since
    the workspace is in the preimage -- but a mismatch caught as "this manifest is for another
    workspace" says what went wrong, while the same mismatch caught as two unequal hexadecimal
    strings says only that something did.
    """
    candidate_set = _summarize_authorized_candidate_set(manifest)
    assert isinstance(manifest, ContextPackAuthorizedCandidateSetManifest)
    stated_workspace = _require_str(
        manifest.workspace_id, "expected_authorized_candidate_set.workspace_id"
    )
    if stated_workspace != expected_workspace_id:
        raise ContractSemanticError(
            f"expected_authorized_candidate_set.workspace_id {stated_workspace!r} is not the "
            f"validated workspace {expected_workspace_id!r}; a frontier authorized in another "
            "workspace vouches for nothing here"
        )
    return candidate_set


def _require_authorized_candidate(
    members: AbstractSet[tuple[str, str, str]],
    partition: str,
    identity: tuple[str, str],
    label: str,
) -> None:
    """Raise unless a selected item is a member of the authorized frontier.

    Under its *exact* partition: the same governed version authorized as history and returned
    as current is not the same claim, so partition is part of the membership key rather than
    something matched loosely. This is the rule that makes "nothing enters a pack after the
    frontier is frozen" checkable instead of merely asserted -- an item that is not a member
    was introduced after authorization ran, whatever else about it validates.
    """
    if (partition, identity[0], identity[1]) not in members:
        raise ContractSemanticError(
            f"{label} selects {identity!r}, which the authorized candidate set does not "
            f"contain in partition {partition!r}; the frontier is frozen before ranking and "
            "selection, so an item that is not on it was introduced after authorization ran"
        )


def _validate_expected_policy_versions(policy_versions: object) -> Mapping[str, str]:
    mapping = _require_mapping(policy_versions, "expected_policy_versions")
    resolved: dict[str, str] = {}
    for name, version in mapping.items():
        key = _require_str(name, "expected_policy_versions key")
        _validate_open_code(key, "expected_policy_versions key")
        value_label = f"expected_policy_versions[{key!r}]"
        value = _require_str(version, value_label)
        _validate_opaque_token(value, value_label)
        resolved[key] = value
    if not resolved:
        raise ContractSemanticError(
            "expected_policy_versions is empty; a pack always records at least one policy "
            "version, so an empty expectation could never be satisfied"
        )
    return resolved


def _validate_statements(
    result: ContextPackBuildResult, citation_ids: AbstractSet[str]
) -> None:
    """Raise unless the conflicts, uncertainties, and omissions a pack states are
    well-formed, resolvable, and deterministically ordered.

    Conflicts and uncertainties are anchored in citation identifiers rather than record
    references, which is the change that lets them cover evidence and governed records alike
    through the one reference system the pack already publishes -- instead of a second,
    competing one that could name something the pack never cited.

    Ordering keys are the whole statement, not just its first field: conflicts by
    `(citation ids, description)` and uncertainties by `(citation ids, description)`, so two
    statements about the same citations still have one deterministic order; omissions by
    `(code, path, message)` with absent optionals ordering as empty, so an omission carrying
    no path sorts before one that does rather than being incomparable.
    """
    conflicts = _require_sequence(result.conflicts, "conflicts")
    if len(conflicts) > _CONTEXT_PACK_MAX_STATEMENTS:
        raise ContractSemanticError(
            f"conflicts carries {len(conflicts)} entries, exceeding the maximum of "
            f"{_CONTEXT_PACK_MAX_STATEMENTS}"
        )
    previous: tuple[bytes, ...] | None = None
    for index, conflict in enumerate(conflicts):
        label = f"conflicts[{index}]"
        _require_type(conflict, ContextPackConflict, label)
        assert isinstance(conflict, ContextPackConflict)
        description = _validate_bounded_text(
            conflict.description, f"{label}.description", minimum=1, maximum=_DESCRIPTION_MAX_LENGTH
        )
        ids = _validate_citation_id_list(
            conflict.conflicting_citation_ids,
            f"{label}.conflicting_citation_ids",
            citation_ids,
            minimum=_CONTEXT_PACK_MIN_CONFLICT_CITATION_IDS,
            maximum=_CONTEXT_PACK_MAX_CONFLICT_CITATION_IDS,
        )
        previous = _require_ascending(
            _identity_sort_key(*ids, description),
            previous,
            label,
            "(conflicting_citation_ids, description)",
        )

    uncertainties = _require_sequence(result.uncertainties, "uncertainties")
    if len(uncertainties) > _CONTEXT_PACK_MAX_STATEMENTS:
        raise ContractSemanticError(
            f"uncertainties carries {len(uncertainties)} entries, exceeding the maximum of "
            f"{_CONTEXT_PACK_MAX_STATEMENTS}"
        )
    previous = None
    for index, uncertainty in enumerate(uncertainties):
        label = f"uncertainties[{index}]"
        _require_type(uncertainty, ContextPackUncertainty, label)
        assert isinstance(uncertainty, ContextPackUncertainty)
        description = _validate_bounded_text(
            uncertainty.description,
            f"{label}.description",
            minimum=1,
            maximum=_DESCRIPTION_MAX_LENGTH,
        )
        ids = _validate_citation_id_list(
            uncertainty.related_citation_ids,
            f"{label}.related_citation_ids",
            citation_ids,
            minimum=_CONTEXT_PACK_MIN_UNCERTAINTY_CITATION_IDS,
            maximum=_CONTEXT_PACK_MAX_UNCERTAINTY_CITATION_IDS,
        )
        previous = _require_ascending(
            _identity_sort_key(*ids, description),
            previous,
            label,
            "(related_citation_ids, description)",
        )

    omissions = _require_sequence(result.omissions, "omissions")
    if len(omissions) > _CONTEXT_PACK_MAX_STATEMENTS:
        raise ContractSemanticError(
            f"omissions carries {len(omissions)} entries, exceeding the maximum of "
            f"{_CONTEXT_PACK_MAX_STATEMENTS}"
        )
    previous = None
    for index, omission in enumerate(omissions):
        label = f"omissions[{index}]"
        _require_type(omission, Omission, label)
        assert isinstance(omission, Omission)
        code = _require_str(omission.code, f"{label}.code")
        _validate_open_code(code, f"{label}.code")
        path = ""
        if omission.path is not None:
            path = _validate_bounded_text(
                omission.path,
                f"{label}.path",
                minimum=0,
                maximum=_CONTEXT_PACK_MAX_OMISSION_PATH_LENGTH,
            )
        message = ""
        if omission.message is not None:
            message = _validate_bounded_text(
                omission.message,
                f"{label}.message",
                minimum=0,
                maximum=_CONTEXT_PACK_MAX_OMISSION_MESSAGE_LENGTH,
            )
        previous = _require_ascending(
            _identity_sort_key(code, path, message), previous, label, "(code, path, message)"
        )


def validate_context_pack_build_result(
    result: object,
    *,
    request: object,
    expected_workspace_id: object,
    expected_authority: object,
    expected_scopes: object,
    expected_purpose: object,
    expected_policy_versions: object,
    expected_authorized_candidate_set: object,
    canonical_resolution_time: object,
    response_freshness: object,
    expected_normalized_query: object = None,
    expected_normalization_version: object = None,
    expected_builder_version: object = None,
    expected_retrieval_version: object = None,
    expected_ranking_version: object = None,
    expected_reranking_version: object = None,
    expected_selection_version: object = None,
    expected_tokenizer_id: object = None,
    expected_tokenizer_version: object = None,
    expected_summarizer_version: object = None,
    expected_model_versions: object = None,
) -> None:
    """Raise unless `result` is a complete, internally coherent, workspace-scoped,
    content-addressed Context Pack that never grants authority through citation alone.

    The first nine arguments are keyword-only and mandatory, and none of them is optional
    in the sense of "check less when absent". A pack is only checkable against the thing it
    claims to be a pack *of*: the request it answered, the workspace and authority it was
    built under, the authorized frontier it ranked over, the instant it resolved at, and the
    freshness the response states. A validator handed fewer of those could not tell a pack
    that honoured its filters from one that ignored them, or an authority context that was
    recorded from one that was invented -- so the weaker call is not offered.

    **The authorized candidate set is mandatory, and it is a manifest rather than a digest.**
    `expected_authorized_candidate_set` is a `ContextPackAuthorizedCandidateSetManifest`
    naming the complete authorized frontier this build was entitled to rank over, supplied
    out of band. Three things follow from it, and all three are enforced:

    - its workspace must be `expected_workspace_id`, so a frontier authorized elsewhere
      cannot vouch for this pack;
    - its digest, *recomputed* here (:func:`compute_authorized_candidate_set_checksum`), must
      equal `reproducibility.authorization_context.authorized_candidate_set_checksum`;
    - every selected item, under its own exact partition, must be a member of it. Nothing may
      enter a pack after the frontier is frozen, so a selected identity that is not in the
      manifest was introduced after authorization ran.

    Taking a bare digest instead would not be verification: a checksum copied out of the pack
    and compared with itself agrees by construction, whatever the pack actually ranked over.
    The manifest is in-process, identity-only trusted input -- never a response field, never
    logged -- which is exactly what lets it be the independent statement the artifact cannot
    make about itself.

    **Producer assertions, and how to bind them.** The `expected_*` arguments after those
    are optional and default to `None`, and they exist because of a real limit: a
    reproducibility record's normalized query, normalization version,
    builder/retrieval/ranking/reranking/selection versions, tokenizer id and
    version, summarizer version, and model versions are all *the producer's own statements
    about the producer*. Nothing inside the artifact can contradict them, so with no
    expectation supplied this layer can only check their shape -- which is exactly what it
    did, and exactly what a reviewer should not mistake for verification. Supply an
    expectation and the corresponding value is bound by exact equality, raising
    `ContractSemanticError` on any mismatch; supply none and the shape check is all that
    runs. The authorized-candidate checksum is deliberately not in that list any more: it is
    the one field that must never be taken on trust, so it is always checked.

    `expected_model_versions` takes a mapping and needs no sentinel to express absence:
    `reproducibility.model_versions` is required and may be empty, so `{}` is a caller
    expecting a build that used no model, and `None` is a caller expecting nothing.

    What is enforced, in the order it is checked:

    - **Mode and request binding.** `mode` is the one recognized value and equals the
      request's; `query` is exactly the request's original query. There is no
      `normalized_intent` on the result any more: the single normalized form lives on
      `reproducibility.normalized_request.normalized_query`, versioned by the normalization
      that produced it, so the two can never disagree by existing in two places.
    - **No authority through possession.** `fresh_authorization_required` is literally
      `true`, and `pack_id` is a content digest -- a value anyone can recompute, which is
      exactly why holding it grants nothing.
    - **Selected partitions.** Each of `evidence`, `records`, `history`, and
      `context_models` is validated by the shared per-item rules
      (:func:`_validate_selected_evidence`, :func:`_validate_selected_record`), is
      deterministically ordered by its identity key, is globally identity-unique across
      the three governed partitions -- a version returned as both current and historical
      would be two contradictory claims about the same instant -- and is a member of the
      supplied authorized candidate set under its own exact partition.
    - **Citations.** Deterministically ordered by `citation_id` and unique within
      `citations`; each resolves to exactly a selected evidence `(evidence_id, checksum)` or
      a selected governed-record `(record_id, version)`; no two carry the same
      `(target, content_pointer, source_span)`, while the same target at two genuinely
      different locations stays two citations.
    - **Coverage, both ways.** Every selected item is cited at least once -- otherwise a
      pack could carry content nothing points at -- and every citation is used by at least
      one section, otherwise it points at nothing the caller was given.
    - **Sections.** Deterministically ordered by `section_id`, non-empty, positively
      counted, and every referenced citation id resolves.
    - **Statements.** Conflicts, uncertainties, and omissions are well-formed,
      citation-resolvable, and deterministically ordered (:func:`_validate_statements`).
    - **Token accounting.** The budget equals the request's and the normalized request's;
      `tokens_used` is exactly the sum of the section token counts and never exceeds the
      budget. Section counts cover the exact model-facing `content` string under the
      declared tokenizer id and version, and nothing else.
    - **Reproducibility** (:func:`_validate_reproducibility`), including exact version-set
      equality, strict shared freshness, and the pinned resolution/generation instants.
    - **Content addressing.** `pack_id` and `reproducibility.artifact_checksum` are both the
      RFC 8785 SHA-256 digest of this result with exactly those two members removed.

    **Boundary.** This entry point takes a *trusted, strict* DTO and re-encodes it to
    compute the digest, so it verifies that this value's content matches its stated
    identity. It cannot verify what a wire document contained, because the parser that
    produced the DTO has already dropped any unknown additive field and resolved any
    duplicated member name. An integrity check at a trust boundary must therefore start from
    the raw bytes: use :func:`validate_context_pack_build_result_document`.

    A direct entry point: every argument is type-guarded, so a hand-built DTO or a wrongly
    typed expectation raises `ContractSemanticError`, never a raw
    `TypeError`/`AttributeError`/`KeyError`.
    """
    _require_type(result, ContextPackBuildResult, "result")
    assert isinstance(result, ContextPackBuildResult)
    _require_type(request, ContextPackBuildInput, "request")
    assert isinstance(request, ContextPackBuildInput)
    validate_context_pack_build_input(request)

    workspace_id = _require_str(expected_workspace_id, "expected_workspace_id")
    _validate_workspace_id(workspace_id, "expected_workspace_id")
    authority = _validate_expected_authority(expected_authority)
    scopes = _validate_expected_scopes(expected_scopes)
    purpose = _require_str(expected_purpose, "expected_purpose")
    _validate_purpose(purpose, "expected_purpose")
    policy_versions = _validate_expected_policy_versions(expected_policy_versions)
    candidate_set = _validate_expected_authorized_candidate_set(
        expected_authorized_candidate_set, expected_workspace_id=workspace_id
    )
    resolution_time = _require_str(canonical_resolution_time, "canonical_resolution_time")
    resolution_instant = _parse_timestamp(resolution_time, "canonical_resolution_time")
    expected_reproducibility = _validate_expected_reproducibility(
        normalized_query=expected_normalized_query,
        normalization_version=expected_normalization_version,
        builder_version=expected_builder_version,
        retrieval_version=expected_retrieval_version,
        ranking_version=expected_ranking_version,
        reranking_version=expected_reranking_version,
        selection_version=expected_selection_version,
        tokenizer_id=expected_tokenizer_id,
        tokenizer_version=expected_tokenizer_version,
        summarizer_version=expected_summarizer_version,
        model_versions=expected_model_versions,
    )

    mode = _require_str(result.mode, "mode")
    _validate_context_pack_mode(mode)
    _require_exact(mode, request.mode, "mode")
    _require_exact(_require_str(result.query, "query"), request.query, "query")
    _validate_context_pack_digest(result.pack_id, "pack_id")

    _require_type(result.fresh_authorization_required, bool, "fresh_authorization_required")
    if not result.fresh_authorization_required:
        raise ContractSemanticError(
            "fresh_authorization_required must always be true: possessing this pack, or its "
            "pack_id, must never be treated as sufficient authorization to follow a citation"
        )

    selected_targets: set[tuple[str, str, str]] = set()

    evidence_items = _require_sequence(result.evidence, "evidence")
    if len(evidence_items) > _CONTEXT_PACK_MAX_SELECTED_ITEMS:
        raise ContractSemanticError(
            f"evidence carries {len(evidence_items)} entries, exceeding the maximum of "
            f"{_CONTEXT_PACK_MAX_SELECTED_ITEMS}"
        )
    selected_evidence: list[tuple[str, str]] = []
    previous: tuple[bytes, ...] | None = None
    for index, artifact in enumerate(evidence_items):
        label = f"evidence[{index}]"
        key = _validate_selected_evidence(
            artifact,
            label,
            expected_workspace_id=workspace_id,
            resolution_instant=resolution_instant,
            resolution_time=resolution_time,
        )
        _require_authorized_candidate(
            candidate_set.members,
            CONTEXT_PACK_CANDIDATE_PARTITION_EVIDENCE,
            key,
            label,
        )
        target = ("evidence", key[0], key[1])
        if target in selected_targets:
            raise ContractSemanticError(f"{label} duplicates evidence identity {key!r}")
        previous = _require_ascending(
            _identity_sort_key(*key), previous, label, "(evidence_id, content_checksum)"
        )
        selected_targets.add(target)
        selected_evidence.append(key)

    selected_records: list[tuple[str, str]] = []
    for partition, records, layer, historical in (
        ("records", result.records, GOVERNANCE_LAYER_GOVERNED, False),
        ("history", result.history, GOVERNANCE_LAYER_GOVERNED, True),
        ("context_models", result.context_models, GOVERNANCE_LAYER_CONTEXT_MODEL, False),
    ):
        items = _require_sequence(records, partition)
        if len(items) > _CONTEXT_PACK_MAX_SELECTED_ITEMS:
            raise ContractSemanticError(
                f"{partition} carries {len(items)} entries, exceeding the maximum of "
                f"{_CONTEXT_PACK_MAX_SELECTED_ITEMS}"
            )
        previous = None
        for index, record in enumerate(items):
            label = f"{partition}[{index}]"
            key = _validate_selected_record(
                record,
                label,
                expected_workspace_id=workspace_id,
                resolution_instant=resolution_instant,
                resolution_time=resolution_time,
                request=request,
                layer=layer,
                historical=historical,
                partition=partition,
            )
            _require_authorized_candidate(candidate_set.members, partition, key, label)
            target = ("record", key[0], key[1])
            if target in selected_targets:
                raise ContractSemanticError(
                    f"{label} duplicates governed-record identity {key!r}, which another "
                    "selected partition already returned; one version cannot be two "
                    "contradictory claims about the same instant"
                )
            previous = _require_ascending(
                _identity_sort_key(*key), previous, label, "(record_id, version)"
            )
            selected_targets.add(target)
            selected_records.append(key)

    citations = _require_sequence(result.citations, "citations")
    if len(citations) > _CONTEXT_PACK_MAX_CITATIONS:
        raise ContractSemanticError(
            f"citations carries {len(citations)} entries, exceeding the maximum of "
            f"{_CONTEXT_PACK_MAX_CITATIONS}"
        )
    citation_ids: set[str] = set()
    cited_targets: set[tuple[str, str, str]] = set()
    seen_locators: set[tuple[tuple[str, str, str], str | None, SourceSpan | None]] = set()
    previous = None
    for index, citation in enumerate(citations):
        label = f"citations[{index}]"
        facts = _describe_context_pack_citation(citation, label)
        if facts.citation_id in citation_ids:
            raise ContractSemanticError(f"{label} duplicates citation_id {facts.citation_id!r}")
        previous = _require_ascending(
            _identity_sort_key(facts.citation_id), previous, label, "citation_id"
        )
        citation_ids.add(facts.citation_id)
        if facts.target not in selected_targets:
            kind, first, second = facts.target
            raise ContractSemanticError(
                f"{label} cites {kind} {(first, second)!r}, which this pack did not select; "
                "a citation resolves to the exact identity and version of something the "
                "pack returned, never merely to the same source"
            )
        if facts.locator in seen_locators:
            raise ContractSemanticError(
                f"{label} duplicates an existing citation of the same target at the same "
                "content_pointer and source_span; the same target at two genuinely different "
                "locations is two citations, the same location twice is one"
            )
        seen_locators.add(facts.locator)
        cited_targets.add(facts.target)

    uncited = sorted(selected_targets - cited_targets)
    if uncited:
        raise ContractSemanticError(
            f"this pack selected content nothing cites: {uncited!r}; a selected item with no "
            "citation is material the caller was handed and cannot attribute"
        )

    tokens_used_expected, used_citation_ids = _validate_sections(result.sections, citation_ids)
    unused = sorted(citation_ids - used_citation_ids)
    if unused:
        raise ContractSemanticError(
            f"citation(s) {unused!r} are used by no section; a citation supports something "
            "the caller was given, and one that supports nothing states nothing"
        )

    _validate_statements(result, citation_ids)

    budget_value = result.budget
    _require_type(budget_value, ContextPackBudget, "budget")
    assert isinstance(budget_value, ContextPackBudget)
    budget = _validate_token_budget(budget_value.token_budget, "budget.token_budget")
    _require_exact(budget, request.token_budget, "budget.token_budget")
    tokens_used = _require_int(budget_value.tokens_used, "budget.tokens_used")
    _validate_token_count(tokens_used, "budget.tokens_used")
    if tokens_used != tokens_used_expected:
        raise ContractSemanticError(
            f"budget.tokens_used {tokens_used!r} is not the sum of this pack's section token "
            f"counts ({tokens_used_expected!r})"
        )
    if tokens_used > budget:
        raise ContractSemanticError(
            f"budget.tokens_used {tokens_used!r} exceeds budget.token_budget {budget!r}"
        )

    _validate_reproducibility(
        result.reproducibility,
        request=request,
        budget=budget,
        selected_evidence=tuple(sorted(selected_evidence, key=lambda item: _identity_sort_key(*item))),
        selected_records=tuple(sorted(selected_records, key=lambda item: _identity_sort_key(*item))),
        expected_workspace_id=workspace_id,
        expected_authority=authority,
        expected_scopes=scopes,
        expected_purpose=purpose,
        expected_policy_versions=policy_versions,
        expected_candidate_set_checksum=candidate_set.checksum,
        canonical_resolution_time=resolution_time,
        resolution_instant=resolution_instant,
        response_freshness=response_freshness,
        expected=expected_reproducibility,
    )

    _verify_context_pack_digest(result.to_wire(), "result")


def validate_context_pack_build_result_document(
    document: str | bytes,
    *,
    request: object,
    expected_workspace_id: object,
    expected_authority: object,
    expected_scopes: object,
    expected_purpose: object,
    expected_policy_versions: object,
    expected_authorized_candidate_set: object,
    canonical_resolution_time: object,
    response_freshness: object,
    expected_normalized_query: object = None,
    expected_normalization_version: object = None,
    expected_builder_version: object = None,
    expected_retrieval_version: object = None,
    expected_ranking_version: object = None,
    expected_reranking_version: object = None,
    expected_selection_version: object = None,
    expected_tokenizer_id: object = None,
    expected_tokenizer_version: object = None,
    expected_summarizer_version: object = None,
    expected_model_versions: object = None,
) -> ContextPackBuildResult:
    """Verify a raw Context Pack document and fully validate the result it carries.

    The raw-wire companion to :func:`validate_context_pack_build_result`, and the entry
    point to reach for at a trust boundary. It runs
    :func:`verify_context_pack_artifact_document` first -- duplicate-member detection, exact
    strict-field preservation, and digest verification, all of which need the bytes rather
    than a decoded value -- and only then applies the full semantic rule set to the decoded
    result. Returns the verified result so a caller need not decode it a second time.

    `expected_authorized_candidate_set` is mandatory here for exactly the reason it is
    mandatory there, and more pointedly: this is the entry point a trust boundary uses, and
    the authorized frontier is the one claim in a pack that byte-level integrity cannot
    check at all. Every optional `expected_*` producer-assertion argument is carried through
    unchanged and means exactly what it means there. They are offered on both entry points
    deliberately: the trust boundary is where a caller is *most* likely to know what builder,
    tokenizer, or normalization it asked for, and a binding available only on the trusted-DTO
    path would be missing from the one call that needs it.
    """
    result = verify_context_pack_artifact_document(document)
    validate_context_pack_build_result(
        result,
        request=request,
        expected_workspace_id=expected_workspace_id,
        expected_authority=expected_authority,
        expected_scopes=expected_scopes,
        expected_purpose=expected_purpose,
        expected_policy_versions=expected_policy_versions,
        expected_authorized_candidate_set=expected_authorized_candidate_set,
        canonical_resolution_time=canonical_resolution_time,
        response_freshness=response_freshness,
        expected_normalized_query=expected_normalized_query,
        expected_normalization_version=expected_normalization_version,
        expected_builder_version=expected_builder_version,
        expected_retrieval_version=expected_retrieval_version,
        expected_ranking_version=expected_ranking_version,
        expected_reranking_version=expected_reranking_version,
        expected_selection_version=expected_selection_version,
        expected_tokenizer_id=expected_tokenizer_id,
        expected_tokenizer_version=expected_tokenizer_version,
        expected_summarizer_version=expected_summarizer_version,
        expected_model_versions=expected_model_versions,
    )
    return result
