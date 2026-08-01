"""Pure semantic validation for the workspace and governed-memory DTOs (A2.2, ADR-038/ADR-039).

Structural decoding lives in :mod:`generated`; version/capability semantics live in
:mod:`compatibility`. This module is the same kind of layer for the workspace and
governed-memory shapes added in the A2.2 slice: pure standard-library functions over
already-decoded dataclasses, raising :class:`~omnivia_core.contracts.v1.compatibility.
ContractSemanticError` on a violated invariant. Nothing here parses or produces JSON.

This module does not implement request-envelope-to-catalogue dispatch: it exposes the
narrow, composable checks a later A2.5 slice will wire against a concrete operation
catalogue (`scope_kind` -> `workspace_id` agreement, per-DTO temporal/evidence/paging
invariants). Standard library only. Nothing here may depend on runtime, storage, HTTP,
MCP, CLI, Platform, Dev, or a validation framework.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Final

from omnivia_core.contracts.v1.compatibility import (
    ContractSemanticError,
    validate_version_window,
    version_in_window,
)
from omnivia_core.contracts.v1.generated import (
    EVIDENCE_DISPOSITION_PATTERN,
    GOVERNANCE_LAYER_PATTERN,
    GOVERNANCE_STATE_PATTERN,
    GOVERNED_RECORD_TYPE_PATTERN,
    IDENTIFIER_PATTERN,
    OPEN_CODE_PATTERN,
    RECORD_CURRENTNESS_PATTERN,
    RECORD_DOMAIN_SCOPE_PATTERN,
    RECORD_ID_PATTERN,
    RECORD_VERSION_PATTERN,
    TIMESTAMP_PATTERN,
    WORKSPACE_ID_PATTERN,
    CandidateAssertion,
    CandidateExtractionMetadata,
    EvidenceReference,
    GovernedRecord,
    MemoryCreateInput,
    MemoryCreateResult,
    ProvenanceEntry,
    RecordIdentity,
    RecordProvenance,
    RecordTemporalMetadata,
    SourceReference,
    SourceSpan,
    SupersessionReference,
    WorkspaceCompatibility,
)

__all__ = [
    "AUTHORITY_LEVELS_IMPLYING_ACCEPTED",
    "AUTHORITY_LEVELS_REQUIRING_REVIEWER",
    "AUTHORITY_LEVEL_PROPOSED",
    "EVIDENCE_DISPOSITIONS_PERMITTING_EMPTY_SOURCES",
    "GOVERNANCE_LAYER_CANDIDATE",
    "GOVERNANCE_STATES_KNOWN_NON_ACCEPTED",
    "GOVERNANCE_STATES_REQUIRING_REVIEWER",
    "GOVERNANCE_STATE_ACCEPTED",
    "GOVERNANCE_STATE_CANDIDATE",
    "GOVERNANCE_STATE_PROPOSED",
    "GOVERNANCE_STATE_REJECTED",
    "GOVERNED_RECORD_VIEWS",
    "GOVERNED_RECORD_VIEW_CANDIDATES",
    "GOVERNED_RECORD_VIEW_CURRENT_CANONICAL",
    "GOVERNED_RECORD_VIEW_HISTORY",
    "MAX_PAGE_LIMIT",
    "MEMORY_CREATE_REQUIRED_AUTHORITY_LEVEL",
    "MEMORY_CREATE_REQUIRED_CURRENTNESS",
    "MEMORY_CREATE_REQUIRED_GOVERNANCE_STATE",
    "MEMORY_CREATE_REQUIRED_LAYER",
    "MEMORY_CREATE_RESERVED_INPUT_FIELDS",
    "MIN_PAGE_LIMIT",
    "SCOPE_KIND_INSTALLATION",
    "SCOPE_KIND_WORKSPACE",
    "decode_memory_create_input",
    "resolve_governed_record_view",
    "validate_evidence_disposition_sources",
    "validate_governed_record",
    "validate_governed_record_authority_coherence",
    "validate_governed_record_layer_coherence",
    "validate_memory_create_input",
    "validate_memory_create_result",
    "validate_operation_scope_workspace_id",
    "validate_page_limit",
    "validate_record_currentness_consistency",
    "validate_record_domain_scope",
    "validate_record_provenance",
    "validate_record_temporal_metadata",
    "validate_workspace_compatibility",
]

# --- governed-record view selection -----------------------------------------

GOVERNED_RECORD_VIEW_CURRENT_CANONICAL: Final = "current_canonical"
GOVERNED_RECORD_VIEW_CANDIDATES: Final = "candidates"
GOVERNED_RECORD_VIEW_HISTORY: Final = "history"

GOVERNED_RECORD_VIEWS: Final[tuple[str, ...]] = (
    GOVERNED_RECORD_VIEW_CURRENT_CANONICAL,
    GOVERNED_RECORD_VIEW_CANDIDATES,
    GOVERNED_RECORD_VIEW_HISTORY,
)


def resolve_governed_record_view(view: str | None) -> str:
    """Resolve an absent `view` selection to its default, `current_canonical`.

    `GovernedRecordView` is an open wire vocabulary, so an unrecognized non-``None``
    value is returned unchanged rather than coerced: only absence gets a default.
    """
    return view if view is not None else GOVERNED_RECORD_VIEW_CURRENT_CANONICAL


# --- direct-entry type guards -------------------------------------------------

def _require_type(value: object, expected: type, label: str) -> None:
    """Raise unless `value` is an instance of `expected`.

    Every public function in this module is a direct entry point: a caller may hand-build a
    dataclass rather than decode one through `from_wire`, and a frozen dataclass enforces
    nothing about the *types* of the fields it was handed. So a wrongly typed nested value
    must surface here as a :class:`ContractSemanticError` rather than as a raw
    `TypeError`/`AttributeError` from whatever this module does with it next. `bool` is
    rejected wherever a non-`bool` type is expected, since `bool` subclasses `int` and would
    otherwise satisfy an `int` guard silently.
    """
    if isinstance(value, bool) and expected is not bool:
        raise ContractSemanticError(f"{label}: expected {expected.__name__}, got bool")
    if not isinstance(value, expected):
        raise ContractSemanticError(
            f"{label}: expected {expected.__name__}, got {type(value).__name__}"
        )


def _require_str(value: object, label: str) -> str:
    """Raise unless `value` is a `str`, returning it narrowed."""
    if isinstance(value, bool) or not isinstance(value, str):
        raise ContractSemanticError(f"{label}: expected a string, got {type(value).__name__}")
    return value


def _require_int(value: object, label: str) -> int:
    """Raise unless `value` is a non-`bool` `int`, returning it narrowed."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractSemanticError(f"{label}: expected an integer, got {type(value).__name__}")
    return value


def _require_number(value: object, label: str) -> float:
    """Raise unless `value` is a non-`bool` `int`/`float`, returning it as a `float`."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractSemanticError(f"{label}: expected a number, got {type(value).__name__}")
    return float(value)


def _require_sequence(value: object, label: str) -> Sequence[object]:
    """Raise unless `value` is a non-string sequence, returning it narrowed.

    `str`/`bytes` are sequences but never a valid list-of-DTOs, so they are rejected rather
    than silently iterated one character at a time.
    """
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractSemanticError(f"{label}: expected a sequence, got {type(value).__name__}")
    return value


# --- temporal ordering -------------------------------------------------------

_TIMESTAMP_RE: Final = re.compile(TIMESTAMP_PATTERN)


def _parse_timestamp(value: object, label: str) -> datetime:
    """Parse `value` as a canonical `Timestamp`: UTC, literal `Z`, calendar-valid.

    `value` is type-guarded first (:func:`_require_str`), since every caller of this is
    reachable directly with a hand-built dataclass whose timestamp field may not be a string
    at all. `datetime.fromisoformat` alone then accepts spellings the canonical `Timestamp`
    contract forbids -- a naive timestamp, a non-`Z` numeric offset, or a `Z` embedded in
    what is otherwise a differently shaped string -- so the wire pattern is checked next.
    Only after both gates does `fromisoformat` get a chance to reject a value that matches
    the pattern but names a calendar-invalid instant (`2024-02-30T00:00:00Z`).
    """
    text = _require_str(value, label)
    if not _TIMESTAMP_RE.match(text):
        raise ContractSemanticError(
            f"{label}: {text!r} is not a canonical RFC 3339 UTC timestamp "
            "(naive, offset, and malformed spellings are all rejected)"
        )
    try:
        return datetime.fromisoformat(text)
    except ValueError as error:
        raise ContractSemanticError(
            f"{label}: {text!r} is not a valid RFC 3339 timestamp"
        ) from error


def validate_record_temporal_metadata(temporal: object) -> None:
    """Raise unless every present instant on `temporal` independently parses as a
    canonical timestamp, and the instants that must agree are in a consistent order.

    Every present field -- `event_at`, `observed_at`, `ingested_at`, `recorded_at`,
    `valid_from`, `valid_until`, `superseded_at` -- is parsed on its own first, so a
    malformed optional field is rejected even when it never participates in an
    ordering comparison (`event_at`/`observed_at`), and a one-sided `valid_from`/
    `valid_until` is validated even when its counterpart is absent and no ordering
    check can run. Three orderings are then checked, each independently, since any
    one of them being reversed states something that cannot be true regardless of
    the others:

    - `valid_from` must not be after `valid_until`, when both are present;
    - `ingested_at` must not be after `recorded_at` (a record cannot be persisted
      before the system first ingested the fact behind it);
    - `superseded_at`, when present, must not be before `recorded_at` (a version
      cannot be superseded before it was itself recorded).

    A direct entry point: `temporal` is type-guarded, and every instant is parsed through
    :func:`_parse_timestamp`, which guards each field's own type, so a hand-built
    `RecordTemporalMetadata` carrying a non-string instant raises `ContractSemanticError`
    rather than a raw `TypeError`.
    """
    _require_type(temporal, RecordTemporalMetadata, "temporal")
    assert isinstance(temporal, RecordTemporalMetadata)
    if temporal.event_at is not None:
        _parse_timestamp(temporal.event_at, "event_at")
    if temporal.observed_at is not None:
        _parse_timestamp(temporal.observed_at, "observed_at")

    valid_from = None
    valid_until = None
    if temporal.valid_from is not None:
        valid_from = _parse_timestamp(temporal.valid_from, "valid_from")
    if temporal.valid_until is not None:
        valid_until = _parse_timestamp(temporal.valid_until, "valid_until")
    if valid_from is not None and valid_until is not None and valid_from > valid_until:
        raise ContractSemanticError(
            f"valid_from {temporal.valid_from!r} is after valid_until {temporal.valid_until!r}"
        )

    ingested_at = _parse_timestamp(temporal.ingested_at, "ingested_at")
    recorded_at = _parse_timestamp(temporal.recorded_at, "recorded_at")
    if ingested_at > recorded_at:
        raise ContractSemanticError(
            f"ingested_at {temporal.ingested_at!r} is after recorded_at {temporal.recorded_at!r}"
        )

    if temporal.superseded_at is not None:
        superseded_at = _parse_timestamp(temporal.superseded_at, "superseded_at")
        if superseded_at < recorded_at:
            raise ContractSemanticError(
                f"superseded_at {temporal.superseded_at!r} is before recorded_at "
                f"{temporal.recorded_at!r}"
            )


# --- bounded pattern-vocabulary validation -----------------------------------
#
# Every validator in this section takes `object`, not `str`, and guards the type before
# measuring or matching it (:func:`_require_str`). Each one is reachable from a public
# direct entry point holding a hand-built dataclass, where a field declared `str` may hold
# anything at all, so a wrong type has to become a `ContractSemanticError` here rather than
# a raw `TypeError` out of `len()` or `re.fullmatch()`.

_IDENTIFIER_RE: Final = re.compile(IDENTIFIER_PATTERN)
_OPEN_CODE_RE: Final = re.compile(OPEN_CODE_PATTERN)
_RECORD_DOMAIN_SCOPE_RE: Final = re.compile(RECORD_DOMAIN_SCOPE_PATTERN)

_BOUNDED_VALUE_MAX_LENGTH: Final = 128
"""Shared `maxLength` every `Identifier` / `OpenCode` / `RecordDomainScope` schema
definition declares. None of the three wire patterns encode an upper bound
themselves -- only the JSON Schema `maxLength` does -- so an overlong,
otherwise pattern-valid value is only caught by checking length separately.
"""


def _validate_identifier(value: object, label: str) -> None:
    """Raise unless `value` is a bounded, non-empty, pattern-valid `Identifier`.

    Structural decoding never enforces `Identifier`'s `pattern`/`maxLength` (that is
    exclusively the JSON Schema checker's job), so a caller decoding a tolerant
    production payload needs this to enforce both at runtime. `fullmatch` is used
    even though the pattern is itself fully anchored (`^...$`), to make the
    full-match intent explicit at the call site.
    """
    text = _require_str(value, label)
    if len(text) > _BOUNDED_VALUE_MAX_LENGTH or not _IDENTIFIER_RE.fullmatch(text):
        raise ContractSemanticError(f"{label}: {text!r} is not a valid Identifier")


def _validate_open_code(value: object, label: str) -> None:
    """Raise unless `value` is a bounded, non-empty, pattern-valid `OpenCode`.

    `OpenCode` is an open wire vocabulary: this only checks the shape every value
    -- known or not yet seen by this build -- must have, so an unrecognized but
    well-formed value still passes and is preserved verbatim by the caller.
    """
    text = _require_str(value, label)
    if len(text) > _BOUNDED_VALUE_MAX_LENGTH or not _OPEN_CODE_RE.fullmatch(text):
        raise ContractSemanticError(f"{label}: {text!r} is not a valid OpenCode")


_WORKSPACE_ID_RE: Final = re.compile(WORKSPACE_ID_PATTERN)
_GOVERNED_RECORD_TYPE_RE: Final = re.compile(GOVERNED_RECORD_TYPE_PATTERN)
_EVIDENCE_DISPOSITION_RE: Final = re.compile(EVIDENCE_DISPOSITION_PATTERN)
_RECORD_ID_RE: Final = re.compile(RECORD_ID_PATTERN)
_RECORD_VERSION_RE: Final = re.compile(RECORD_VERSION_PATTERN)
_GOVERNANCE_LAYER_RE: Final = re.compile(GOVERNANCE_LAYER_PATTERN)
_GOVERNANCE_STATE_RE: Final = re.compile(GOVERNANCE_STATE_PATTERN)
_RECORD_CURRENTNESS_RE: Final = re.compile(RECORD_CURRENTNESS_PATTERN)

_RECORD_VERSION_MAX_LENGTH: Final = 512
"""`RecordVersion`'s schema `maxLength`. Unlike every other bounded value validated in this
module, `RecordVersion` allows up to 512 characters, not the shared 128-character bound."""


def _validate_workspace_id(value: object, label: str) -> None:
    """Raise unless `value` is a bounded, non-empty, pattern-valid `WorkspaceId`.

    `fullmatch` is required, not `.match`: `WORKSPACE_ID_PATTERN` ends in `$`, and
    Python's `$` matches immediately before a trailing newline as well as at the true
    end of string, so `.match` alone would silently accept `"ws-1\\n"`.
    """
    text = _require_str(value, label)
    if len(text) > _BOUNDED_VALUE_MAX_LENGTH or not _WORKSPACE_ID_RE.fullmatch(text):
        raise ContractSemanticError(f"{label}: {text!r} is not a valid WorkspaceId")


def _validate_governed_record_type(value: object, label: str) -> None:
    """Raise unless `value` is a bounded, non-empty, pattern-valid `GovernedRecordType`.

    `GovernedRecordType` is an open wire vocabulary: this only checks the shape every
    value -- known or not yet seen by this build -- must have, so an unrecognized but
    well-formed record type still passes and is preserved verbatim by the caller.
    """
    text = _require_str(value, label)
    if len(text) > _BOUNDED_VALUE_MAX_LENGTH or not _GOVERNED_RECORD_TYPE_RE.fullmatch(text):
        raise ContractSemanticError(f"{label}: {text!r} is not a valid GovernedRecordType")


def _validate_evidence_disposition_code(value: object, label: str) -> None:
    """Raise unless `value` is a bounded, non-empty, pattern-valid `EvidenceDisposition`.

    `EvidenceDisposition` is an open wire vocabulary: this only checks shape, not
    membership in :data:`EVIDENCE_DISPOSITIONS_PERMITTING_EMPTY_SOURCES` or any other
    known value -- an unrecognized but well-formed disposition still passes here.
    """
    text = _require_str(value, label)
    if len(text) > _BOUNDED_VALUE_MAX_LENGTH or not _EVIDENCE_DISPOSITION_RE.fullmatch(text):
        raise ContractSemanticError(f"{label}: {text!r} is not a valid EvidenceDisposition")


def _validate_record_id(value: object, label: str) -> None:
    """Raise unless `value` is a bounded, non-empty, pattern-valid `RecordId`."""
    text = _require_str(value, label)
    if len(text) > _BOUNDED_VALUE_MAX_LENGTH or not _RECORD_ID_RE.fullmatch(text):
        raise ContractSemanticError(f"{label}: {text!r} is not a valid RecordId")


def _validate_record_version(value: object, label: str) -> None:
    """Raise unless `value` is a bounded, non-empty, pattern-valid `RecordVersion`.

    `RecordVersion`'s own `maxLength` is 512, not the 128-character bound shared by
    every other identifier/code validated in this module.
    """
    text = _require_str(value, label)
    if len(text) > _RECORD_VERSION_MAX_LENGTH or not _RECORD_VERSION_RE.fullmatch(text):
        raise ContractSemanticError(f"{label}: {text!r} is not a valid RecordVersion")


def _validate_governance_layer(value: object, label: str) -> None:
    """Raise unless `value` is a bounded, non-empty, pattern-valid `GovernanceLayer`.

    `GovernanceLayer` is an open wire vocabulary: this only checks shape, not
    membership in any known L0-L4 layer -- an unrecognized but well-formed layer
    still passes here.
    """
    text = _require_str(value, label)
    if len(text) > _BOUNDED_VALUE_MAX_LENGTH or not _GOVERNANCE_LAYER_RE.fullmatch(text):
        raise ContractSemanticError(f"{label}: {text!r} is not a valid GovernanceLayer")


def _validate_governance_state_code(value: object, label: str) -> None:
    """Raise unless `value` is a bounded, non-empty, pattern-valid `GovernanceState`.

    `GovernanceState` is an open wire vocabulary: this only checks shape, not
    membership in any known state -- an unrecognized but well-formed state still
    passes here.
    """
    text = _require_str(value, label)
    if len(text) > _BOUNDED_VALUE_MAX_LENGTH or not _GOVERNANCE_STATE_RE.fullmatch(text):
        raise ContractSemanticError(f"{label}: {text!r} is not a valid GovernanceState")


def _validate_record_currentness_code(value: object, label: str) -> None:
    """Raise unless `value` is a bounded, non-empty, pattern-valid `RecordCurrentness`.

    `RecordCurrentness` is an open wire vocabulary: this only checks shape, not
    membership in :data:`RECORD_CURRENTNESS_CURRENT`/:data:`RECORD_CURRENTNESS_SUPERSEDED`
    -- an unrecognized but well-formed currentness still passes here.
    """
    text = _require_str(value, label)
    if len(text) > _BOUNDED_VALUE_MAX_LENGTH or not _RECORD_CURRENTNESS_RE.fullmatch(text):
        raise ContractSemanticError(f"{label}: {text!r} is not a valid RecordCurrentness")


def _validate_supersession_reference(reference: object, label: str) -> None:
    """Raise unless `reference`'s `record_id`/`version`/`reason` are each individually
    valid.

    `version` and `reason` are optional on `SupersessionReference` and are only
    validated when present; `record_id` is always required and always validated.
    """
    _require_type(reference, SupersessionReference, label)
    assert isinstance(reference, SupersessionReference)
    _validate_record_id(reference.record_id, f"{label}.record_id")
    if reference.version is not None:
        _validate_record_version(reference.version, f"{label}.version")
    if reference.reason is not None:
        _validate_open_code(reference.reason, f"{label}.reason")


def _validate_record_identity(identity: object, label: str = "identity") -> None:
    """Raise unless `identity`'s own fields are each individually valid.

    Covers the bounded, pattern-valid shape of `record_id`, `version`, `layer`,
    `governance_state`, and `currentness`, plus `supersedes`/`superseded_by` when
    present (:func:`_validate_supersession_reference`). This checks shape only --
    cross-field coherence (currentness vs. supersession, governance vs. authority) is
    :func:`validate_record_currentness_consistency` and
    :func:`validate_governed_record_authority_coherence`'s job, not this function's.
    """
    _require_type(identity, RecordIdentity, label)
    assert isinstance(identity, RecordIdentity)
    _validate_record_id(identity.record_id, f"{label}.record_id")
    _validate_record_version(identity.version, f"{label}.version")
    _validate_governance_layer(identity.layer, f"{label}.layer")
    _validate_governance_state_code(identity.governance_state, f"{label}.governance_state")
    _validate_record_currentness_code(identity.currentness, f"{label}.currentness")
    if identity.supersedes is not None:
        _validate_supersession_reference(identity.supersedes, f"{label}.supersedes")
    if identity.superseded_by is not None:
        _validate_supersession_reference(identity.superseded_by, f"{label}.superseded_by")


def validate_record_domain_scope(domain_scope: object) -> None:
    """Raise unless `domain_scope` is a bounded, non-empty, pattern-valid `RecordDomainScope`.

    `RecordDomainScope` is open -- an unrecognized but well-formed classification such
    as `some_future.domain` is preserved, not rejected -- but it is not unconstrained:
    whitespace, malformed nonempty values such as `!!!`, `Upper.Case`, or
    `personal..preferences`, and overlong values are all rejected, using the generated
    `RECORD_DOMAIN_SCOPE_PATTERN` as a full-match constraint rather than duplicating it.
    """
    text = _require_str(domain_scope, "domain_scope")
    if len(text) > _BOUNDED_VALUE_MAX_LENGTH or not _RECORD_DOMAIN_SCOPE_RE.fullmatch(text):
        raise ContractSemanticError(f"domain_scope {text!r} is not a valid RecordDomainScope")


# --- evidence disposition / sources agreement --------------------------------

EVIDENCE_DISPOSITIONS_PERMITTING_EMPTY_SOURCES: Final[frozenset[str]] = frozenset(
    {"unavailable", "redacted"}
)
"""The only `EvidenceDisposition` values that excuse an empty `sources` list.

`EvidenceDisposition` is an open wire vocabulary, so a value outside this frozen set --
including `available` and any value this build has never seen -- must not be read as
excusing empty sources: that would let an unrecognized future disposition silently
disable an invariant this build cannot verify holds.
"""


def validate_evidence_disposition_sources(disposition: object, sources: object) -> None:
    """Raise unless `sources` being empty is actually excused by `disposition`.

    Covers both required cases with one rule: `available` never excuses empty
    sources, and neither does any disposition outside
    :data:`EVIDENCE_DISPOSITIONS_PERMITTING_EMPTY_SOURCES` -- including an
    unrecognized future value, which fails safe rather than being assumed to
    excuse it.

    A direct entry point: `sources` is guarded as a real sequence rather than merely tested
    for truthiness, so a caller passing something that is not a source list at all fails
    loudly instead of being read as "non-empty, therefore excused".
    """
    if _require_sequence(sources, "sources"):
        return
    if disposition not in EVIDENCE_DISPOSITIONS_PERMITTING_EMPTY_SOURCES:
        raise ContractSemanticError(
            f"evidence_disposition {disposition!r} does not excuse empty sources; only "
            f"{sorted(EVIDENCE_DISPOSITIONS_PERMITTING_EMPTY_SOURCES)!r} may have zero sources"
        )


# --- currentness / supersession agreement ------------------------------------

RECORD_CURRENTNESS_CURRENT: Final = "current"
RECORD_CURRENTNESS_SUPERSEDED: Final = "superseded"


def validate_record_currentness_consistency(identity: object, temporal: object) -> None:
    """Raise unless `identity`/`temporal` agree on this version's supersession state.

    `current` cannot carry a `superseded_by` pointer or a `superseded_at` instant --
    both assert this version was later replaced, which contradicts being the active one.
    `superseded` is the converse: it requires both, since a version marked superseded
    with no record of what replaced it, or when, is not fully stated. Neither
    `supersedes` nor `superseded_by` may point at this same version's own identity: a
    version cannot be its own predecessor or successor, regardless of `currentness`.

    A direct entry point: both arguments are type-guarded before any field is read.
    """
    _require_type(identity, RecordIdentity, "identity")
    assert isinstance(identity, RecordIdentity)
    _require_type(temporal, RecordTemporalMetadata, "temporal")
    assert isinstance(temporal, RecordTemporalMetadata)
    if identity.currentness == RECORD_CURRENTNESS_CURRENT:
        if identity.superseded_by is not None:
            raise ContractSemanticError(
                "currentness 'current' must not carry a superseded_by pointer"
            )
        if temporal.superseded_at is not None:
            raise ContractSemanticError(
                "currentness 'current' must not carry a superseded_at instant"
            )
    if identity.currentness == RECORD_CURRENTNESS_SUPERSEDED and (
        identity.superseded_by is None or temporal.superseded_at is None
    ):
        raise ContractSemanticError(
            "currentness 'superseded' requires both a superseded_by pointer and a "
            "superseded_at instant"
        )

    own = (identity.record_id, identity.version)
    if identity.supersedes is not None and (
        identity.supersedes.record_id,
        identity.supersedes.version,
    ) == own:
        raise ContractSemanticError("a record must not supersede itself")
    if identity.superseded_by is not None and (
        identity.superseded_by.record_id,
        identity.superseded_by.version,
    ) == own:
        raise ContractSemanticError("a record must not be superseded by itself")


# --- governance-state / authority-level coherence ----------------------------

GOVERNANCE_STATE_PROPOSED: Final = "proposed"
GOVERNANCE_STATE_ACCEPTED: Final = "accepted"
GOVERNANCE_STATE_REJECTED: Final = "rejected"
GOVERNANCE_STATE_CANDIDATE: Final = "candidate"

AUTHORITY_LEVEL_PROPOSED: Final = "proposed"
"""The known authority level a known `accepted` governance state must never carry.

`accepted` asserts a decision was made in this record's favour; `proposed` authority
asserts the opposite -- no decision has been made yet. Both cannot be true of the same
record version, regardless of what an unrecognized open value on either side might mean.
"""

GOVERNANCE_LAYER_CANDIDATE: Final = "l1"
"""The known candidate governance layer: a record proposed but not yet governed/canonical
knowledge. Distinct from :data:`GOVERNANCE_STATE_CANDIDATE`, which is a `governance_state`
value, not a `layer` value; the two happen to share a name in the domain vocabulary but are
independent axes on `RecordIdentity`.
"""

GOVERNANCE_STATES_REQUIRING_REVIEWER: Final[frozenset[str]] = frozenset(
    {GOVERNANCE_STATE_ACCEPTED, GOVERNANCE_STATE_REJECTED}
)
"""Governance states that assert a decision was made and so must carry a reviewer.

`accepted` and `rejected` are both decision-bearing regardless of what `authority_level`
happens to say: an unrecognized-but-valid open `authority_level` code must never be read
as already satisfying, or exempting a record from, this requirement -- this build cannot
verify what an unknown authority level means, so the check is keyed on the known,
closed-vocabulary `governance_state` instead. Exact membership only. Requiring a reviewer
for `rejected` does not make it a favourable or accepted authority decision: see
:data:`GOVERNANCE_STATES_KNOWN_NON_ACCEPTED`, which still forbids `rejected` from carrying
any authority level in :data:`AUTHORITY_LEVELS_REQUIRING_REVIEWER`.
"""

AUTHORITY_LEVELS_IMPLYING_ACCEPTED: Final[frozenset[str]] = frozenset({"reviewed", "canonical"})
"""Authority levels a freshly `proposed` governance state must never carry.

A record still in the `proposed` governance state cannot simultaneously already
carry a `reviewed` or `canonical` authority level: that would assert a review or
canonicalization decision that has not happened yet. Exact membership only --
an authority level outside this frozen set, including one this build has never
seen, is never treated as implying accepted authority.
"""

AUTHORITY_LEVELS_REQUIRING_REVIEWER: Final[frozenset[str]] = frozenset(
    {"reviewed", "canonical", "accepted"}
)
"""Authority levels that must carry a reviewer/policy identity.

`reviewed`, `canonical`, and `accepted` authority all assert that some
reviewer or policy actually made a decision; a record claiming one of these
without a `reviewer` is asserting a decision nobody made. Exact membership
only -- an unrecognized future authority level is never assumed to require,
or to already satisfy, this rule.
"""

GOVERNANCE_STATES_KNOWN_NON_ACCEPTED: Final[frozenset[str]] = frozenset(
    {GOVERNANCE_STATE_PROPOSED, GOVERNANCE_STATE_REJECTED, GOVERNANCE_STATE_CANDIDATE}
)
"""Known governance states that assert a record has *not* been accepted.

`reviewed`/`canonical`/`accepted` authority (:data:`AUTHORITY_LEVELS_REQUIRING_REVIEWER`)
asserts a decision was made in this record's favour, which contradicts a known
governance state that says the opposite -- still `proposed`, explicitly `rejected`, or
merely a `candidate` awaiting a decision. Exact membership only: an unrecognized open
`governance_state` this build has never seen is never assumed to belong to this set, so
it is never treated as contradicting a decision-bearing authority level. Only
:data:`GOVERNANCE_STATE_ACCEPTED` -- the known accepted state -- and an unrecognized
open state are compatible with decision-bearing authority.
"""


def validate_governed_record_authority_coherence(
    governance_state: object, authority_level: object, reviewer: object
) -> None:
    """Raise unless `governance_state`, `authority_level`, and `reviewer` agree.

    `governance_state` and `authority_level` are each validated shape-first, using the
    same canonical `GovernanceState` and `OpenCode` checks a decoded `RecordIdentity`/
    `GovernedRecord` already gets, since this function is itself part of the public API
    and must not trust a blank or malformed value merely because a caller invoked it
    directly rather than through :func:`validate_governed_record`. A present `reviewer`
    must always be a bounded, pattern-valid `Identifier`: a blank or malformed reviewer
    is rejected before any other check runs. Beyond that, six contradictions are rejected:
    a `proposed` governance state carrying a
    `reviewed`/`canonical` authority level (:data:`AUTHORITY_LEVELS_IMPLYING_ACCEPTED`),
    a `proposed` governance state already carrying a `reviewer`, a known `accepted`
    governance state carrying known `proposed` authority_level
    (:data:`AUTHORITY_LEVEL_PROPOSED`) -- the two assert opposite decisions about the
    same record version -- an accepted/reviewed/canonical authority level
    (:data:`AUTHORITY_LEVELS_REQUIRING_REVIEWER`) with no `reviewer` recorded, a known
    decision-bearing governance state (:data:`GOVERNANCE_STATES_REQUIRING_REVIEWER`)
    with no `reviewer` recorded -- this fires even when `authority_level` is an
    unrecognized-but-valid open code, since an unknown authority level must never be
    read as exempting a known `accepted` governance state from needing a reviewer --
    and a decision-bearing authority level paired with a known non-accepted governance
    state (:data:`GOVERNANCE_STATES_KNOWN_NON_ACCEPTED`), such as `rejected` or
    `candidate` authority carrying `reviewed`/`canonical`/`accepted` authority. All
    frozen sets are exact-membership only, so an unrecognized open authority-level or
    governance-state value is never silently treated as canonical/accepted authority,
    nor silently treated as contradicting it.
    """
    _validate_governance_state_code(governance_state, "governance_state")
    _validate_open_code(authority_level, "authority_level")
    if reviewer is not None:
        _validate_identifier(reviewer, "reviewer")
    if governance_state == GOVERNANCE_STATE_PROPOSED:
        if authority_level in AUTHORITY_LEVELS_IMPLYING_ACCEPTED:
            raise ContractSemanticError(
                f"governance_state {GOVERNANCE_STATE_PROPOSED!r} must not carry authority_level "
                f"{authority_level!r}"
            )
        if reviewer is not None:
            raise ContractSemanticError(
                f"governance_state {GOVERNANCE_STATE_PROPOSED!r} must not carry a reviewer"
            )
    if (
        governance_state == GOVERNANCE_STATE_ACCEPTED
        and authority_level == AUTHORITY_LEVEL_PROPOSED
    ):
        raise ContractSemanticError(
            f"governance_state {GOVERNANCE_STATE_ACCEPTED!r} must not carry authority_level "
            f"{AUTHORITY_LEVEL_PROPOSED!r}"
        )
    if authority_level in AUTHORITY_LEVELS_REQUIRING_REVIEWER and reviewer is None:
        raise ContractSemanticError(
            f"authority_level {authority_level!r} requires a reviewer/policy identity"
        )
    if governance_state in GOVERNANCE_STATES_REQUIRING_REVIEWER and reviewer is None:
        raise ContractSemanticError(
            f"governance_state {governance_state!r} requires a reviewer/policy identity, "
            f"regardless of authority_level {authority_level!r}"
        )
    if (
        authority_level in AUTHORITY_LEVELS_REQUIRING_REVIEWER
        and governance_state in GOVERNANCE_STATES_KNOWN_NON_ACCEPTED
    ):
        raise ContractSemanticError(
            f"authority_level {authority_level!r} asserts a decision was made in this "
            f"record's favour, which contradicts known non-accepted governance_state "
            f"{governance_state!r}"
        )


def validate_governed_record_layer_coherence(
    layer: object, governance_state: object, authority_level: object
) -> None:
    """Raise unless a known :data:`GOVERNANCE_LAYER_CANDIDATE` layer's `governance_state`/
    `authority_level` are coherent with still being a candidate.

    A record in the known L1 candidate layer cannot simultaneously carry a known
    `accepted` `governance_state`, nor a known decision-bearing `authority_level`
    (:data:`AUTHORITY_LEVELS_REQUIRING_REVIEWER`): both assert a governance decision
    this record's own layer says has not happened. This is deliberately narrow -- it
    is not a complete L0-L4 transition matrix, only the one contradiction this build
    can state with confidence -- so a `layer` outside :data:`GOVERNANCE_LAYER_CANDIDATE`,
    including any unrecognized open layer, is passed through untouched.

    A direct entry point: each argument is guarded as a string before it is compared or
    tested for frozen-set membership, so a wrongly typed (or unhashable) argument raises
    `ContractSemanticError` rather than a raw `TypeError`.
    """
    if _require_str(layer, "layer") != GOVERNANCE_LAYER_CANDIDATE:
        return
    _require_str(governance_state, "governance_state")
    _require_str(authority_level, "authority_level")
    if governance_state == GOVERNANCE_STATE_ACCEPTED:
        raise ContractSemanticError(
            f"layer {GOVERNANCE_LAYER_CANDIDATE!r} must not carry governance_state "
            f"{GOVERNANCE_STATE_ACCEPTED!r}"
        )
    if authority_level in AUTHORITY_LEVELS_REQUIRING_REVIEWER:
        raise ContractSemanticError(
            f"layer {GOVERNANCE_LAYER_CANDIDATE!r} must not carry decision-bearing "
            f"authority_level {authority_level!r}"
        )


def validate_record_provenance(provenance: object) -> None:
    """Raise unless `provenance` is internally coherent: identity shape, temporal
    ordering, evidence-disposition/source agreement, currentness/supersession
    coherence, source-list integrity, claim lineage, and history-entry validity all hold.

    Composes :func:`validate_record_temporal_metadata`,
    :func:`validate_evidence_disposition_sources`, and
    :func:`validate_record_currentness_consistency` over `provenance`'s own fields, and
    adds the checks a generic governed-record read needs that those helpers do not
    cover: `identity`'s own fields are shape-valid
    (:func:`_validate_record_identity`); `evidence_disposition` is itself a bounded,
    pattern-valid code; every declared `sources` entry is individually valid
    (:func:`_validate_source_reference`) and no two declare the same `(kind,
    source_id)` -- checked with a seen-set so the rejection does not depend on which
    of the two conflicting declarations comes first; and every `history` entry has a
    bounded, pattern-valid `actor_id`/`actor_kind`/`action` and a canonical
    `occurred_at`, with every piece of history evidence individually valid and the entry's
    evidence list within its bounded cardinality (:func:`_validate_evidence_list`). A
    history entry's optional `reason_code`/`reason_comment` -- present only on a
    governance-transition event, which copies them verbatim from the requesting
    `GovernanceRationale` -- are validated as a bounded, pattern-valid `OpenCode` and a
    bounded comment when present; requiring them on a governance transition is that
    operation's own result-validation concern, not this generic read's. An empty `history`
    is valid: no history event is invented here, and `history` carries no upper bound at
    all -- it is append-only and exactly one event is added per governance transition, so
    any finite inline cap would eventually make a previously valid record impossible to
    transition.

    Historical evidence is validated *intrinsically only*: a history event's evidence is
    never required to cite a source the record's *current* version declares. History is
    append-only and survives supersession, so an event written against an earlier version
    legitimately cites that version's sources -- sources a replacement claim drawing on
    wholly different evidence does not, and must not, redeclare. What binds a transition's
    own new event to the claim it justifies is exact equality against
    `request.replacement.assertion.evidence` in
    :mod:`~omnivia_core.contracts.v1.semantics_knowledge`, not a lookup against this list.

    `assertion`/`extraction` are structurally optional so a record written before those
    fields existed still decodes, but whenever either is present it is validated in full
    (:func:`_validate_assertion_lineage`, :func:`_validate_extraction_lineage`) against
    this same version's declared `sources` and `evidence_disposition` -- exactly the rules
    `memory.create` enforces on the input that supplied it. Without that, a governance
    transition preserving lineage unchanged would pass on lineage that is identically
    malformed on both sides, since a transition validator can only see that the two agree.

    A direct entry point: `provenance` and every nested value this reaches into are
    type-guarded, so a hand-built dataclass raises `ContractSemanticError` rather than a
    raw `TypeError`/`AttributeError`.
    """
    _require_type(provenance, RecordProvenance, "provenance")
    assert isinstance(provenance, RecordProvenance)
    _validate_record_identity(provenance.identity)
    validate_record_temporal_metadata(provenance.temporal)
    _validate_evidence_disposition_code(provenance.evidence_disposition, "evidence_disposition")
    sources = _validate_no_duplicate_sources(provenance.sources, "sources")
    validate_evidence_disposition_sources(provenance.evidence_disposition, sources)
    validate_record_currentness_consistency(provenance.identity, provenance.temporal)

    for index, source in enumerate(sources):
        _validate_source_reference(source, f"sources[{index}]")

    if provenance.assertion is not None:
        _validate_assertion_lineage(
            provenance.assertion, sources, provenance.evidence_disposition, "assertion"
        )
    if provenance.extraction is not None:
        _validate_extraction_lineage(provenance.extraction, "extraction")

    for history_index, entry in enumerate(_require_sequence(provenance.history, "history")):
        label = f"history[{history_index}]"
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
            _validate_evidence_list(entry.evidence, f"{label}.evidence")


def validate_governed_record(record: object) -> None:
    """Raise unless `record` is internally shape-valid, temporally, evidentially,
    provenance, currentness, and authority consistent.

    Validates `workspace_id`, `record_type`, `domain_scope`, and `authority_level` are
    each a bounded, pattern-valid value; composes :func:`validate_record_provenance`
    over `record.provenance` (identity shape, temporal ordering, evidence/source
    integrity, currentness/supersession coherence, and history/provenance agreement);
    and composes :func:`validate_governed_record_authority_coherence` and
    :func:`validate_governed_record_layer_coherence` over the resulting
    governance/authority/layer triple, so a caller decoding a `memory.get` /
    `memory.list` / `memory.search` result can enforce every invariant with one call.

    A direct entry point: `record` is type-guarded before any field is read.
    """
    _require_type(record, GovernedRecord, "record")
    assert isinstance(record, GovernedRecord)
    _validate_workspace_id(record.workspace_id, "workspace_id")
    _validate_governed_record_type(record.record_type, "record_type")
    validate_record_domain_scope(record.domain_scope)
    _validate_open_code(record.authority_level, "authority_level")
    validate_record_provenance(record.provenance)
    validate_governed_record_authority_coherence(
        record.provenance.identity.governance_state, record.authority_level, record.reviewer
    )
    validate_governed_record_layer_coherence(
        record.provenance.identity.layer,
        record.provenance.identity.governance_state,
        record.authority_level,
    )


# --- memory.create is proposed-only ------------------------------------------

MEMORY_CREATE_REQUIRED_GOVERNANCE_STATE: Final = "proposed"

MEMORY_CREATE_REQUIRED_CURRENTNESS: Final = "current"
"""The only currentness a freshly proposed `memory.create` result may claim.

A brand-new proposal cannot already be dead history, so this is an allowlist rather
than a blocklist of forbidden values: `RecordCurrentness` is an open vocabulary, and
an unrecognized value must never be read as fresh just because it is not one of the
known historical ones -- this build cannot verify what an unknown value means, so it
fails closed instead of bypassing the invariant.
"""

MEMORY_CREATE_RESERVED_INPUT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "workspace_id",
        "layer",
        "provenance",
        "identity",
        "history",
        "temporal",
        "ingested_at",
        "recorded_at",
        "record_id",
        "version",
        "governance_state",
        "currentness",
        "authority_level",
        "reviewer",
        "supersedes",
        "superseded_by",
        "superseded_at",
        "valid_from",
        "valid_until",
    }
)
"""Server-owned governance fields `memory.create` must never accept from a caller.

`valid_from`/`valid_until` are the server-final temporal names (`RecordTemporalMetadata`);
a caller may only *propose* a validity window through `assertion.proposed_valid_from` /
`assertion.proposed_valid_until`, which are not reserved and remain accepted.

`MemoryCreateInput`'s JSON Schema already excludes every one of these
(`unevaluatedProperties: false`), but the tolerant generated decoder production
actually calls ignores unknown fields by design -- so a raw payload smuggling one of
these names would decode successfully instead of failing loudly.
:func:`decode_memory_create_input` is the narrow production entry point that closes
that gap by inspecting the raw mapping before handing it to the tolerant decoder.
"""


def decode_memory_create_input(
    payload: object, path: str = "MemoryCreateInput"
) -> MemoryCreateInput:
    """Decode and fully validate `payload` into a semantically valid `MemoryCreateInput`.

    This is the narrow production entry point for `memory.create` input, and it is
    deliberately semantic, not merely structural: a caller must never be able to reach
    a handler with a candidate that merely parses. Three gates run in order, each
    closing a gap the next one down could not: (1) the raw mapping is inspected for
    any of the frozen :data:`MEMORY_CREATE_RESERVED_INPUT_FIELDS` server-owned
    governance names -- `MemoryCreateInput`'s JSON Schema already excludes every one of
    these (`unevaluatedProperties: false`), but the tolerant generated decoder
    production actually calls ignores unknown fields by design, so a raw payload
    smuggling one of these names would otherwise decode successfully instead of
    failing loudly; (2) the payload is structurally decoded with
    `MemoryCreateInput.from_wire`, which enforces required fields and wire types but
    nothing about domain scope shape, timestamp ordering, evidence/source coherence, or
    confidence bounds; (3) the decoded candidate is passed to
    :func:`validate_memory_create_input`, which enforces exactly those semantic
    invariants. Only a candidate that survives all three gates is returned. Genuinely
    unknown additive fields are still ignored, exactly as `MemoryCreateInput.from_wire`
    ignores them: only the frozen reserved names are rejected at gate (1), so a
    compatible minor release can still add unrelated optional input fields without
    this function needing to change.
    """
    if isinstance(payload, Mapping):
        present = sorted(MEMORY_CREATE_RESERVED_INPUT_FIELDS & set(payload))
        if present:
            raise ContractSemanticError(
                f"{path}: reserved server-owned field(s) not accepted on memory.create "
                f"input: {present}"
            )
    candidate = MemoryCreateInput.from_wire(payload, path)
    validate_memory_create_input(candidate)
    return candidate


MEMORY_CREATE_REQUIRED_LAYER: Final = "l1"
"""The only governance layer a fresh `memory.create` result may belong to.

`memory.create` proposes a candidate observation, never governed/canonical
knowledge (`l2`) or anything above it, so this is an allowlist rather than a
blocklist: `GovernanceLayer` is an open vocabulary, and an unrecognized value must
never be read as the candidate layer just because it is not one of the known
higher layers.
"""

MEMORY_CREATE_REQUIRED_AUTHORITY_LEVEL: Final = "proposed"
"""The only authority level a fresh `memory.create` result may carry.

Mirrors :data:`MEMORY_CREATE_REQUIRED_GOVERNANCE_STATE`: a brand-new candidate
cannot already carry `reviewed` or `canonical` authority, so this is an allowlist,
not a blocklist of forbidden values.
"""


def validate_memory_create_result(result: object, expected_workspace_id: object) -> None:
    """Raise unless `result` is exactly the proposed-only `memory.create` result tuple.

    `memory.create` never creates accepted canonical knowledge (frozen decision #3):
    the resulting record must pass full governed-record temporal/evidence/currentness/
    authority validation (:func:`validate_governed_record`), belong to the candidate
    layer (:data:`MEMORY_CREATE_REQUIRED_LAYER`), have `governance_state` exactly
    :data:`MEMORY_CREATE_REQUIRED_GOVERNANCE_STATE`, have `authority_level` exactly
    :data:`MEMORY_CREATE_REQUIRED_AUTHORITY_LEVEL`, have `currentness` exactly
    :data:`MEMORY_CREATE_REQUIRED_CURRENTNESS`, carry no `reviewer`, carry no
    `supersedes`/`superseded_by`/`superseded_at`, carry a non-empty `domain_scope`, and
    belong to `expected_workspace_id` -- the workspace this caller actually selected,
    not merely some workspace the record claims. `expected_workspace_id` is required
    and validated as a real, pattern-valid workspace id: an absent or malformed
    expectation can never be satisfied by any record, so it fails closed rather than
    skipping the workspace check.

    A direct entry point: `result` is type-guarded before any field is read.
    """
    _require_type(result, MemoryCreateResult, "result")
    assert isinstance(result, MemoryCreateResult)
    validate_governed_record(result.record)
    record = result.record
    identity = record.provenance.identity
    if identity.layer != MEMORY_CREATE_REQUIRED_LAYER:
        raise ContractSemanticError(
            f"memory.create result must have layer {MEMORY_CREATE_REQUIRED_LAYER!r}, "
            f"got {identity.layer!r}"
        )
    if identity.governance_state != MEMORY_CREATE_REQUIRED_GOVERNANCE_STATE:
        raise ContractSemanticError(
            "memory.create result must have governance_state "
            f"{MEMORY_CREATE_REQUIRED_GOVERNANCE_STATE!r}, got {identity.governance_state!r}"
        )
    if record.authority_level != MEMORY_CREATE_REQUIRED_AUTHORITY_LEVEL:
        raise ContractSemanticError(
            "memory.create result must have authority_level "
            f"{MEMORY_CREATE_REQUIRED_AUTHORITY_LEVEL!r}, got {record.authority_level!r}"
        )
    if identity.currentness != MEMORY_CREATE_REQUIRED_CURRENTNESS:
        raise ContractSemanticError(
            "memory.create result must have currentness "
            f"{MEMORY_CREATE_REQUIRED_CURRENTNESS!r}, got {identity.currentness!r}"
        )
    if record.reviewer is not None:
        raise ContractSemanticError("memory.create result must not already carry a reviewer")
    if identity.supersedes is not None:
        raise ContractSemanticError(
            "memory.create result must not already supersede another version"
        )
    if identity.superseded_by is not None:
        raise ContractSemanticError("memory.create result must not already be superseded")
    if record.provenance.temporal.superseded_at is not None:
        raise ContractSemanticError("memory.create result must not already carry superseded_at")
    if expected_workspace_id is None:
        raise ContractSemanticError(
            f"expected_workspace_id {expected_workspace_id!r} is not a valid WorkspaceId"
        )
    _validate_workspace_id(expected_workspace_id, "expected_workspace_id")
    if record.workspace_id != expected_workspace_id:
        raise ContractSemanticError(
            f"memory.create result workspace_id {record.workspace_id!r} does not match "
            f"the selected workspace {expected_workspace_id!r}"
        )


# --- memory.create candidate evidence/provenance coherence -------------------


_SOURCE_LIST_MAX_ITEMS: Final = 256
"""The `maxItems` both source arrays declare: `RecordProvenance.sources` and
`MemoryCreateInput.sources`.

Enforced here because the tolerant generated decoder ignores `maxItems` by design, so an
over-long list would otherwise decode and pass semantic validation while failing strict
JSON Schema -- and a Context Pack selecting such a record would have been validated against
a bar the schema does not agree with. Unlike `RecordProvenance.history`, which is
deliberately unbounded because it is append-only and a finite cap would eventually make a
previously valid record impossible to transition, `sources` is a *declaration set* for one
version rather than a growing log, so a ceiling on it takes nothing away."""


def _validate_no_duplicate_sources(sources: object, label: str) -> tuple[SourceReference, ...]:
    """Raise unless no two entries in `sources` declare the same `(kind, source_id)`,
    returning the list narrowed to a tuple of `SourceReference`.

    Checked with a seen-set rather than by building a `{key: source}` mapping, so the
    rejection is deterministic and does not depend on which of the two conflicting
    declarations happens to come first or last in the list: either list order
    (`[A, B]` or `[B, A]`) for the same conflicting pair raises the same way.

    This is also where the source list's own shape is guarded, since it is the first thing
    every caller runs over `sources`: the list must be a real sequence, every member a real
    `SourceReference`, and each one's `kind`/`source_id` a real string, so building the key
    below cannot raise a raw `AttributeError`/`TypeError` for a hand-built dataclass. The
    narrowed tuple it returns is what callers validate in full afterwards.

    The list's :data:`_SOURCE_LIST_MAX_ITEMS` cardinality is applied here for the same
    reason and in the same place, since both source arrays this function guards --
    `RecordProvenance.sources` and `MemoryCreateInput.sources` -- declare exactly that
    `maxItems`, and the tolerant decoder applies neither.
    """
    items = _require_sequence(sources, label)
    if len(items) > _SOURCE_LIST_MAX_ITEMS:
        raise ContractSemanticError(
            f"{label} has {len(items)} entries, exceeding the maximum of "
            f"{_SOURCE_LIST_MAX_ITEMS}"
        )
    seen: set[tuple[str, str]] = set()
    validated: list[SourceReference] = []
    for index, source in enumerate(items):
        source_label = f"{label}[{index}]"
        _require_type(source, SourceReference, source_label)
        assert isinstance(source, SourceReference)
        key = (
            _require_str(source.kind, f"{source_label}.kind"),
            _require_str(source.source_id, f"{source_label}.source_id"),
        )
        if key in seen:
            raise ContractSemanticError(
                f"{label}[{index}] duplicates an already-declared source {key!r}"
            )
        seen.add(key)
        validated.append(source)
    return tuple(validated)


def _validate_evidence_sources_declared(
    sources: Sequence[SourceReference], evidence: Sequence[EvidenceReference], context: str
) -> None:
    """Raise unless every `evidence` reference agrees with its declared source.

    Each evidence item must point at a source `sources` actually declares (same
    `(kind, source_id)`); a source key with no matching declared source is rejected
    outright. Beyond that key match, `locator`/`retrieved_at` are compared only when
    *both* sides carry a value: a declared source that omits one of these is never
    required to be repeated by the evidence, and compatible additive detail on the
    evidence side (present there, absent on the source) is never rejected as a
    conflict -- only two actually-present, actually-different values are. `context`
    labels the error for whichever caller this is.

    This binds a *claim's own* assertion evidence to the sources that claim declares. It is
    deliberately never applied to a governed record's history-entry evidence: history is
    append-only and survives supersession, so an event written against an earlier version
    may legitimately cite a source only that earlier version declared.
    """
    declared_by_key = {(source.kind, source.source_id): source for source in sources}
    for item in evidence:
        key = (item.source.kind, item.source.source_id)
        declared = declared_by_key.get(key)
        if declared is None:
            raise ContractSemanticError(
                f"{context} evidence source {key!r} is not among the declared sources"
            )
        if (
            item.source.locator is not None
            and declared.locator is not None
            and item.source.locator != declared.locator
        ):
            raise ContractSemanticError(
                f"{context} evidence source {key!r} locator {item.source.locator!r} "
                f"conflicts with declared source locator {declared.locator!r}"
            )
        if (
            item.source.retrieved_at is not None
            and declared.retrieved_at is not None
            and item.source.retrieved_at != declared.retrieved_at
        ):
            raise ContractSemanticError(
                f"{context} evidence source {key!r} retrieved_at {item.source.retrieved_at!r} "
                f"conflicts with declared source retrieved_at {declared.retrieved_at!r}"
            )


_SOURCE_LOCATOR_MAX_LENGTH: Final = 2048
"""`SourceReference.locator`'s schema `maxLength`, enforced here since structural
decoding never checks it."""

_SOURCE_SPAN_POINTER_MAX_LENGTH: Final = 2048
"""`SourceSpan.pointer`'s schema `maxLength`, enforced here since structural decoding
never checks it."""

_EVIDENCE_EXCERPT_MAX_LENGTH: Final = 4096
"""`EvidenceReference.excerpt`'s schema `maxLength`, enforced here since structural
decoding never checks it."""

_REASON_COMMENT_MAX_LENGTH: Final = 2048
"""`ProvenanceEntry.reason_comment`'s schema `maxLength`, enforced here since structural
decoding never checks it. The same bound `GovernanceRationale.comment` carries, since a
governance-transition history entry copies that comment over verbatim."""


_EVIDENCE_LIST_MAX_ITEMS: Final = 256
"""The single `maxItems` both evidence arrays declare: `CandidateAssertion.evidence` and
`ProvenanceEntry.evidence`.

The two must be the same number, not merely both bounded. A `record.supersede` transition
appends exactly one event whose evidence must equal the replacement's *complete* assertion
evidence, so a lower bound on the event side would make an otherwise valid replacement
impossible to record at all. Enforced here as well as in the schemas because the tolerant
generated decoder ignores `maxItems` by design, so an over-long list would otherwise decode
and pass semantic validation while failing strict schema validation.
"""


def _validate_source_reference(source: object, label: str) -> None:
    """Raise unless `source` has a valid `kind`/`source_id` shape, bounded `locator`,
    and a canonical `retrieved_at` timestamp when present.

    `kind` is `SourceKind`, an `OpenCode`-shaped open vocabulary, so an unrecognized
    but well-formed kind is accepted -- only its shape is checked. `source_id` is a
    bounded, pattern-valid `Identifier`. `locator` has no wire pattern, only a schema
    `maxLength` structural decoding never enforces, so only its length is bounded here.

    Direct-entry safe: `source` and each field read off it are type-guarded first.
    """
    _require_type(source, SourceReference, label)
    assert isinstance(source, SourceReference)
    _validate_open_code(source.kind, f"{label}.kind")
    _validate_identifier(source.source_id, f"{label}.source_id")
    if source.locator is not None:
        locator = _require_str(source.locator, f"{label}.locator")
        if len(locator) > _SOURCE_LOCATOR_MAX_LENGTH:
            raise ContractSemanticError(
                f"{label}.locator exceeds the maximum length of {_SOURCE_LOCATOR_MAX_LENGTH}"
            )
    if source.retrieved_at is not None:
        _parse_timestamp(source.retrieved_at, f"{label}.retrieved_at")


def _validate_source_span(span: object, label: str) -> None:
    """Raise unless `span` has a bounded `pointer` and a coherent, non-negative offset
    pair.

    `pointer` has no wire pattern, only a schema `maxLength` structural decoding never
    enforces. `start_offset`/`end_offset` each have a schema `minimum: 0` structural
    decoding never enforces either, so a negative offset is only caught here; and when
    both are present, `start_offset` must not be after `end_offset` -- a span cannot
    end before it starts.

    Direct-entry safe: `span` is type-guarded, `pointer` must be a real string, and each
    present offset must be a real non-`bool` integer before it is compared.
    """
    _require_type(span, SourceSpan, label)
    assert isinstance(span, SourceSpan)
    if len(_require_str(span.pointer, f"{label}.pointer")) > _SOURCE_SPAN_POINTER_MAX_LENGTH:
        raise ContractSemanticError(
            f"{label}.pointer exceeds the maximum length of {_SOURCE_SPAN_POINTER_MAX_LENGTH}"
        )
    start_offset = None
    end_offset = None
    if span.start_offset is not None:
        start_offset = _require_int(span.start_offset, f"{label}.start_offset")
        if start_offset < 0:
            raise ContractSemanticError(f"{label}.start_offset {start_offset!r} is negative")
    if span.end_offset is not None:
        end_offset = _require_int(span.end_offset, f"{label}.end_offset")
        if end_offset < 0:
            raise ContractSemanticError(f"{label}.end_offset {end_offset!r} is negative")
    if start_offset is not None and end_offset is not None and start_offset > end_offset:
        raise ContractSemanticError(
            f"{label}.start_offset {start_offset!r} is after {label}.end_offset {end_offset!r}"
        )


def _validate_evidence_reference(evidence: object, label: str) -> None:
    """Raise unless `evidence`'s source, optional span, and optional excerpt are all
    individually valid.

    `span` and `excerpt` are not made mandatory here: the canonical `EvidenceReference`
    schema deliberately declares both optional, since an exact `source` reference can
    itself be the available evidence link. When either is present, it must be
    internally coherent (:func:`_validate_source_span`) or within its bounded length.

    Direct-entry safe: `evidence` and each nested value are type-guarded first.
    """
    _require_type(evidence, EvidenceReference, label)
    assert isinstance(evidence, EvidenceReference)
    _validate_source_reference(evidence.source, f"{label}.source")
    if evidence.span is not None:
        _validate_source_span(evidence.span, f"{label}.span")
    if evidence.excerpt is not None:
        excerpt = _require_str(evidence.excerpt, f"{label}.excerpt")
        if len(excerpt) > _EVIDENCE_EXCERPT_MAX_LENGTH:
            raise ContractSemanticError(
                f"{label}.excerpt exceeds the maximum length of {_EVIDENCE_EXCERPT_MAX_LENGTH}"
            )


def _validate_evidence_list(evidence: object, label: str) -> tuple[EvidenceReference, ...]:
    """Raise unless `evidence` is a bounded sequence of individually valid
    `EvidenceReference`s, returning it narrowed to a tuple.

    Shared by the two places an evidence array appears -- a `CandidateAssertion`'s claim
    lineage and a `ProvenanceEntry`'s per-event evidence -- so both get the same
    intrinsic validation and the same :data:`_EVIDENCE_LIST_MAX_ITEMS` cardinality bound.
    Duplicate rejection is deliberately *not* here: two identical references are a
    contradiction in a claim's own lineage (:func:`_reject_duplicate_evidence_references`)
    but say nothing invalid about an append-only historical event.
    """
    items = _require_sequence(evidence, label)
    if len(items) > _EVIDENCE_LIST_MAX_ITEMS:
        raise ContractSemanticError(
            f"{label} has {len(items)} entries, exceeding the maximum of "
            f"{_EVIDENCE_LIST_MAX_ITEMS}"
        )
    validated: list[EvidenceReference] = []
    for index, item in enumerate(items):
        item_label = f"{label}[{index}]"
        _require_type(item, EvidenceReference, item_label)
        assert isinstance(item, EvidenceReference)
        _validate_evidence_reference(item, item_label)
        validated.append(item)
    return tuple(validated)


def _reject_duplicate_evidence_references(
    evidence: Sequence[EvidenceReference], label: str
) -> None:
    """Raise unless no two entries in `evidence` are the same complete `EvidenceReference`.

    Compared by full dataclass equality, not by source key: two references to the same
    source at different spans are two distinct pieces of evidence and are both kept, while
    the same span/excerpt listed twice is a duplicate. Compared against a list rather than a
    set so nothing here depends on `EvidenceReference` staying hashable.
    """
    seen: list[EvidenceReference] = []
    for index, item in enumerate(evidence):
        if item in seen:
            raise ContractSemanticError(
                f"{label}[{index}] duplicates an already-declared evidence reference"
            )
        seen.append(item)


def _validate_assertion_lineage(
    assertion: object,
    sources: Sequence[SourceReference],
    evidence_disposition: str,
    label: str,
) -> None:
    """Raise unless `assertion` is coherent claim lineage for a claim that declares exactly
    `sources` under `evidence_disposition`.

    The one rule set both `memory.create` input validation and generic governed-record
    provenance validation run, so the same assertion cannot be judged twice by two
    different standards. That sharing is the point: a governance-transition validator can
    only see that both versions preserved lineage *identically*, so unless the lineage
    itself is validated wherever it appears, an identically malformed assertion passes a
    transition unchallenged.

    In order: `evidence` is a bounded list of intrinsically valid references
    (:func:`_validate_evidence_list`); any disposition outside
    :data:`EVIDENCE_DISPOSITIONS_PERMITTING_EMPTY_SOURCES` -- not only the literal
    `available` value -- requires at least one concrete reference, so an unrecognized
    future disposition never silently excuses missing evidence; no two references are the
    same complete reference (:func:`_reject_duplicate_evidence_references`); every
    reference points at a source this same claim actually declares, with no contradictory
    `locator`/`retrieved_at` between the two (:func:`_validate_evidence_sources_declared`);
    `actor_id` is a bounded, pattern-valid `Identifier` and `actor_kind`/`actor_role` are
    bounded, pattern-valid `OpenCode`s; `asserted_at` is a canonical timestamp; and
    `proposed_valid_from`/`proposed_valid_until` are each validated independently when
    present, then compared for ordering only once both are.
    """
    _require_type(assertion, CandidateAssertion, label)
    assert isinstance(assertion, CandidateAssertion)
    evidence = _validate_evidence_list(assertion.evidence, f"{label}.evidence")
    if evidence_disposition not in EVIDENCE_DISPOSITIONS_PERMITTING_EMPTY_SOURCES and not evidence:
        raise ContractSemanticError(
            f"evidence_disposition {evidence_disposition!r} requires at least "
            "one concrete evidence reference on the assertion; only "
            f"{sorted(EVIDENCE_DISPOSITIONS_PERMITTING_EMPTY_SOURCES)!r} may have none"
        )
    _reject_duplicate_evidence_references(evidence, f"{label}.evidence")
    _validate_evidence_sources_declared(sources, evidence, label)

    _validate_identifier(assertion.actor_id, f"{label}.actor_id")
    _validate_open_code(assertion.actor_kind, f"{label}.actor_kind")
    _validate_open_code(assertion.actor_role, f"{label}.actor_role")
    _parse_timestamp(assertion.asserted_at, f"{label}.asserted_at")

    valid_from = None
    valid_until = None
    if assertion.proposed_valid_from is not None:
        valid_from = _parse_timestamp(
            assertion.proposed_valid_from, f"{label}.proposed_valid_from"
        )
    if assertion.proposed_valid_until is not None:
        valid_until = _parse_timestamp(
            assertion.proposed_valid_until, f"{label}.proposed_valid_until"
        )
    if valid_from is not None and valid_until is not None and valid_from > valid_until:
        raise ContractSemanticError(
            f"{label}.proposed_valid_from {assertion.proposed_valid_from!r} is after "
            f"{label}.proposed_valid_until {assertion.proposed_valid_until!r}"
        )


def _validate_extraction_lineage(extraction: object, label: str) -> None:
    """Raise unless `extraction` is coherent automated-extraction lineage.

    The companion to :func:`_validate_assertion_lineage`, shared by the same two callers
    for the same reason. Every identifier -- the required `extractor_id` and the optional
    `extractor_version`/`model_version`/`prompt_version` -- is a bounded, pattern-valid
    `Identifier`; `extracted_at` is a canonical timestamp; an optional
    `reconciliation_state` is a bounded, pattern-valid `OpenCode` whose unrecognized values
    are preserved untouched (there is nothing it could widen: neither `MemoryCreateInput`
    nor `RecordProvenance` carries an authority-level field derived from it); and an
    optional `confidence` must be a real number inside `[0, 1]`.
    """
    _require_type(extraction, CandidateExtractionMetadata, label)
    assert isinstance(extraction, CandidateExtractionMetadata)
    _validate_identifier(extraction.extractor_id, f"{label}.extractor_id")
    if extraction.extractor_version is not None:
        _validate_identifier(extraction.extractor_version, f"{label}.extractor_version")
    if extraction.model_version is not None:
        _validate_identifier(extraction.model_version, f"{label}.model_version")
    if extraction.prompt_version is not None:
        _validate_identifier(extraction.prompt_version, f"{label}.prompt_version")
    _parse_timestamp(extraction.extracted_at, f"{label}.extracted_at")
    if extraction.reconciliation_state is not None:
        _validate_open_code(extraction.reconciliation_state, f"{label}.reconciliation_state")
    if extraction.confidence is not None:
        confidence = _require_number(extraction.confidence, f"{label}.confidence")
        if not (0.0 <= confidence <= 1.0):
            raise ContractSemanticError(
                f"{label}.confidence {extraction.confidence!r} is outside [0, 1]"
            )


def validate_memory_create_input(candidate: object) -> None:
    """Raise unless `candidate` is an internally coherent `memory.create` proposal.

    Composes :func:`validate_evidence_disposition_sources` over the input's own
    `sources` and :func:`validate_record_domain_scope` over the input's `domain_scope`,
    then adds the candidate-specific invariants `memory.create` needs that a generic
    governed-record read never has to check: `record_type` and `evidence_disposition`
    are each a bounded, pattern-valid code (:func:`_validate_governed_record_type`,
    :func:`_validate_evidence_disposition_code`); no two declared `sources` repeat the
    same `(kind, source_id)` (:func:`_validate_no_duplicate_sources`); any evidence
    disposition outside :data:`EVIDENCE_DISPOSITIONS_PERMITTING_EMPTY_SOURCES` -- not
    only the literal `available` value -- requires at least one concrete evidence
    reference on the assertion (not just a declared source), so an unrecognized future
    disposition never silently excuses missing evidence; every piece of assertion
    evidence must point at a source this same input actually declares, with no
    contradictory `locator`/`retrieved_at` between the two
    (:func:`_validate_evidence_sources_declared`); every declared source and every
    piece of assertion evidence has a valid `kind`, `source_id`, bounded `locator`, and
    canonical `retrieved_at` (:func:`_validate_source_reference`), and every evidence
    `span`, when present, has a bounded, coherent, non-negative offset pair and an
    excerpt within its bounded length (:func:`_validate_source_span`); no two pieces of
    assertion evidence are the same complete reference, and the list stays within
    :data:`_EVIDENCE_LIST_MAX_ITEMS`;
    `assertion.actor_id` and the extraction's identifiers are bounded, pattern-valid
    `Identifier`s; `assertion.actor_kind`/`actor_role` and the extraction's
    `reconciliation_state` are bounded, pattern-valid `OpenCode`s; `assertion.asserted_at`,
    the optional top-level `event_at`/`observed_at`, and the extraction's `extracted_at`
    are all canonical timestamps; `assertion.proposed_valid_from`/`proposed_valid_until`
    are each validated independently when present, then compared for ordering only once
    both are; and an extraction's `confidence`, when present, must fall in `[0, 1]`.
    Unknown open `reconciliation_state` values are preserved untouched -- there is
    nothing here that could widen authority from an input field, since
    `MemoryCreateInput` carries no authority-level field at all.

    The assertion and extraction rules are not spelled out again here: they are
    :func:`_validate_assertion_lineage` and :func:`_validate_extraction_lineage`, the same
    two helpers :func:`validate_record_provenance` runs over the lineage this input is
    preserved as, so an input and the record it produces are held to one standard rather
    than two that can drift apart.

    A direct entry point: `candidate` and every nested value are type-guarded, so a
    hand-built dataclass raises `ContractSemanticError`, never a raw
    `TypeError`/`AttributeError`.
    """
    _require_type(candidate, MemoryCreateInput, "candidate")
    assert isinstance(candidate, MemoryCreateInput)
    _validate_governed_record_type(candidate.record_type, "record_type")
    _validate_evidence_disposition_code(candidate.evidence_disposition, "evidence_disposition")
    sources = _validate_no_duplicate_sources(candidate.sources, "sources")
    validate_evidence_disposition_sources(candidate.evidence_disposition, sources)

    for index, source in enumerate(sources):
        _validate_source_reference(source, f"sources[{index}]")

    _validate_assertion_lineage(
        candidate.assertion, sources, candidate.evidence_disposition, "assertion"
    )

    if candidate.event_at is not None:
        _parse_timestamp(candidate.event_at, "event_at")
    if candidate.observed_at is not None:
        _parse_timestamp(candidate.observed_at, "observed_at")

    validate_record_domain_scope(candidate.domain_scope)

    if candidate.extraction is not None:
        _validate_extraction_lineage(candidate.extraction, "extraction")


# --- bounded page limits ------------------------------------------------------

MIN_PAGE_LIMIT: Final = 1
MAX_PAGE_LIMIT: Final = 1000


def validate_page_limit(limit: int | None) -> None:
    """Raise unless `limit` is absent or within the bounded positive range.

    Structural decoding never enforces `PageLimit`'s `minimum`/`maximum` (that is
    exclusively the JSON Schema checker's job), so a caller decoding a tolerant
    production payload needs this to enforce the bound at runtime.
    """
    if limit is None:
        return
    if not (MIN_PAGE_LIMIT <= limit <= MAX_PAGE_LIMIT):
        raise ContractSemanticError(
            f"page limit {limit!r} is outside the bounded range "
            f"[{MIN_PAGE_LIMIT}, {MAX_PAGE_LIMIT}]"
        )


# --- workspace compatibility windows -----------------------------------------


def validate_workspace_compatibility(compatibility: WorkspaceCompatibility) -> None:
    """Raise unless `compatibility` is an internally consistent `WorkspaceCompatibility`.

    The `supported_workspace_versions` window must not be reversed, and
    `workspace_format_version` must actually fall inside it: a workspace
    descriptor claiming a concrete format version its own declared window
    excludes is asserting something that cannot be true.
    """
    validate_version_window(compatibility.supported_workspace_versions)
    if not version_in_window(
        compatibility.workspace_format_version, compatibility.supported_workspace_versions
    ):
        window = compatibility.supported_workspace_versions
        raise ContractSemanticError(
            f"workspace_format_version {compatibility.workspace_format_version!r} falls "
            f"outside supported_workspace_versions [{window.minimum!r}, {window.maximum!r}]"
        )


# --- installation vs. workspace scope agreement ------------------------------

SCOPE_KIND_INSTALLATION: Final = "installation"
SCOPE_KIND_WORKSPACE: Final = "workspace"


def validate_operation_scope_workspace_id(scope_kind: str, workspace_id: str | None) -> None:
    """Raise unless `workspace_id` agrees with `scope_kind`.

    A workspace-scoped operation (`scope_kind == "workspace"`, e.g.
    `workspace.inspect` or any `memory.*` operation) requires a real, pattern-valid
    selected `RequestMetadata.workspace_id`: absent, empty, whitespace-only, malformed,
    trailing-newline, and overlong (over 128 characters) values are all rejected, since
    structural decoding alone never enforces `WorkspaceId`'s pattern or `maxLength`. An
    installation-scoped operation (`scope_kind == "installation"`, e.g. `workspace.list`
    or `workspace.create`) must never carry one: accepting it would let a caller
    fabricate a workspace selection an installation-level operation has no use for and
    must not honour. A `scope_kind` outside this frozen pair fails closed rather than
    being silently accepted: this build has no workspace-id agreement rule for a scope
    kind it has never seen.
    """
    if scope_kind == SCOPE_KIND_WORKSPACE:
        if workspace_id is None:
            raise ContractSemanticError(
                "workspace-scoped operations require a non-empty, valid "
                f"RequestMetadata.workspace_id, got {workspace_id!r}"
            )
        _validate_workspace_id(workspace_id, "RequestMetadata.workspace_id")
        return
    if scope_kind == SCOPE_KIND_INSTALLATION:
        if workspace_id is not None:
            raise ContractSemanticError(
                "installation-scoped operations must not carry RequestMetadata.workspace_id"
            )
        return
    raise ContractSemanticError(f"unrecognized operation scope kind {scope_kind!r}")
