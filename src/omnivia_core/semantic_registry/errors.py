"""Stable error codes and exceptions for the Semantic Registry domain layer.

Every construction failure in this package raises a :class:`SemanticRegistryError`
carrying one of the frozen :class:`SemanticErrorCode` members, never a bare
``ValueError``/``TypeError``/``KeyError``. Codes are frozen strings so a caller
(or a test) can match on `error.code` instead of parsing English text.
"""

from __future__ import annotations

from enum import Enum


class SemanticErrorCode(str, Enum):
    """Frozen, stable reasons a Semantic Registry value is refused."""

    MISSING_FIELD = "missing_field"
    INVALID_FIELD = "invalid_field"
    UNSUPPORTED_VALUE = "unsupported_value"
    DUPLICATE_ID = "duplicate_id"
    UNKNOWN_REFERENCE = "unknown_reference"
    UNKNOWN_OPERATION_KIND = "unknown_operation_kind"
    CYCLIC_DEPENDENCY = "cyclic_dependency"
    STALE_BASE_DIGEST = "stale_base_digest"
    IMMUTABLE_VIOLATION = "immutable_violation"
    TEMPORAL_START_INDETERMINATE = "temporal_start_indeterminate"
    TEMPORAL_END_INDETERMINATE = "temporal_end_indeterminate"
    TEMPORAL_TIMEZONE_INDETERMINATE = "temporal_timezone_indeterminate"
    TEMPORAL_INTERVAL_INVALID = "temporal_interval_invalid"
    EVIDENCE_INTEGRITY_CONFLICT = "evidence_integrity_conflict"
    EVIDENCE_SOURCE_UNSUPPORTED = "evidence_source_unsupported"
    PERMISSION_DENIED = "permission_denied"
    CANDIDATE_STATE_CONFLICT = "candidate_state_conflict"
    CANDIDATE_SUPPRESSED = "candidate_suppressed"
    CROSS_WORKSPACE_ACCESS = "cross_workspace_access"


class SemanticRegistryError(ValueError):
    """Base error for every public failure raised by ``semantic_registry``."""

    def __init__(self, code: SemanticErrorCode, message: str) -> None:
        self.code = code
        super().__init__(f"[{code.value}] {message}")


class SemanticValidationError(SemanticRegistryError):
    """A value failed its own invariants at construction time."""


class SemanticConflictError(SemanticRegistryError):
    """An operation conflicts with authoritative state (stale base, cycle, ...)."""


class TemporalValidationError(SemanticValidationError):
    """A temporal field or interval fails its own invariants."""


class EvidenceValidationError(SemanticValidationError):
    """An evidence record fails its own invariants."""


class PermissionDeniedError(SemanticValidationError):
    """The caller is not authorised to perform the requested operation."""


class CandidateConflictError(SemanticConflictError):
    """A candidate operation conflicts with authoritative candidate state."""


def require(condition: object, code: SemanticErrorCode, message: str) -> None:
    """Raise :class:`SemanticValidationError` unless `condition` is truthy."""
    if not condition:
        raise SemanticValidationError(code, message)


__all__ = [
    "CandidateConflictError",
    "EvidenceValidationError",
    "PermissionDeniedError",
    "SemanticConflictError",
    "SemanticErrorCode",
    "SemanticRegistryError",
    "SemanticValidationError",
    "TemporalValidationError",
    "require",
]
