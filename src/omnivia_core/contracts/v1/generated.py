# GENERATED FILE - DO NOT EDIT.
#
# Source of truth:
#   contracts/application/v1/schemas/common.schema.json
#   contracts/application/v1/schemas/compatibility.schema.json
#   contracts/application/v1/schemas/errors.schema.json
#   contracts/application/v1/schemas/envelopes.schema.json
#   contracts/application/v1/schemas/service.schema.json
#   contracts/application/v1/schemas/records.schema.json
#   contracts/application/v1/schemas/jobs.schema.json
#   contracts/application/v1/schemas/operations.schema.json
#   contracts/application/v1/schemas/compatibility-matrix.schema.json
# Generator:
#   scripts/generate-application-contracts.py
#
# Regenerate: python scripts/generate-application-contracts.py
# Verify:     python scripts/generate-application-contracts.py --check
#
# Frozen dataclasses, type aliases, and frozen vocabulary for the OmniVia Core
# Application Contract v1. Standard library only: this module must never depend
# on runtime, storage, HTTP, MCP, CLI, Platform, Dev, or a validation framework.

"""Generated Application Contract v1 types (ADR-038).

Structural decoding lives here; conformance validation does not. `from_wire` ignores unknown fields
and preserves unknown open string values, which is the production posture. Strict rejection of
unknown fields is the job of the canonical JSON Schemas.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from types import MappingProxyType
from typing import Any, Final, TypeAlias, cast

__all__ = [
    "AUDIT_REFERENCE_PATTERN",
    "CAPABILITY_ID_PATTERN",
    "COMPATIBILITY_STATUSES",
    "COMPATIBILITY_STATUS_COMPATIBLE",
    "COMPATIBILITY_STATUS_COMPATIBLE_WITH_DEPRECATIONS",
    "COMPATIBILITY_STATUS_INCOMPATIBLE",
    "COMPATIBILITY_STATUS_UPGRADE_REQUIRED",
    "COMPONENT_KIND_PATTERN",
    "CONTRACT_VERSION",
    "CONTRACT_VERSION_PATTERN",
    "CORRELATION_ID_PATTERN",
    "DEFAULT_RETRY_CLASSIFICATION",
    "ERROR_CODE_AUTHENTICATION_REQUIRED",
    "ERROR_CODE_AUTHORIZATION_DENIED",
    "ERROR_CODE_BOOTSTRAP_IN_PROGRESS",
    "ERROR_CODE_CANCELLED",
    "ERROR_CODE_CAPABILITY_NOT_GRANTED",
    "ERROR_CODE_CONFLICT",
    "ERROR_CODE_DEADLINE_EXCEEDED",
    "ERROR_CODE_DEPENDENCY_UNAVAILABLE",
    "ERROR_CODE_IDEMPOTENCY_CONFLICT",
    "ERROR_CODE_INCOMPATIBLE_VERSION",
    "ERROR_CODE_INTERNAL_NON_RECOVERABLE",
    "ERROR_CODE_INTERNAL_RECOVERABLE",
    "ERROR_CODE_INVALID_PURPOSE",
    "ERROR_CODE_MUTATION_PRECONDITION_FAILED",
    "ERROR_CODE_NOT_FOUND",
    "ERROR_CODE_PATTERN",
    "ERROR_CODE_PROJECTION_UNAVAILABLE",
    "ERROR_CODE_RATE_LIMITED",
    "ERROR_CODE_SIZE_LIMIT_EXCEEDED",
    "ERROR_CODE_STALE_PROJECTION",
    "ERROR_CODE_TOKEN_LIMIT_EXCEEDED",
    "ERROR_CODE_UPGRADE_REQUIRED",
    "ERROR_CODE_WORKSPACE_BUSY",
    "ERROR_CODE_WORKSPACE_LEASE_UNAVAILABLE",
    "ERROR_CODE_WORKSPACE_MIGRATION_REQUIRED",
    "ERROR_CODE_WORKSPACE_NOT_GRANTED",
    "EVIDENCE_DISPOSITION_PATTERN",
    "FROZEN_ERROR_CODES",
    "FROZEN_RETRY_CLASSES",
    "GOVERNANCE_LAYER_PATTERN",
    "GOVERNANCE_STATE_PATTERN",
    "IDEMPOTENCY_KEY_PATTERN",
    "IDENTIFIER_PATTERN",
    "JOB_CANCELLATION_DISPOSITION_PATTERN",
    "JOB_PROGRESS_UNIT_PATTERN",
    "JOB_RESUME_DISPOSITION_PATTERN",
    "JOB_RETRY_DISPOSITION_PATTERN",
    "JOB_STATE_PATTERN",
    "OPAQUE_TOKEN_PATTERN",
    "OPEN_CODE_PATTERN",
    "OPERATION_COMPATIBILITY_STATE_PATTERN",
    "OPERATION_COMPLETION_MODE_PATTERN",
    "OPERATION_NAME_PATTERN",
    "OPERATION_SCOPE_KIND_PATTERN",
    "OPERATION_SIDE_EFFECT_PATTERN",
    "PROBE_KIND_PATTERN",
    "PROBE_STATUS_PATTERN",
    "PROJECTION_VERSION_PATTERN",
    "PURPOSE_PATTERN",
    "QUALIFICATION_STATE_PATTERN",
    "RECORD_CURRENTNESS_PATTERN",
    "RECORD_ID_PATTERN",
    "RECORD_VERSION_PATTERN",
    "RELEASE_VERSION_PATTERN",
    "REQUEST_ID_PATTERN",
    "RETRYABLE_RETRY_CLASSES",
    "RETRY_CLASS_NON_RETRYABLE",
    "RETRY_CLASS_PATTERN",
    "RETRY_CLASS_RETRYABLE",
    "RETRY_CLASS_RETRYABLE_AFTER_DELAY",
    "RETRY_CLASS_RETRYABLE_AFTER_PRECONDITION_REFRESH",
    "SCHEMA_BASE_URI",
    "SCOPE_PATTERN",
    "SOURCE_KIND_PATTERN",
    "TIMESTAMP_PATTERN",
    "TRACE_ID_PATTERN",
    "UPGRADE_STATES",
    "UPGRADE_STATE_IN_PROGRESS",
    "UPGRADE_STATE_NONE",
    "UPGRADE_STATE_OPTIONAL",
    "UPGRADE_STATE_REQUIRED",
    "WORKSPACE_ID_PATTERN",
    "ApiError",
    "AuditReference",
    "CapabilityCompatibilityEntry",
    "CapabilityId",
    "CapabilityRef",
    "CapabilityRequirement",
    "CapabilitySet",
    "ClientIdentity",
    "CompatibilityMatrix",
    "CompatibilityMetadata",
    "ComponentKind",
    "ContractDecodeError",
    "ContractVersion",
    "CorrelationId",
    "Deprecation",
    "DurationMs",
    "ErrorCode",
    "ErrorResponseEnvelope",
    "EvidenceDisposition",
    "EvidenceReference",
    "GovernanceLayer",
    "GovernanceState",
    "GrantedAuthority",
    "IdempotencyKey",
    "Identifier",
    "JobAttempt",
    "JobCancellationDisposition",
    "JobCancellationOutcome",
    "JobControl",
    "JobEvent",
    "JobHandle",
    "JobIdentity",
    "JobProgress",
    "JobProgressUnit",
    "JobReference",
    "JobResumeDisposition",
    "JobRetryDisposition",
    "JobState",
    "JobTerminalCancellation",
    "JobTerminalFailure",
    "JobTerminalResult",
    "JobTerminalSuccess",
    "JsonObject",
    "MutationPrecondition",
    "Omission",
    "OpaqueToken",
    "OpenCode",
    "OperationAuditMetadata",
    "OperationCompatibilityEntry",
    "OperationCompatibilityState",
    "OperationCompletionMode",
    "OperationIdempotencyMetadata",
    "OperationJobMetadata",
    "OperationMetadata",
    "OperationName",
    "OperationPaginationMetadata",
    "OperationPreconditionMetadata",
    "OperationScope",
    "OperationScopeKind",
    "OperationSideEffect",
    "PageMetadata",
    "PartialResult",
    "PrincipalClaim",
    "ProbeKind",
    "ProbeStatus",
    "ProjectionFreshness",
    "ProjectionVersion",
    "ProvenanceEntry",
    "Purpose",
    "QualificationState",
    "RecordCurrentness",
    "RecordId",
    "RecordIdentity",
    "RecordProvenance",
    "RecordTemporalMetadata",
    "RecordVersion",
    "ReleaseCompatibilityEntry",
    "ReleaseVersion",
    "RequestEnvelope",
    "RequestId",
    "RequestMetadata",
    "ResponseEnvelope",
    "ResponseMetadata",
    "RetryClass",
    "SchemaReference",
    "Scope",
    "ServiceComponentStatus",
    "ServiceProbeRequest",
    "ServiceProbeResult",
    "SourceKind",
    "SourceReference",
    "SourceSpan",
    "SuccessResponseEnvelope",
    "SupersessionReference",
    "Timestamp",
    "TraceId",
    "UpgradeState",
    "VersionCapabilityEnvelope",
    "VersionWindow",
    "Warning",
    "WorkspaceId",
    "job_terminal_result_from_wire",
    "job_terminal_result_to_wire",
    "response_envelope_from_wire",
    "response_envelope_to_wire",
]

class ContractDecodeError(ValueError):
    """Raised when a wire payload cannot be decoded into a contract value.

    Decoding is *tolerant about vocabulary and strict about structure*: unknown
    fields are ignored and unknown open string values are preserved, but a
    missing required field or a wrongly typed value is always an error.
    """


def _require_mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractDecodeError(f"{path}: expected an object, got {type(value).__name__}")
    for key in value:
        if not isinstance(key, str):
            raise ContractDecodeError(f"{path}: object keys must be strings")
    return cast(Mapping[str, Any], value)


def _require_field(mapping: Mapping[str, Any], key: str, path: str) -> object:
    if key not in mapping:
        raise ContractDecodeError(f"{path}: missing required field {key!r}")
    return mapping[key]


def _decode_str(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise ContractDecodeError(f"{path}: expected a string, got {type(value).__name__}")
    return value


def _decode_int(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractDecodeError(f"{path}: expected an integer, got {type(value).__name__}")
    return value


def _decode_bool(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise ContractDecodeError(f"{path}: expected a boolean, got {type(value).__name__}")
    return value


def _decode_sequence(value: object, path: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractDecodeError(f"{path}: expected an array, got {type(value).__name__}")
    return tuple(value)


def _decode_json_value(value: object, path: str) -> Any:
    """Recursively copy and validate an opaque JSON value.

    Objects become read-only mappings and arrays become tuples, so an opaque
    payload carried by a frozen dataclass cannot be mutated through the field.
    """
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ContractDecodeError(f"{path}: {value!r} is not representable in JSON")
        return value
    if isinstance(value, Mapping):
        mapping = _require_mapping(value, path)
        return MappingProxyType(
            {key: _decode_json_value(item, f"{path}.{key}") for key, item in mapping.items()}
        )
    if isinstance(value, Sequence):
        return tuple(
            _decode_json_value(item, f"{path}[{index}]") for index, item in enumerate(value)
        )
    raise ContractDecodeError(f"{path}: {type(value).__name__} is not a JSON value")


def _decode_json_object(value: object, path: str) -> Mapping[str, Any]:
    mapping = _require_mapping(value, path)
    return MappingProxyType(
        {key: _decode_json_value(item, f"{path}.{key}") for key, item in mapping.items()}
    )


def _encode_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _encode_json_value(item) for key, item in value.items()}
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return value
    return [_encode_json_value(item) for item in value]


def _encode_json_object(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _encode_json_value(item) for key, item in value.items()}


# --- contract identity -----------------------------------------------------

CONTRACT_VERSION: Final = "1.1"
SCHEMA_BASE_URI: Final = "https://contracts.omnivia.dev/application/v1/"

# --- frozen vocabulary -----------------------------------------------------
#
# ErrorCode and RetryClass are open patterned strings on the wire, so these constants are the
# frozen v1 vocabulary rather than a closed enumeration. A value outside them is valid and must be
# preserved, not coerced.

ERROR_CODE_AUTHENTICATION_REQUIRED: Final = "authentication_required"
ERROR_CODE_AUTHORIZATION_DENIED: Final = "authorization_denied"
ERROR_CODE_WORKSPACE_NOT_GRANTED: Final = "workspace_not_granted"
ERROR_CODE_CAPABILITY_NOT_GRANTED: Final = "capability_not_granted"
ERROR_CODE_INVALID_PURPOSE: Final = "invalid_purpose"
ERROR_CODE_NOT_FOUND: Final = "not_found"
ERROR_CODE_CONFLICT: Final = "conflict"
ERROR_CODE_MUTATION_PRECONDITION_FAILED: Final = "mutation_precondition_failed"
ERROR_CODE_IDEMPOTENCY_CONFLICT: Final = "idempotency_conflict"
ERROR_CODE_WORKSPACE_BUSY: Final = "workspace_busy"
ERROR_CODE_BOOTSTRAP_IN_PROGRESS: Final = "bootstrap_in_progress"
ERROR_CODE_WORKSPACE_LEASE_UNAVAILABLE: Final = "workspace_lease_unavailable"
ERROR_CODE_WORKSPACE_MIGRATION_REQUIRED: Final = "workspace_migration_required"
ERROR_CODE_INCOMPATIBLE_VERSION: Final = "incompatible_version"
ERROR_CODE_UPGRADE_REQUIRED: Final = "upgrade_required"
ERROR_CODE_PROJECTION_UNAVAILABLE: Final = "projection_unavailable"
ERROR_CODE_STALE_PROJECTION: Final = "stale_projection"
ERROR_CODE_RATE_LIMITED: Final = "rate_limited"
ERROR_CODE_SIZE_LIMIT_EXCEEDED: Final = "size_limit_exceeded"
ERROR_CODE_TOKEN_LIMIT_EXCEEDED: Final = "token_limit_exceeded"
ERROR_CODE_DEADLINE_EXCEEDED: Final = "deadline_exceeded"
ERROR_CODE_CANCELLED: Final = "cancelled"
ERROR_CODE_DEPENDENCY_UNAVAILABLE: Final = "dependency_unavailable"
ERROR_CODE_INTERNAL_RECOVERABLE: Final = "internal_recoverable"
ERROR_CODE_INTERNAL_NON_RECOVERABLE: Final = "internal_non_recoverable"

FROZEN_ERROR_CODES: Final[tuple[str, ...]] = (
    ERROR_CODE_AUTHENTICATION_REQUIRED,
    ERROR_CODE_AUTHORIZATION_DENIED,
    ERROR_CODE_WORKSPACE_NOT_GRANTED,
    ERROR_CODE_CAPABILITY_NOT_GRANTED,
    ERROR_CODE_INVALID_PURPOSE,
    ERROR_CODE_NOT_FOUND,
    ERROR_CODE_CONFLICT,
    ERROR_CODE_MUTATION_PRECONDITION_FAILED,
    ERROR_CODE_IDEMPOTENCY_CONFLICT,
    ERROR_CODE_WORKSPACE_BUSY,
    ERROR_CODE_BOOTSTRAP_IN_PROGRESS,
    ERROR_CODE_WORKSPACE_LEASE_UNAVAILABLE,
    ERROR_CODE_WORKSPACE_MIGRATION_REQUIRED,
    ERROR_CODE_INCOMPATIBLE_VERSION,
    ERROR_CODE_UPGRADE_REQUIRED,
    ERROR_CODE_PROJECTION_UNAVAILABLE,
    ERROR_CODE_STALE_PROJECTION,
    ERROR_CODE_RATE_LIMITED,
    ERROR_CODE_SIZE_LIMIT_EXCEEDED,
    ERROR_CODE_TOKEN_LIMIT_EXCEEDED,
    ERROR_CODE_DEADLINE_EXCEEDED,
    ERROR_CODE_CANCELLED,
    ERROR_CODE_DEPENDENCY_UNAVAILABLE,
    ERROR_CODE_INTERNAL_RECOVERABLE,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
)

RETRY_CLASS_NON_RETRYABLE: Final = "non_retryable"
"""Retrying the identical request cannot succeed. Surface the failure."""
RETRY_CLASS_RETRYABLE: Final = "retryable"
"""The identical request may succeed if retried; no minimum delay is implied."""
RETRY_CLASS_RETRYABLE_AFTER_DELAY: Final = "retryable_after_delay"
"""Retry only after backing off. Honour `retry_after_ms` when present."""
RETRY_CLASS_RETRYABLE_AFTER_PRECONDITION_REFRESH: Final = "retryable_after_precondition_refresh"
"""
The request is only safe to retry after re-reading the record and rebuilding
`mutation_precondition`. Never blind-retry.
"""

FROZEN_RETRY_CLASSES: Final[tuple[str, ...]] = (
    RETRY_CLASS_NON_RETRYABLE,
    RETRY_CLASS_RETRYABLE,
    RETRY_CLASS_RETRYABLE_AFTER_DELAY,
    RETRY_CLASS_RETRYABLE_AFTER_PRECONDITION_REFRESH,
)

RETRYABLE_RETRY_CLASSES: Final[frozenset[str]] = frozenset(
    {
        RETRY_CLASS_RETRYABLE,
        RETRY_CLASS_RETRYABLE_AFTER_DELAY,
    }
)
"""The only retry classes a caller may blind-retry.

Anything outside this set, including a class introduced by a newer peer, fails safe as non-
retryable.
"""

DEFAULT_RETRY_CLASSIFICATION: Final[Mapping[str, str]] = MappingProxyType(
    {
        ERROR_CODE_AUTHENTICATION_REQUIRED: RETRY_CLASS_NON_RETRYABLE,
        ERROR_CODE_AUTHORIZATION_DENIED: RETRY_CLASS_NON_RETRYABLE,
        ERROR_CODE_WORKSPACE_NOT_GRANTED: RETRY_CLASS_NON_RETRYABLE,
        ERROR_CODE_CAPABILITY_NOT_GRANTED: RETRY_CLASS_NON_RETRYABLE,
        ERROR_CODE_INVALID_PURPOSE: RETRY_CLASS_NON_RETRYABLE,
        ERROR_CODE_NOT_FOUND: RETRY_CLASS_NON_RETRYABLE,
        ERROR_CODE_CONFLICT: RETRY_CLASS_NON_RETRYABLE,
        ERROR_CODE_MUTATION_PRECONDITION_FAILED: RETRY_CLASS_RETRYABLE_AFTER_PRECONDITION_REFRESH,
        ERROR_CODE_IDEMPOTENCY_CONFLICT: RETRY_CLASS_NON_RETRYABLE,
        ERROR_CODE_WORKSPACE_BUSY: RETRY_CLASS_RETRYABLE_AFTER_DELAY,
        ERROR_CODE_BOOTSTRAP_IN_PROGRESS: RETRY_CLASS_RETRYABLE_AFTER_DELAY,
        ERROR_CODE_WORKSPACE_LEASE_UNAVAILABLE: RETRY_CLASS_RETRYABLE_AFTER_DELAY,
        ERROR_CODE_WORKSPACE_MIGRATION_REQUIRED: RETRY_CLASS_NON_RETRYABLE,
        ERROR_CODE_INCOMPATIBLE_VERSION: RETRY_CLASS_NON_RETRYABLE,
        ERROR_CODE_UPGRADE_REQUIRED: RETRY_CLASS_NON_RETRYABLE,
        ERROR_CODE_PROJECTION_UNAVAILABLE: RETRY_CLASS_RETRYABLE_AFTER_DELAY,
        ERROR_CODE_STALE_PROJECTION: RETRY_CLASS_RETRYABLE_AFTER_DELAY,
        ERROR_CODE_RATE_LIMITED: RETRY_CLASS_RETRYABLE_AFTER_DELAY,
        ERROR_CODE_SIZE_LIMIT_EXCEEDED: RETRY_CLASS_NON_RETRYABLE,
        ERROR_CODE_TOKEN_LIMIT_EXCEEDED: RETRY_CLASS_NON_RETRYABLE,
        ERROR_CODE_DEADLINE_EXCEEDED: RETRY_CLASS_RETRYABLE,
        ERROR_CODE_CANCELLED: RETRY_CLASS_NON_RETRYABLE,
        ERROR_CODE_DEPENDENCY_UNAVAILABLE: RETRY_CLASS_RETRYABLE_AFTER_DELAY,
        ERROR_CODE_INTERNAL_RECOVERABLE: RETRY_CLASS_RETRYABLE,
        ERROR_CODE_INTERNAL_NON_RECOVERABLE: RETRY_CLASS_NON_RETRYABLE,
    }
)
"""Frozen retry classification for every v1 error code."""

COMPATIBILITY_STATUSES: Final[tuple[str, ...]] = (
    "compatible",
    "compatible_with_deprecations",
    "upgrade_required",
    "incompatible",
)
UPGRADE_STATES: Final[tuple[str, ...]] = (
    "none",
    "optional",
    "required",
    "in_progress",
)

COMPATIBILITY_STATUS_COMPATIBLE: Final = "compatible"
COMPATIBILITY_STATUS_COMPATIBLE_WITH_DEPRECATIONS: Final = "compatible_with_deprecations"
COMPATIBILITY_STATUS_UPGRADE_REQUIRED: Final = "upgrade_required"
COMPATIBILITY_STATUS_INCOMPATIBLE: Final = "incompatible"

UPGRADE_STATE_NONE: Final = "none"
UPGRADE_STATE_OPTIONAL: Final = "optional"
UPGRADE_STATE_REQUIRED: Final = "required"
UPGRADE_STATE_IN_PROGRESS: Final = "in_progress"


# --- wire patterns ---------------------------------------------------------
#
# Published for callers that need to validate a value without a JSON Schema library. The generated
# decoders deliberately do not apply them: structural decoding and conformance validation are
# separate concerns.

CONTRACT_VERSION_PATTERN: Final = '^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)$'
RELEASE_VERSION_PATTERN: Final = (
    '^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)(?:-((?:0|[1-9][0'
    '-9]*|[0-9]*[a-zA-Z-][0-9a-zA-Z-]*)(?:\\.(?:0|[1-9][0-9]*|[0-9]*[a-zA'
    '-Z-][0-9a-zA-Z-]*))*))?(?:\\+([0-9a-zA-Z-]+(?:\\.[0-9a-zA-Z-]+)*))?$'
)
REQUEST_ID_PATTERN: Final = '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
CORRELATION_ID_PATTERN: Final = '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
TRACE_ID_PATTERN: Final = '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
WORKSPACE_ID_PATTERN: Final = '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
AUDIT_REFERENCE_PATTERN: Final = '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
IDENTIFIER_PATTERN: Final = '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
CAPABILITY_ID_PATTERN: Final = (
    '^[a-z][a-z0-9]*(?:[_-][a-z0-9]+)*(?:'
    '\\.[a-z][a-z0-9]*(?:[_-][a-z0-9]+)*)+$'
)
OPEN_CODE_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
SCOPE_PATTERN: Final = '^[a-z][a-z0-9_]*(?:[.:][a-z][a-z0-9_]*)*$'
PURPOSE_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
OPAQUE_TOKEN_PATTERN: Final = '^[!-~]+$'
IDEMPOTENCY_KEY_PATTERN: Final = '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
TIMESTAMP_PATTERN: Final = (
    '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:'
    '[0-9]{2}:[0-9]{2}(?:\\.[0-9]{1,9})?Z$'
)
PROJECTION_VERSION_PATTERN: Final = '^[!-~]+$'
OPERATION_COMPATIBILITY_STATE_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
QUALIFICATION_STATE_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
COMPONENT_KIND_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
OPERATION_NAME_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)+$'
ERROR_CODE_PATTERN: Final = '^[a-z][a-z0-9_]*$'
RETRY_CLASS_PATTERN: Final = '^[a-z][a-z0-9_]*$'
JOB_STATE_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
JOB_PROGRESS_UNIT_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
JOB_CANCELLATION_DISPOSITION_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
JOB_RETRY_DISPOSITION_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
JOB_RESUME_DISPOSITION_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
OPERATION_SIDE_EFFECT_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
OPERATION_SCOPE_KIND_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
OPERATION_COMPLETION_MODE_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
RECORD_ID_PATTERN: Final = '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
RECORD_VERSION_PATTERN: Final = '^[!-~]+$'
GOVERNANCE_LAYER_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
RECORD_CURRENTNESS_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
GOVERNANCE_STATE_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
SOURCE_KIND_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
EVIDENCE_DISPOSITION_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
PROBE_KIND_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
PROBE_STATUS_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'


# --- generated types -------------------------------------------------------

ContractVersion: TypeAlias = str
"""A `major.minor` contract version. Major changes are breaking; minor changes are additive and
forward compatible.
"""

ReleaseVersion: TypeAlias = str
"""A SemVer 2.0.0 release string identifying a concrete build, not a contract."""

RequestId: TypeAlias = str
"""Bounded, non-empty caller-assigned identifier for a single request attempt."""

CorrelationId: TypeAlias = str
"""Bounded, non-empty identifier grouping related requests into one logical operation."""

TraceId: TypeAlias = str
"""Bounded, non-empty distributed-trace identifier. Diagnostic only; never an authorization input.
"""

WorkspaceId: TypeAlias = str
"""Bounded, non-empty identifier of the workspace a request is scoped to."""

AuditReference: TypeAlias = str
"""Bounded, non-empty server-issued reference to the audit record for a completed operation."""

Identifier: TypeAlias = str
"""Generic bounded, non-empty identifier used for clients, principals, roles, and deprecations."""

CapabilityId: TypeAlias = str
"""Stable namespaced capability identifier such as `memory.read`. At least one dot is required so
capability names always carry a namespace.
"""

OpenCode: TypeAlias = str
"""An open, lowercase, dot-namespaced code. Unknown values are valid by design so that compatible
minor releases can add vocabulary; consumers must preserve values they do not recognize.
"""

Scope: TypeAlias = str
"""An open scope token such as `memory:read` requested by the caller. Scopes narrow a request; they
never widen granted authority.
"""

Purpose: TypeAlias = str
"""An open purpose-limitation token stating why the caller is making this request."""

OpaqueToken: TypeAlias = str
"""A bounded, server-issued opaque token. Clients must round-trip it verbatim and must never parse
it.
"""

IdempotencyKey: TypeAlias = str
"""Caller-assigned key making a mutation safe to retry. Equal keys with different inputs are an
`idempotency_conflict`.
"""

Timestamp: TypeAlias = str
"""An RFC 3339 timestamp in UTC with a literal `Z` offset."""

DurationMs: TypeAlias = int
"""A bounded non-negative duration in milliseconds."""

ProjectionVersion: TypeAlias = str
"""An opaque per-projection version marker used to reason about read staleness."""

JsonObject: TypeAlias = Mapping[str, Any]
"""An opaque JSON object. The application contract carries domain payloads without inspecting them;
per-operation payload schemas are out of scope for v1 foundations.
"""

OperationCompatibilityState: TypeAlias = str
"""Open, dot-namespaced code naming an operation's lifecycle state, such as `stable` or
`experimental` or `deprecated` or `removed`. Open by design so a compatible minor release can add
states without breaking existing decoders.
"""

QualificationState: TypeAlias = str
"""Open, dot-namespaced code naming how thoroughly a release or capability combination has actually
been verified, such as `development` or `unverified` or `qualified` or `supported`. Open by
design so a compatible minor release can add states without breaking existing decoders. A
combination absent this state, or carrying anything other than an explicitly verified state, must
never be treated as supported: an empty or `unverified` entry is not evidence of support.
"""

ComponentKind: TypeAlias = str
"""Open, dot-namespaced code naming which component a compatibility entry describes, such as `core`
or `runtime` or `cli` or `mcp` or `sdk`. Open by design so a compatible minor release can add
components without breaking existing decoders.
"""

OperationName: TypeAlias = str
"""Dot-namespaced operation identifier such as `memory.get`. The per-operation payload catalogue is
out of scope for v1 foundations; only the name is contractual here.
"""

ErrorCode: TypeAlias = str
"""Stable machine-readable failure code. OPEN by design: this is a patterned string, not an enum, so
compatible minor releases can add codes. Decoders must preserve unknown codes and must not map
them onto a known code.
"""

RetryClass: TypeAlias = str
"""How a caller may retry. OPEN by design, for the same reason as `ErrorCode`. An unrecognized retry
class MUST fail safe as non-retryable: never infer that an unknown class is retryable.
"""

JobState: TypeAlias = str
"""Open, dot-namespaced code naming where a job stands in its lifecycle, such as `queued` or
`running` or `succeeded` or `failed` or `cancelled`. Open by design so a compatible minor release
can add states without breaking existing decoders.
"""

JobProgressUnit: TypeAlias = str
"""Open, dot-namespaced code naming what `JobProgress.completed_units`/`total_units` count, such as
`item` or `byte` or `document`. Open by design so a compatible minor release can add units
without breaking existing decoders.
"""

JobCancellationDisposition: TypeAlias = str
"""Open, dot-namespaced code naming whether a job may be cancelled and where a requested
cancellation stands, such as `not_cancellable` or `cancellable` or `cancellation_requested` or
`cancelled`. Open by design; carries no scheduler, worker, lease, or persistence detail.
"""

JobRetryDisposition: TypeAlias = str
"""Open, dot-namespaced code naming whether a job may be retried and where a requested retry stands,
such as `not_retryable` or `retryable` or `retry_scheduled`. Open by design; carries no
scheduler, worker, lease, or persistence detail.
"""

JobResumeDisposition: TypeAlias = str
"""Open, dot-namespaced code naming whether a suspended or cancelled job may be resumed, such as
`not_resumable` or `resumable` or `resume_requested`. Open by design; carries no scheduler,
worker, lease, or persistence detail.
"""

OperationSideEffect: TypeAlias = str
"""Open, dot-namespaced code naming whether invoking an operation mutates state, such as `none` or
`create` or `update` or `delete`. Open by design so a compatible minor release can add
classifications without breaking existing decoders.
"""

OperationScopeKind: TypeAlias = str
"""Open, dot-namespaced code naming the kind of scope an operation carries, such as `installation`
or `workspace`. Open by design so a compatible minor release can add scope kinds without breaking
existing decoders. A given operation's scope metadata carries exactly one kind.
"""

OperationCompletionMode: TypeAlias = str
"""Open, dot-namespaced code naming how an operation completes, such as `synchronous` (no durable
job, ever), `may_return_job` (a response may carry a `JobReference`), or `always_returns_job`
(every invocation starts a durable job). Independent of `OperationSideEffect`: an operation like
`import.start` is representable as a mutation (`side_effect`) that always returns a durable job
(`completion_mode`). Open by design so a compatible minor release can add modes without breaking
existing decoders.
"""

@dataclass(frozen=True, slots=True)
class OperationPaginationMetadata:
    """Whether and how an operation's results are paginated."""

    paginated: bool
    max_page_size: int | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["paginated"] = self.paginated
        if self.max_page_size is not None:
            wire["max_page_size"] = self.max_page_size
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "OperationPaginationMetadata"
    ) -> OperationPaginationMetadata:
        """Decode a wire payload into a OperationPaginationMetadata.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_paginated = _decode_bool(
            _require_field(mapping, "paginated", path),
            f"{path}.paginated",
        )
        field_max_page_size: int | None = None
        if "max_page_size" in mapping:
            raw_max_page_size = mapping["max_page_size"]
            if raw_max_page_size is None:
                raise ContractDecodeError(
                    f"{path}.max_page_size: null is not a valid value"
                )
            field_max_page_size = _decode_int(raw_max_page_size, f"{path}.max_page_size")
        return cls(
            paginated=field_paginated,
            max_page_size=field_max_page_size,
        )


@dataclass(frozen=True, slots=True)
class OperationIdempotencyMetadata:
    """How this operation may safely be retried."""

    supports_idempotency_key: bool
    safe_to_retry: bool

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["supports_idempotency_key"] = self.supports_idempotency_key
        wire["safe_to_retry"] = self.safe_to_retry
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "OperationIdempotencyMetadata"
    ) -> OperationIdempotencyMetadata:
        """Decode a wire payload into a OperationIdempotencyMetadata.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_supports_idempotency_key = _decode_bool(
            _require_field(mapping, "supports_idempotency_key", path),
            f"{path}.supports_idempotency_key",
        )
        field_safe_to_retry = _decode_bool(
            _require_field(mapping, "safe_to_retry", path),
            f"{path}.safe_to_retry",
        )
        return cls(
            supports_idempotency_key=field_supports_idempotency_key,
            safe_to_retry=field_safe_to_retry,
        )


@dataclass(frozen=True, slots=True)
class OperationPreconditionMetadata:
    """How this operation uses optimistic-concurrency preconditions."""

    supports_mutation_precondition: bool
    required: bool

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["supports_mutation_precondition"] = self.supports_mutation_precondition
        wire["required"] = self.required
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "OperationPreconditionMetadata"
    ) -> OperationPreconditionMetadata:
        """Decode a wire payload into a OperationPreconditionMetadata.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_supports_mutation_precondition = _decode_bool(
            _require_field(mapping, "supports_mutation_precondition", path),
            f"{path}.supports_mutation_precondition",
        )
        field_required = _decode_bool(_require_field(mapping, "required", path), f"{path}.required")
        return cls(
            supports_mutation_precondition=field_supports_mutation_precondition,
            required=field_required,
        )


SchemaReference: TypeAlias = str
"""A URI reference to the JSON Schema document governing an operation's input or result payload."""

RecordId: TypeAlias = str
"""Stable identifier of a governed record, constant across every version of that record."""

RecordVersion: TypeAlias = str
"""Opaque, server-issued version marker of one specific revision of a record. Clients must round-
trip it verbatim and must never parse it.
"""

GovernanceLayer: TypeAlias = str
"""Open, dot-namespaced code naming the knowledge-governance layer a record belongs to: `l0` (raw
evidence), `l1` (candidate observations), `l2` (governed records / canonical knowledge), `l3`
(context models), or `l4` (organisational model). Distinct from workspace scope, which is a
caller-facing tenancy boundary, not a knowledge-governance layer. Open by design so a compatible
minor release can add layers without breaking existing decoders.
"""

RecordCurrentness: TypeAlias = str
"""Open, dot-namespaced code naming whether a record version is the active one, such as `current` or
`superseded` or `retracted`. Open by design; an unrecognized value must be preserved, not coerced
to a known one.
"""

GovernanceState: TypeAlias = str
"""Open, dot-namespaced code naming a record's position in its own governance workflow, such as
`proposed` or `candidate` or `accepted` or `rejected`. Distinct from `GovernanceLayer` (which
namespace a record belongs to) and `RecordCurrentness` (whether this version is the active one):
a record can be `accepted` and still later superseded, or `proposed` and never adopted. Open by
design so a compatible minor release can add states without breaking existing decoders.
"""

SourceKind: TypeAlias = str
"""Open, dot-namespaced code naming the kind of thing a source reference points at, such as
`document` or `conversation` or `api_response`.
"""

@dataclass(frozen=True, slots=True)
class SourceSpan:
    """An addressable position within a source: a pointer plus an optional character span, so
    evidence can be pinpointed within a source rather than only referencing the source as a
    whole.
    """

    pointer: str
    start_offset: int | None = None
    end_offset: int | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["pointer"] = self.pointer
        if self.start_offset is not None:
            wire["start_offset"] = self.start_offset
        if self.end_offset is not None:
            wire["end_offset"] = self.end_offset
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "SourceSpan") -> SourceSpan:
        """Decode a wire payload into a SourceSpan.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_pointer = _decode_str(_require_field(mapping, "pointer", path), f"{path}.pointer")
        field_start_offset: int | None = None
        if "start_offset" in mapping:
            raw_start_offset = mapping["start_offset"]
            if raw_start_offset is None:
                raise ContractDecodeError(
                    f"{path}.start_offset: null is not a valid value"
                )
            field_start_offset = _decode_int(raw_start_offset, f"{path}.start_offset")
        field_end_offset: int | None = None
        if "end_offset" in mapping:
            raw_end_offset = mapping["end_offset"]
            if raw_end_offset is None:
                raise ContractDecodeError(
                    f"{path}.end_offset: null is not a valid value"
                )
            field_end_offset = _decode_int(raw_end_offset, f"{path}.end_offset")
        return cls(
            pointer=field_pointer,
            start_offset=field_start_offset,
            end_offset=field_end_offset,
        )


EvidenceDisposition: TypeAlias = str
"""Open, dot-namespaced code stating whether concrete evidence is actually available for a record,
such as `available` or `unavailable` or `redacted`. Open by design; an unrecognized value must be
preserved, not coerced to a known one.
"""

ProbeKind: TypeAlias = str
"""Open, dot-namespaced code naming which runtime probe is being requested or answered. The frozen,
currently known probe kinds are exactly `service.health`, `service.readiness`, and
`service.discover`. Open by design so a compatible minor release can add probe kinds without
breaking existing callers.
"""

ProbeStatus: TypeAlias = str
"""Open, dot-namespaced code naming the outcome of a probe or one of its components, such as `pass`
or `warn` or `fail`. Open by design; an unrecognized status must be preserved and surfaced, not
coerced to a known one.
"""

@dataclass(frozen=True, slots=True)
class ClientIdentity:
    """Self-declared identity of the calling client. Diagnostic and compatibility input only;
    never an authorization input.
    """

    id: Identifier
    version: ReleaseVersion

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["id"] = self.id
        wire["version"] = self.version
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "ClientIdentity") -> ClientIdentity:
        """Decode a wire payload into a ClientIdentity.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_id = _decode_str(_require_field(mapping, "id", path), f"{path}.id")
        field_version = _decode_str(_require_field(mapping, "version", path), f"{path}.version")
        return cls(
            id=field_id,
            version=field_version,
        )


@dataclass(frozen=True, slots=True)
class PrincipalClaim:
    """UNTRUSTED. A claim made by the caller about who it is acting as. Authentication
    credentials stay transport-owned; a principal claim never becomes a GrantedAuthority
    without server-side validation.
    """

    claimed_principal_id: Identifier | None = None
    claimed_roles: tuple[Identifier, ...] | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        if self.claimed_principal_id is not None:
            wire["claimed_principal_id"] = self.claimed_principal_id
        if self.claimed_roles is not None:
            wire["claimed_roles"] = list(self.claimed_roles)
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "PrincipalClaim") -> PrincipalClaim:
        """Decode a wire payload into a PrincipalClaim.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_claimed_principal_id: Identifier | None = None
        if "claimed_principal_id" in mapping:
            raw_claimed_principal_id = mapping["claimed_principal_id"]
            if raw_claimed_principal_id is None:
                raise ContractDecodeError(
                    f"{path}.claimed_principal_id: null is not a valid value"
                )
            field_claimed_principal_id = _decode_str(
                raw_claimed_principal_id,
                f"{path}.claimed_principal_id",
            )
        field_claimed_roles: tuple[Identifier, ...] | None = None
        if "claimed_roles" in mapping:
            raw_claimed_roles = mapping["claimed_roles"]
            if raw_claimed_roles is None:
                raise ContractDecodeError(
                    f"{path}.claimed_roles: null is not a valid value"
                )
            field_claimed_roles_items = _decode_sequence(raw_claimed_roles, f"{path}.claimed_roles")
            field_claimed_roles = tuple(
                _decode_str(item, f"{path}.claimed_roles[{index}]")
                for index, item in enumerate(field_claimed_roles_items)
            )
        return cls(
            claimed_principal_id=field_claimed_principal_id,
            claimed_roles=field_claimed_roles,
        )


@dataclass(frozen=True, slots=True)
class MutationPrecondition:
    """Optimistic-concurrency precondition for a mutation. A mismatch is a
    `mutation_precondition_failed` error.
    """

    record_version: OpaqueToken

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["record_version"] = self.record_version
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "MutationPrecondition") -> MutationPrecondition:
        """Decode a wire payload into a MutationPrecondition.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_record_version = _decode_str(
            _require_field(mapping, "record_version", path),
            f"{path}.record_version",
        )
        return cls(
            record_version=field_record_version,
        )


@dataclass(frozen=True, slots=True)
class Warning:
    """A non-fatal advisory attached to a successful or failed response."""

    code: OpenCode
    message: str
    details: JsonObject | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["code"] = self.code
        wire["message"] = self.message
        if self.details is not None:
            wire["details"] = _encode_json_object(self.details)
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "Warning") -> Warning:
        """Decode a wire payload into a Warning.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_code = _decode_str(_require_field(mapping, "code", path), f"{path}.code")
        field_message = _decode_str(_require_field(mapping, "message", path), f"{path}.message")
        field_details: JsonObject | None = None
        if "details" in mapping:
            raw_details = mapping["details"]
            if raw_details is None:
                raise ContractDecodeError(
                    f"{path}.details: null is not a valid value"
                )
            field_details = _decode_json_object(raw_details, f"{path}.details")
        return cls(
            code=field_code,
            message=field_message,
            details=field_details,
        )


@dataclass(frozen=True, slots=True)
class Omission:
    """A statement that something the caller asked for was deliberately not returned."""

    code: OpenCode
    path: str | None = None
    message: str | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["code"] = self.code
        if self.path is not None:
            wire["path"] = self.path
        if self.message is not None:
            wire["message"] = self.message
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "Omission") -> Omission:
        """Decode a wire payload into a Omission.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_code = _decode_str(_require_field(mapping, "code", path), f"{path}.code")
        field_path: str | None = None
        if "path" in mapping:
            raw_path = mapping["path"]
            if raw_path is None:
                raise ContractDecodeError(
                    f"{path}.path: null is not a valid value"
                )
            field_path = _decode_str(raw_path, f"{path}.path")
        field_message: str | None = None
        if "message" in mapping:
            raw_message = mapping["message"]
            if raw_message is None:
                raise ContractDecodeError(
                    f"{path}.message: null is not a valid value"
                )
            field_message = _decode_str(raw_message, f"{path}.message")
        return cls(
            code=field_code,
            path=field_path,
            message=field_message,
        )


@dataclass(frozen=True, slots=True)
class PartialResult:
    """Marks a result as incomplete. A partial result is still a success; callers must not treat
    it as a full answer.
    """

    is_partial: bool
    reasons: tuple[OpenCode, ...]

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["is_partial"] = self.is_partial
        wire["reasons"] = list(self.reasons)
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "PartialResult") -> PartialResult:
        """Decode a wire payload into a PartialResult.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_is_partial = _decode_bool(
            _require_field(mapping, "is_partial", path),
            f"{path}.is_partial",
        )
        field_reasons_items = _decode_sequence(
            _require_field(mapping, "reasons", path),
            f"{path}.reasons",
        )
        field_reasons = tuple(
            _decode_str(item, f"{path}.reasons[{index}]")
            for index, item in enumerate(field_reasons_items)
        )
        return cls(
            is_partial=field_is_partial,
            reasons=field_reasons,
        )


@dataclass(frozen=True, slots=True)
class ProjectionFreshness:
    """Staleness statement for reads served from a projection rather than the write model."""

    as_of: Timestamp
    projection_versions: Mapping[str, ProjectionVersion]
    stale: bool

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["as_of"] = self.as_of
        wire["projection_versions"] = dict(self.projection_versions)
        wire["stale"] = self.stale
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "ProjectionFreshness") -> ProjectionFreshness:
        """Decode a wire payload into a ProjectionFreshness.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_as_of = _decode_str(_require_field(mapping, "as_of", path), f"{path}.as_of")
        field_projection_versions_entries = _require_mapping(
            _require_field(mapping, "projection_versions", path),
            f"{path}.projection_versions",
        )
        field_projection_versions = MappingProxyType(
            {
                key: _decode_str(value, f"{path}.projection_versions.{key}")
                for key, value in field_projection_versions_entries.items()
            }
        )
        field_stale = _decode_bool(_require_field(mapping, "stale", path), f"{path}.stale")
        return cls(
            as_of=field_as_of,
            projection_versions=field_projection_versions,
            stale=field_stale,
        )


@dataclass(frozen=True, slots=True)
class PageMetadata:
    """Pagination position. Token issuance semantics are deliberately out of scope for v1
    foundations.
    """

    continuation_token: OpaqueToken | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        if self.continuation_token is not None:
            wire["continuation_token"] = self.continuation_token
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "PageMetadata") -> PageMetadata:
        """Decode a wire payload into a PageMetadata.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_continuation_token: OpaqueToken | None = None
        if "continuation_token" in mapping:
            raw_continuation_token = mapping["continuation_token"]
            if raw_continuation_token is None:
                raise ContractDecodeError(
                    f"{path}.continuation_token: null is not a valid value"
                )
            field_continuation_token = _decode_str(
                raw_continuation_token,
                f"{path}.continuation_token",
            )
        return cls(
            continuation_token=field_continuation_token,
        )


@dataclass(frozen=True, slots=True)
class JobReference:
    """Reference to asynchronous work started by an operation. Job lifecycle, polling, and
    cancellation are later phases; v1 carries the identifier only.
    """

    job_id: OpaqueToken

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["job_id"] = self.job_id
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "JobReference") -> JobReference:
        """Decode a wire payload into a JobReference.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_job_id = _decode_str(_require_field(mapping, "job_id", path), f"{path}.job_id")
        return cls(
            job_id=field_job_id,
        )


@dataclass(frozen=True, slots=True)
class VersionWindow:
    """An inclusive range of contract versions a peer supports."""

    minimum: ContractVersion
    maximum: ContractVersion

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["minimum"] = self.minimum
        wire["maximum"] = self.maximum
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "VersionWindow") -> VersionWindow:
        """Decode a wire payload into a VersionWindow.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_minimum = _decode_str(_require_field(mapping, "minimum", path), f"{path}.minimum")
        field_maximum = _decode_str(_require_field(mapping, "maximum", path), f"{path}.maximum")
        return cls(
            minimum=field_minimum,
            maximum=field_maximum,
        )


@dataclass(frozen=True, slots=True)
class Deprecation:
    """A stable, citable notice that something in the contract is going away."""

    id: Identifier
    since: ContractVersion
    removal: ContractVersion | None = None
    replacement: str | None = None
    message: str | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["id"] = self.id
        wire["since"] = self.since
        if self.removal is not None:
            wire["removal"] = self.removal
        if self.replacement is not None:
            wire["replacement"] = self.replacement
        if self.message is not None:
            wire["message"] = self.message
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "Deprecation") -> Deprecation:
        """Decode a wire payload into a Deprecation.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_id = _decode_str(_require_field(mapping, "id", path), f"{path}.id")
        field_since = _decode_str(_require_field(mapping, "since", path), f"{path}.since")
        field_removal: ContractVersion | None = None
        if "removal" in mapping:
            raw_removal = mapping["removal"]
            if raw_removal is None:
                raise ContractDecodeError(
                    f"{path}.removal: null is not a valid value"
                )
            field_removal = _decode_str(raw_removal, f"{path}.removal")
        field_replacement: str | None = None
        if "replacement" in mapping:
            raw_replacement = mapping["replacement"]
            if raw_replacement is None:
                raise ContractDecodeError(
                    f"{path}.replacement: null is not a valid value"
                )
            field_replacement = _decode_str(raw_replacement, f"{path}.replacement")
        field_message: str | None = None
        if "message" in mapping:
            raw_message = mapping["message"]
            if raw_message is None:
                raise ContractDecodeError(
                    f"{path}.message: null is not a valid value"
                )
            field_message = _decode_str(raw_message, f"{path}.message")
        return cls(
            id=field_id,
            since=field_since,
            removal=field_removal,
            replacement=field_replacement,
            message=field_message,
        )


@dataclass(frozen=True, slots=True)
class UpgradeState:
    """Where the peer stands relative to a required or offered upgrade."""

    value: OpenCode
    target_version: ContractVersion | None = None
    reason: str | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["value"] = self.value
        if self.target_version is not None:
            wire["target_version"] = self.target_version
        if self.reason is not None:
            wire["reason"] = self.reason
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "UpgradeState") -> UpgradeState:
        """Decode a wire payload into a UpgradeState.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_value = _decode_str(_require_field(mapping, "value", path), f"{path}.value")
        field_target_version: ContractVersion | None = None
        if "target_version" in mapping:
            raw_target_version = mapping["target_version"]
            if raw_target_version is None:
                raise ContractDecodeError(
                    f"{path}.target_version: null is not a valid value"
                )
            field_target_version = _decode_str(raw_target_version, f"{path}.target_version")
        field_reason: str | None = None
        if "reason" in mapping:
            raw_reason = mapping["reason"]
            if raw_reason is None:
                raise ContractDecodeError(
                    f"{path}.reason: null is not a valid value"
                )
            field_reason = _decode_str(raw_reason, f"{path}.reason")
        return cls(
            value=field_value,
            target_version=field_target_version,
            reason=field_reason,
        )


@dataclass(frozen=True, slots=True)
class CapabilityRef:
    """A concrete capability at a concrete version."""

    id: CapabilityId
    version: ContractVersion

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["id"] = self.id
        wire["version"] = self.version
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "CapabilityRef") -> CapabilityRef:
        """Decode a wire payload into a CapabilityRef.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_id = _decode_str(_require_field(mapping, "id", path), f"{path}.id")
        field_version = _decode_str(_require_field(mapping, "version", path), f"{path}.version")
        return cls(
            id=field_id,
            version=field_version,
        )


@dataclass(frozen=True, slots=True)
class CapabilityRequirement:
    """A capability the caller needs, at or above a minimum version."""

    id: CapabilityId
    minimum_version: ContractVersion
    required: bool

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["id"] = self.id
        wire["minimum_version"] = self.minimum_version
        wire["required"] = self.required
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "CapabilityRequirement"
    ) -> CapabilityRequirement:
        """Decode a wire payload into a CapabilityRequirement.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_id = _decode_str(_require_field(mapping, "id", path), f"{path}.id")
        field_minimum_version = _decode_str(
            _require_field(mapping, "minimum_version", path),
            f"{path}.minimum_version",
        )
        field_required = _decode_bool(_require_field(mapping, "required", path), f"{path}.required")
        return cls(
            id=field_id,
            minimum_version=field_minimum_version,
            required=field_required,
        )


@dataclass(frozen=True, slots=True)
class ApiError:
    """A single typed failure. The code and retry class are the contract; the message is not."""

    code: ErrorCode
    message: str
    retry_class: RetryClass
    retry_after_ms: DurationMs | None = None
    details: JsonObject | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["code"] = self.code
        wire["message"] = self.message
        wire["retry_class"] = self.retry_class
        if self.retry_after_ms is not None:
            wire["retry_after_ms"] = self.retry_after_ms
        if self.details is not None:
            wire["details"] = _encode_json_object(self.details)
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "ApiError") -> ApiError:
        """Decode a wire payload into a ApiError.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_code = _decode_str(_require_field(mapping, "code", path), f"{path}.code")
        field_message = _decode_str(_require_field(mapping, "message", path), f"{path}.message")
        field_retry_class = _decode_str(
            _require_field(mapping, "retry_class", path),
            f"{path}.retry_class",
        )
        field_retry_after_ms: DurationMs | None = None
        if "retry_after_ms" in mapping:
            raw_retry_after_ms = mapping["retry_after_ms"]
            if raw_retry_after_ms is None:
                raise ContractDecodeError(
                    f"{path}.retry_after_ms: null is not a valid value"
                )
            field_retry_after_ms = _decode_int(raw_retry_after_ms, f"{path}.retry_after_ms")
        field_details: JsonObject | None = None
        if "details" in mapping:
            raw_details = mapping["details"]
            if raw_details is None:
                raise ContractDecodeError(
                    f"{path}.details: null is not a valid value"
                )
            field_details = _decode_json_object(raw_details, f"{path}.details")
        return cls(
            code=field_code,
            message=field_message,
            retry_class=field_retry_class,
            retry_after_ms=field_retry_after_ms,
            details=field_details,
        )


@dataclass(frozen=True, slots=True)
class JobIdentity:
    """The identity of one asynchronous job: what it is, which application operation started it,
    its immutable audit linkage, and, when applicable, which workspace it runs against.
    """

    job_id: OpaqueToken
    job_kind: OpenCode
    originating_operation: OperationName
    audit_reference: AuditReference
    workspace_id: WorkspaceId | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["job_id"] = self.job_id
        wire["job_kind"] = self.job_kind
        wire["originating_operation"] = self.originating_operation
        wire["audit_reference"] = self.audit_reference
        if self.workspace_id is not None:
            wire["workspace_id"] = self.workspace_id
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "JobIdentity") -> JobIdentity:
        """Decode a wire payload into a JobIdentity.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_job_id = _decode_str(_require_field(mapping, "job_id", path), f"{path}.job_id")
        field_job_kind = _decode_str(_require_field(mapping, "job_kind", path), f"{path}.job_kind")
        field_originating_operation = _decode_str(
            _require_field(mapping, "originating_operation", path),
            f"{path}.originating_operation",
        )
        field_audit_reference = _decode_str(
            _require_field(mapping, "audit_reference", path),
            f"{path}.audit_reference",
        )
        field_workspace_id: WorkspaceId | None = None
        if "workspace_id" in mapping:
            raw_workspace_id = mapping["workspace_id"]
            if raw_workspace_id is None:
                raise ContractDecodeError(
                    f"{path}.workspace_id: null is not a valid value"
                )
            field_workspace_id = _decode_str(raw_workspace_id, f"{path}.workspace_id")
        return cls(
            job_id=field_job_id,
            job_kind=field_job_kind,
            originating_operation=field_originating_operation,
            audit_reference=field_audit_reference,
            workspace_id=field_workspace_id,
        )


@dataclass(frozen=True, slots=True)
class JobProgress:
    """A point-in-time progress statement for a running job."""

    unit: JobProgressUnit
    completed_units: int
    total_units: int | None = None
    message: str | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["unit"] = self.unit
        wire["completed_units"] = self.completed_units
        if self.total_units is not None:
            wire["total_units"] = self.total_units
        if self.message is not None:
            wire["message"] = self.message
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "JobProgress") -> JobProgress:
        """Decode a wire payload into a JobProgress.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_unit = _decode_str(_require_field(mapping, "unit", path), f"{path}.unit")
        field_completed_units = _decode_int(
            _require_field(mapping, "completed_units", path),
            f"{path}.completed_units",
        )
        field_total_units: int | None = None
        if "total_units" in mapping:
            raw_total_units = mapping["total_units"]
            if raw_total_units is None:
                raise ContractDecodeError(
                    f"{path}.total_units: null is not a valid value"
                )
            field_total_units = _decode_int(raw_total_units, f"{path}.total_units")
        field_message: str | None = None
        if "message" in mapping:
            raw_message = mapping["message"]
            if raw_message is None:
                raise ContractDecodeError(
                    f"{path}.message: null is not a valid value"
                )
            field_message = _decode_str(raw_message, f"{path}.message")
        return cls(
            unit=field_unit,
            completed_units=field_completed_units,
            total_units=field_total_units,
            message=field_message,
        )


@dataclass(frozen=True, slots=True)
class JobControl:
    """The control actions a caller may take on a job: cancellation, retry, and resume.
    Deliberately exposes only these caller-facing dispositions, never scheduler, worker,
    lease, or persistence detail.
    """

    cancellation: JobCancellationDisposition
    retry: JobRetryDisposition
    resume: JobResumeDisposition

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["cancellation"] = self.cancellation
        wire["retry"] = self.retry
        wire["resume"] = self.resume
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "JobControl") -> JobControl:
        """Decode a wire payload into a JobControl.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_cancellation = _decode_str(
            _require_field(mapping, "cancellation", path),
            f"{path}.cancellation",
        )
        field_retry = _decode_str(_require_field(mapping, "retry", path), f"{path}.retry")
        field_resume = _decode_str(_require_field(mapping, "resume", path), f"{path}.resume")
        return cls(
            cancellation=field_cancellation,
            retry=field_retry,
            resume=field_resume,
        )


@dataclass(frozen=True, slots=True)
class JobEvent:
    """One entry in a job's ordered event stream."""

    sequence: int
    occurred_at: Timestamp
    state: JobState
    message: str | None = None
    details: JsonObject | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["sequence"] = self.sequence
        wire["occurred_at"] = self.occurred_at
        wire["state"] = self.state
        if self.message is not None:
            wire["message"] = self.message
        if self.details is not None:
            wire["details"] = _encode_json_object(self.details)
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "JobEvent") -> JobEvent:
        """Decode a wire payload into a JobEvent.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_sequence = _decode_int(_require_field(mapping, "sequence", path), f"{path}.sequence")
        field_occurred_at = _decode_str(
            _require_field(mapping, "occurred_at", path),
            f"{path}.occurred_at",
        )
        field_state = _decode_str(_require_field(mapping, "state", path), f"{path}.state")
        field_message: str | None = None
        if "message" in mapping:
            raw_message = mapping["message"]
            if raw_message is None:
                raise ContractDecodeError(
                    f"{path}.message: null is not a valid value"
                )
            field_message = _decode_str(raw_message, f"{path}.message")
        field_details: JsonObject | None = None
        if "details" in mapping:
            raw_details = mapping["details"]
            if raw_details is None:
                raise ContractDecodeError(
                    f"{path}.details: null is not a valid value"
                )
            field_details = _decode_json_object(raw_details, f"{path}.details")
        return cls(
            sequence=field_sequence,
            occurred_at=field_occurred_at,
            state=field_state,
            message=field_message,
            details=field_details,
        )


@dataclass(frozen=True, slots=True)
class JobCancellationOutcome:
    """The explicit outcome recorded when a job's terminal state is cancellation, distinguishing
    it from an ordinary success or failure.
    """

    reason: OpenCode

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["reason"] = self.reason
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "JobCancellationOutcome"
    ) -> JobCancellationOutcome:
        """Decode a wire payload into a JobCancellationOutcome.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_reason = _decode_str(_require_field(mapping, "reason", path), f"{path}.reason")
        return cls(
            reason=field_reason,
        )


@dataclass(frozen=True, slots=True)
class OperationScope:
    """What an operation itself declares it needs and touches, independent of any single
    caller's request.
    """

    required_scopes: tuple[Scope, ...]
    side_effect: OperationSideEffect
    scope_kind: OperationScopeKind

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["required_scopes"] = list(self.required_scopes)
        wire["side_effect"] = self.side_effect
        wire["scope_kind"] = self.scope_kind
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "OperationScope") -> OperationScope:
        """Decode a wire payload into a OperationScope.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_required_scopes_items = _decode_sequence(
            _require_field(mapping, "required_scopes", path),
            f"{path}.required_scopes",
        )
        field_required_scopes = tuple(
            _decode_str(item, f"{path}.required_scopes[{index}]")
            for index, item in enumerate(field_required_scopes_items)
        )
        field_side_effect = _decode_str(
            _require_field(mapping, "side_effect", path),
            f"{path}.side_effect",
        )
        field_scope_kind = _decode_str(
            _require_field(mapping, "scope_kind", path),
            f"{path}.scope_kind",
        )
        return cls(
            required_scopes=field_required_scopes,
            side_effect=field_side_effect,
            scope_kind=field_scope_kind,
        )


@dataclass(frozen=True, slots=True)
class OperationJobMetadata:
    """How an operation completes and, when durable work is involved, what kind of job it starts."""

    completion_mode: OperationCompletionMode
    job_kind: OpenCode | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["completion_mode"] = self.completion_mode
        if self.job_kind is not None:
            wire["job_kind"] = self.job_kind
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "OperationJobMetadata") -> OperationJobMetadata:
        """Decode a wire payload into a OperationJobMetadata.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_completion_mode = _decode_str(
            _require_field(mapping, "completion_mode", path),
            f"{path}.completion_mode",
        )
        field_job_kind: OpenCode | None = None
        if "job_kind" in mapping:
            raw_job_kind = mapping["job_kind"]
            if raw_job_kind is None:
                raise ContractDecodeError(
                    f"{path}.job_kind: null is not a valid value"
                )
            field_job_kind = _decode_str(raw_job_kind, f"{path}.job_kind")
        return cls(
            completion_mode=field_completion_mode,
            job_kind=field_job_kind,
        )


@dataclass(frozen=True, slots=True)
class OperationAuditMetadata:
    """Whether and how this operation is audited."""

    audited: bool
    audit_category: OpenCode | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["audited"] = self.audited
        if self.audit_category is not None:
            wire["audit_category"] = self.audit_category
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "OperationAuditMetadata"
    ) -> OperationAuditMetadata:
        """Decode a wire payload into a OperationAuditMetadata.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_audited = _decode_bool(_require_field(mapping, "audited", path), f"{path}.audited")
        field_audit_category: OpenCode | None = None
        if "audit_category" in mapping:
            raw_audit_category = mapping["audit_category"]
            if raw_audit_category is None:
                raise ContractDecodeError(
                    f"{path}.audit_category: null is not a valid value"
                )
            field_audit_category = _decode_str(raw_audit_category, f"{path}.audit_category")
        return cls(
            audited=field_audited,
            audit_category=field_audit_category,
        )


@dataclass(frozen=True, slots=True)
class SourceReference:
    """A pointer to the external or internal thing a record's claim came from."""

    kind: SourceKind
    source_id: Identifier
    locator: str | None = None
    retrieved_at: Timestamp | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["kind"] = self.kind
        wire["source_id"] = self.source_id
        if self.locator is not None:
            wire["locator"] = self.locator
        if self.retrieved_at is not None:
            wire["retrieved_at"] = self.retrieved_at
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "SourceReference") -> SourceReference:
        """Decode a wire payload into a SourceReference.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_kind = _decode_str(_require_field(mapping, "kind", path), f"{path}.kind")
        field_source_id = _decode_str(
            _require_field(mapping, "source_id", path),
            f"{path}.source_id",
        )
        field_locator: str | None = None
        if "locator" in mapping:
            raw_locator = mapping["locator"]
            if raw_locator is None:
                raise ContractDecodeError(
                    f"{path}.locator: null is not a valid value"
                )
            field_locator = _decode_str(raw_locator, f"{path}.locator")
        field_retrieved_at: Timestamp | None = None
        if "retrieved_at" in mapping:
            raw_retrieved_at = mapping["retrieved_at"]
            if raw_retrieved_at is None:
                raise ContractDecodeError(
                    f"{path}.retrieved_at: null is not a valid value"
                )
            field_retrieved_at = _decode_str(raw_retrieved_at, f"{path}.retrieved_at")
        return cls(
            kind=field_kind,
            source_id=field_source_id,
            locator=field_locator,
            retrieved_at=field_retrieved_at,
        )


@dataclass(frozen=True, slots=True)
class RecordTemporalMetadata:
    """The distinct instants a governed record's lifecycle turns on: when the underlying fact
    was observed, when the system ingested it, when this version was persisted, and the
    window it is asserted valid for.
    """

    ingested_at: Timestamp
    recorded_at: Timestamp
    observed_at: Timestamp | None = None
    valid_from: Timestamp | None = None
    valid_until: Timestamp | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        if self.observed_at is not None:
            wire["observed_at"] = self.observed_at
        wire["ingested_at"] = self.ingested_at
        wire["recorded_at"] = self.recorded_at
        if self.valid_from is not None:
            wire["valid_from"] = self.valid_from
        if self.valid_until is not None:
            wire["valid_until"] = self.valid_until
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "RecordTemporalMetadata"
    ) -> RecordTemporalMetadata:
        """Decode a wire payload into a RecordTemporalMetadata.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_observed_at: Timestamp | None = None
        if "observed_at" in mapping:
            raw_observed_at = mapping["observed_at"]
            if raw_observed_at is None:
                raise ContractDecodeError(
                    f"{path}.observed_at: null is not a valid value"
                )
            field_observed_at = _decode_str(raw_observed_at, f"{path}.observed_at")
        field_ingested_at = _decode_str(
            _require_field(mapping, "ingested_at", path),
            f"{path}.ingested_at",
        )
        field_recorded_at = _decode_str(
            _require_field(mapping, "recorded_at", path),
            f"{path}.recorded_at",
        )
        field_valid_from: Timestamp | None = None
        if "valid_from" in mapping:
            raw_valid_from = mapping["valid_from"]
            if raw_valid_from is None:
                raise ContractDecodeError(
                    f"{path}.valid_from: null is not a valid value"
                )
            field_valid_from = _decode_str(raw_valid_from, f"{path}.valid_from")
        field_valid_until: Timestamp | None = None
        if "valid_until" in mapping:
            raw_valid_until = mapping["valid_until"]
            if raw_valid_until is None:
                raise ContractDecodeError(
                    f"{path}.valid_until: null is not a valid value"
                )
            field_valid_until = _decode_str(raw_valid_until, f"{path}.valid_until")
        return cls(
            observed_at=field_observed_at,
            ingested_at=field_ingested_at,
            recorded_at=field_recorded_at,
            valid_from=field_valid_from,
            valid_until=field_valid_until,
        )


@dataclass(frozen=True, slots=True)
class SupersessionReference:
    """A direction-neutral pointer from one record version to another related record version.
    The direction of the relationship comes entirely from which field on `RecordIdentity`
    carries it (`supersedes` vs `superseded_by`); this DTO itself states only which record
    and version, and why.
    """

    record_id: RecordId
    version: RecordVersion | None = None
    reason: OpenCode | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["record_id"] = self.record_id
        if self.version is not None:
            wire["version"] = self.version
        if self.reason is not None:
            wire["reason"] = self.reason
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "SupersessionReference"
    ) -> SupersessionReference:
        """Decode a wire payload into a SupersessionReference.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_record_id = _decode_str(
            _require_field(mapping, "record_id", path),
            f"{path}.record_id",
        )
        field_version: RecordVersion | None = None
        if "version" in mapping:
            raw_version = mapping["version"]
            if raw_version is None:
                raise ContractDecodeError(
                    f"{path}.version: null is not a valid value"
                )
            field_version = _decode_str(raw_version, f"{path}.version")
        field_reason: OpenCode | None = None
        if "reason" in mapping:
            raw_reason = mapping["reason"]
            if raw_reason is None:
                raise ContractDecodeError(
                    f"{path}.reason: null is not a valid value"
                )
            field_reason = _decode_str(raw_reason, f"{path}.reason")
        return cls(
            record_id=field_record_id,
            version=field_version,
            reason=field_reason,
        )


@dataclass(frozen=True, slots=True)
class ServiceProbeRequest:
    """A request to answer one runtime probe. Deliberately distinct from `RequestEnvelope`: it
    carries no `operation`, no `input`, and no workspace or authority scoping, because a
    probe must be answerable before those concepts apply.
    """

    probe: ProbeKind
    request_id: RequestId | None = None
    deadline_ms: DurationMs | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["probe"] = self.probe
        if self.request_id is not None:
            wire["request_id"] = self.request_id
        if self.deadline_ms is not None:
            wire["deadline_ms"] = self.deadline_ms
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "ServiceProbeRequest") -> ServiceProbeRequest:
        """Decode a wire payload into a ServiceProbeRequest.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_probe = _decode_str(_require_field(mapping, "probe", path), f"{path}.probe")
        field_request_id: RequestId | None = None
        if "request_id" in mapping:
            raw_request_id = mapping["request_id"]
            if raw_request_id is None:
                raise ContractDecodeError(
                    f"{path}.request_id: null is not a valid value"
                )
            field_request_id = _decode_str(raw_request_id, f"{path}.request_id")
        field_deadline_ms: DurationMs | None = None
        if "deadline_ms" in mapping:
            raw_deadline_ms = mapping["deadline_ms"]
            if raw_deadline_ms is None:
                raise ContractDecodeError(
                    f"{path}.deadline_ms: null is not a valid value"
                )
            field_deadline_ms = _decode_int(raw_deadline_ms, f"{path}.deadline_ms")
        return cls(
            probe=field_probe,
            request_id=field_request_id,
            deadline_ms=field_deadline_ms,
        )


@dataclass(frozen=True, slots=True)
class ServiceComponentStatus:
    """The health of one subsystem a readiness or health probe inspected."""

    id: Identifier
    status: ProbeStatus
    observed_at: Timestamp
    message: str | None = None
    details: JsonObject | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["id"] = self.id
        wire["status"] = self.status
        wire["observed_at"] = self.observed_at
        if self.message is not None:
            wire["message"] = self.message
        if self.details is not None:
            wire["details"] = _encode_json_object(self.details)
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "ServiceComponentStatus"
    ) -> ServiceComponentStatus:
        """Decode a wire payload into a ServiceComponentStatus.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_id = _decode_str(_require_field(mapping, "id", path), f"{path}.id")
        field_status = _decode_str(_require_field(mapping, "status", path), f"{path}.status")
        field_observed_at = _decode_str(
            _require_field(mapping, "observed_at", path),
            f"{path}.observed_at",
        )
        field_message: str | None = None
        if "message" in mapping:
            raw_message = mapping["message"]
            if raw_message is None:
                raise ContractDecodeError(
                    f"{path}.message: null is not a valid value"
                )
            field_message = _decode_str(raw_message, f"{path}.message")
        field_details: JsonObject | None = None
        if "details" in mapping:
            raw_details = mapping["details"]
            if raw_details is None:
                raise ContractDecodeError(
                    f"{path}.details: null is not a valid value"
                )
            field_details = _decode_json_object(raw_details, f"{path}.details")
        return cls(
            id=field_id,
            status=field_status,
            observed_at=field_observed_at,
            message=field_message,
            details=field_details,
        )


@dataclass(frozen=True, slots=True)
class GrantedAuthority:
    """Server-produced, validated authority actually applied to a request. This is the only
    authority statement a client may trust.
    """

    principal_id: Identifier
    roles: tuple[Identifier, ...]
    capabilities: tuple[CapabilityRef, ...]

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["principal_id"] = self.principal_id
        wire["roles"] = list(self.roles)
        wire["capabilities"] = [item.to_wire() for item in self.capabilities]
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "GrantedAuthority") -> GrantedAuthority:
        """Decode a wire payload into a GrantedAuthority.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_principal_id = _decode_str(
            _require_field(mapping, "principal_id", path),
            f"{path}.principal_id",
        )
        field_roles_items = _decode_sequence(
            _require_field(mapping, "roles", path),
            f"{path}.roles",
        )
        field_roles = tuple(
            _decode_str(item, f"{path}.roles[{index}]")
            for index, item in enumerate(field_roles_items)
        )
        field_capabilities_items = _decode_sequence(
            _require_field(mapping, "capabilities", path),
            f"{path}.capabilities",
        )
        field_capabilities = tuple(
            CapabilityRef.from_wire(item, f"{path}.capabilities[{index}]")
            for index, item in enumerate(field_capabilities_items)
        )
        return cls(
            principal_id=field_principal_id,
            roles=field_roles,
            capabilities=field_capabilities,
        )


@dataclass(frozen=True, slots=True)
class CompatibilityMetadata:
    """The outcome of version negotiation: what was selected, what is supported, and what the
    caller must do next.
    """

    selected_api_version: ContractVersion
    selected_workspace_version: ContractVersion
    supported_api_versions: VersionWindow
    supported_workspace_versions: VersionWindow
    status: OpenCode
    upgrade_state: UpgradeState
    deprecations: tuple[Deprecation, ...]

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["selected_api_version"] = self.selected_api_version
        wire["selected_workspace_version"] = self.selected_workspace_version
        wire["supported_api_versions"] = self.supported_api_versions.to_wire()
        wire["supported_workspace_versions"] = self.supported_workspace_versions.to_wire()
        wire["status"] = self.status
        wire["upgrade_state"] = self.upgrade_state.to_wire()
        wire["deprecations"] = [item.to_wire() for item in self.deprecations]
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "CompatibilityMetadata"
    ) -> CompatibilityMetadata:
        """Decode a wire payload into a CompatibilityMetadata.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_selected_api_version = _decode_str(
            _require_field(mapping, "selected_api_version", path),
            f"{path}.selected_api_version",
        )
        field_selected_workspace_version = _decode_str(
            _require_field(mapping, "selected_workspace_version", path),
            f"{path}.selected_workspace_version",
        )
        field_supported_api_versions = VersionWindow.from_wire(
            _require_field(mapping, "supported_api_versions", path),
            f"{path}.supported_api_versions",
        )
        field_supported_workspace_versions = VersionWindow.from_wire(
            _require_field(mapping, "supported_workspace_versions", path),
            f"{path}.supported_workspace_versions",
        )
        field_status = _decode_str(_require_field(mapping, "status", path), f"{path}.status")
        field_upgrade_state = UpgradeState.from_wire(
            _require_field(mapping, "upgrade_state", path),
            f"{path}.upgrade_state",
        )
        field_deprecations_items = _decode_sequence(
            _require_field(mapping, "deprecations", path),
            f"{path}.deprecations",
        )
        field_deprecations = tuple(
            Deprecation.from_wire(item, f"{path}.deprecations[{index}]")
            for index, item in enumerate(field_deprecations_items)
        )
        return cls(
            selected_api_version=field_selected_api_version,
            selected_workspace_version=field_selected_workspace_version,
            supported_api_versions=field_supported_api_versions,
            supported_workspace_versions=field_supported_workspace_versions,
            status=field_status,
            upgrade_state=field_upgrade_state,
            deprecations=field_deprecations,
        )


@dataclass(frozen=True, slots=True)
class CapabilitySet:
    """The three capability views a caller needs: what the server can do, what this caller is
    allowed, and the intersection actually usable. `effective` is always `supported`
    intersected with `granted`; it is never widened by a claim.
    """

    supported: tuple[CapabilityRef, ...]
    granted: tuple[CapabilityRef, ...]
    effective: tuple[CapabilityRef, ...]

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["supported"] = [item.to_wire() for item in self.supported]
        wire["granted"] = [item.to_wire() for item in self.granted]
        wire["effective"] = [item.to_wire() for item in self.effective]
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "CapabilitySet") -> CapabilitySet:
        """Decode a wire payload into a CapabilitySet.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_supported_items = _decode_sequence(
            _require_field(mapping, "supported", path),
            f"{path}.supported",
        )
        field_supported = tuple(
            CapabilityRef.from_wire(item, f"{path}.supported[{index}]")
            for index, item in enumerate(field_supported_items)
        )
        field_granted_items = _decode_sequence(
            _require_field(mapping, "granted", path),
            f"{path}.granted",
        )
        field_granted = tuple(
            CapabilityRef.from_wire(item, f"{path}.granted[{index}]")
            for index, item in enumerate(field_granted_items)
        )
        field_effective_items = _decode_sequence(
            _require_field(mapping, "effective", path),
            f"{path}.effective",
        )
        field_effective = tuple(
            CapabilityRef.from_wire(item, f"{path}.effective[{index}]")
            for index, item in enumerate(field_effective_items)
        )
        return cls(
            supported=field_supported,
            granted=field_granted,
            effective=field_effective,
        )


@dataclass(frozen=True, slots=True)
class ReleaseCompatibilityEntry:
    """One concrete component release, the contract and workspace-format version windows it
    supports, and how thoroughly that support has been qualified. An entry's mere presence is
    not itself support evidence; `qualification_state` is.
    """

    component: ComponentKind
    release_version: ReleaseVersion
    api_version: ContractVersion
    supported_api_versions: VersionWindow
    supported_workspace_versions: VersionWindow
    qualification_state: QualificationState

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["component"] = self.component
        wire["release_version"] = self.release_version
        wire["api_version"] = self.api_version
        wire["supported_api_versions"] = self.supported_api_versions.to_wire()
        wire["supported_workspace_versions"] = self.supported_workspace_versions.to_wire()
        wire["qualification_state"] = self.qualification_state
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "ReleaseCompatibilityEntry"
    ) -> ReleaseCompatibilityEntry:
        """Decode a wire payload into a ReleaseCompatibilityEntry.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_component = _decode_str(
            _require_field(mapping, "component", path),
            f"{path}.component",
        )
        field_release_version = _decode_str(
            _require_field(mapping, "release_version", path),
            f"{path}.release_version",
        )
        field_api_version = _decode_str(
            _require_field(mapping, "api_version", path),
            f"{path}.api_version",
        )
        field_supported_api_versions = VersionWindow.from_wire(
            _require_field(mapping, "supported_api_versions", path),
            f"{path}.supported_api_versions",
        )
        field_supported_workspace_versions = VersionWindow.from_wire(
            _require_field(mapping, "supported_workspace_versions", path),
            f"{path}.supported_workspace_versions",
        )
        field_qualification_state = _decode_str(
            _require_field(mapping, "qualification_state", path),
            f"{path}.qualification_state",
        )
        return cls(
            component=field_component,
            release_version=field_release_version,
            api_version=field_api_version,
            supported_api_versions=field_supported_api_versions,
            supported_workspace_versions=field_supported_workspace_versions,
            qualification_state=field_qualification_state,
        )


@dataclass(frozen=True, slots=True)
class CapabilityCompatibilityEntry:
    """One capability's compatibility posture: the concrete capability and version, when it was
    introduced, and how thoroughly it has been qualified. Mirrors
    `ReleaseCompatibilityEntry`'s qualification discipline: presence in this list is not
    itself support evidence.
    """

    capability: CapabilityRef
    introduced_in: ContractVersion
    qualification_state: QualificationState

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["capability"] = self.capability.to_wire()
        wire["introduced_in"] = self.introduced_in
        wire["qualification_state"] = self.qualification_state
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "CapabilityCompatibilityEntry"
    ) -> CapabilityCompatibilityEntry:
        """Decode a wire payload into a CapabilityCompatibilityEntry.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_capability = CapabilityRef.from_wire(
            _require_field(mapping, "capability", path),
            f"{path}.capability",
        )
        field_introduced_in = _decode_str(
            _require_field(mapping, "introduced_in", path),
            f"{path}.introduced_in",
        )
        field_qualification_state = _decode_str(
            _require_field(mapping, "qualification_state", path),
            f"{path}.qualification_state",
        )
        return cls(
            capability=field_capability,
            introduced_in=field_introduced_in,
            qualification_state=field_qualification_state,
        )


@dataclass(frozen=True, slots=True)
class OperationCompatibilityEntry:
    """One operation's compatibility posture: when it was introduced, its current lifecycle
    state, and how thoroughly that lifecycle state has actually been verified. `state` and
    `qualification_state` are deliberately separate axes: an operation's mere presence in
    this list, or its lifecycle being `stable`, must never be read as evidence that it has
    been qualified -- only `qualification_state` is that evidence.
    """

    operation: OperationName
    introduced_in: ContractVersion
    state: OperationCompatibilityState
    qualification_state: QualificationState
    deprecation: Deprecation | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["operation"] = self.operation
        wire["introduced_in"] = self.introduced_in
        wire["state"] = self.state
        wire["qualification_state"] = self.qualification_state
        if self.deprecation is not None:
            wire["deprecation"] = self.deprecation.to_wire()
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "OperationCompatibilityEntry"
    ) -> OperationCompatibilityEntry:
        """Decode a wire payload into a OperationCompatibilityEntry.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_operation = _decode_str(
            _require_field(mapping, "operation", path),
            f"{path}.operation",
        )
        field_introduced_in = _decode_str(
            _require_field(mapping, "introduced_in", path),
            f"{path}.introduced_in",
        )
        field_state = _decode_str(_require_field(mapping, "state", path), f"{path}.state")
        field_qualification_state = _decode_str(
            _require_field(mapping, "qualification_state", path),
            f"{path}.qualification_state",
        )
        field_deprecation: Deprecation | None = None
        if "deprecation" in mapping:
            raw_deprecation = mapping["deprecation"]
            if raw_deprecation is None:
                raise ContractDecodeError(
                    f"{path}.deprecation: null is not a valid value"
                )
            field_deprecation = Deprecation.from_wire(raw_deprecation, f"{path}.deprecation")
        return cls(
            operation=field_operation,
            introduced_in=field_introduced_in,
            state=field_state,
            qualification_state=field_qualification_state,
            deprecation=field_deprecation,
        )


@dataclass(frozen=True, slots=True)
class RequestMetadata:
    """Everything the server needs to route, scope, bound, and audit a request, independent of
    the operation payload.
    """

    request_id: RequestId
    correlation_id: CorrelationId
    trace_id: TraceId
    api_version: ContractVersion
    client: ClientIdentity
    workspace_id: WorkspaceId
    scopes: tuple[Scope, ...]
    purpose: Purpose
    required_capabilities: tuple[CapabilityRequirement, ...]
    deadline_ms: DurationMs | None = None
    idempotency_key: IdempotencyKey | None = None
    mutation_precondition: MutationPrecondition | None = None
    principal_claim: PrincipalClaim | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["request_id"] = self.request_id
        wire["correlation_id"] = self.correlation_id
        wire["trace_id"] = self.trace_id
        wire["api_version"] = self.api_version
        wire["client"] = self.client.to_wire()
        wire["workspace_id"] = self.workspace_id
        wire["scopes"] = list(self.scopes)
        wire["purpose"] = self.purpose
        if self.deadline_ms is not None:
            wire["deadline_ms"] = self.deadline_ms
        if self.idempotency_key is not None:
            wire["idempotency_key"] = self.idempotency_key
        if self.mutation_precondition is not None:
            wire["mutation_precondition"] = self.mutation_precondition.to_wire()
        wire["required_capabilities"] = [item.to_wire() for item in self.required_capabilities]
        if self.principal_claim is not None:
            wire["principal_claim"] = self.principal_claim.to_wire()
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "RequestMetadata") -> RequestMetadata:
        """Decode a wire payload into a RequestMetadata.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_request_id = _decode_str(
            _require_field(mapping, "request_id", path),
            f"{path}.request_id",
        )
        field_correlation_id = _decode_str(
            _require_field(mapping, "correlation_id", path),
            f"{path}.correlation_id",
        )
        field_trace_id = _decode_str(_require_field(mapping, "trace_id", path), f"{path}.trace_id")
        field_api_version = _decode_str(
            _require_field(mapping, "api_version", path),
            f"{path}.api_version",
        )
        field_client = ClientIdentity.from_wire(
            _require_field(mapping, "client", path),
            f"{path}.client",
        )
        field_workspace_id = _decode_str(
            _require_field(mapping, "workspace_id", path),
            f"{path}.workspace_id",
        )
        field_scopes_items = _decode_sequence(
            _require_field(mapping, "scopes", path),
            f"{path}.scopes",
        )
        field_scopes = tuple(
            _decode_str(item, f"{path}.scopes[{index}]")
            for index, item in enumerate(field_scopes_items)
        )
        field_purpose = _decode_str(_require_field(mapping, "purpose", path), f"{path}.purpose")
        field_deadline_ms: DurationMs | None = None
        if "deadline_ms" in mapping:
            raw_deadline_ms = mapping["deadline_ms"]
            if raw_deadline_ms is None:
                raise ContractDecodeError(
                    f"{path}.deadline_ms: null is not a valid value"
                )
            field_deadline_ms = _decode_int(raw_deadline_ms, f"{path}.deadline_ms")
        field_idempotency_key: IdempotencyKey | None = None
        if "idempotency_key" in mapping:
            raw_idempotency_key = mapping["idempotency_key"]
            if raw_idempotency_key is None:
                raise ContractDecodeError(
                    f"{path}.idempotency_key: null is not a valid value"
                )
            field_idempotency_key = _decode_str(raw_idempotency_key, f"{path}.idempotency_key")
        field_mutation_precondition: MutationPrecondition | None = None
        if "mutation_precondition" in mapping:
            raw_mutation_precondition = mapping["mutation_precondition"]
            if raw_mutation_precondition is None:
                raise ContractDecodeError(
                    f"{path}.mutation_precondition: null is not a valid value"
                )
            field_mutation_precondition = MutationPrecondition.from_wire(
                raw_mutation_precondition,
                f"{path}.mutation_precondition",
            )
        field_required_capabilities_items = _decode_sequence(
            _require_field(mapping, "required_capabilities", path),
            f"{path}.required_capabilities",
        )
        field_required_capabilities = tuple(
            CapabilityRequirement.from_wire(item, f"{path}.required_capabilities[{index}]")
            for index, item in enumerate(field_required_capabilities_items)
        )
        field_principal_claim: PrincipalClaim | None = None
        if "principal_claim" in mapping:
            raw_principal_claim = mapping["principal_claim"]
            if raw_principal_claim is None:
                raise ContractDecodeError(
                    f"{path}.principal_claim: null is not a valid value"
                )
            field_principal_claim = PrincipalClaim.from_wire(
                raw_principal_claim,
                f"{path}.principal_claim",
            )
        return cls(
            request_id=field_request_id,
            correlation_id=field_correlation_id,
            trace_id=field_trace_id,
            api_version=field_api_version,
            client=field_client,
            workspace_id=field_workspace_id,
            scopes=field_scopes,
            purpose=field_purpose,
            deadline_ms=field_deadline_ms,
            idempotency_key=field_idempotency_key,
            mutation_precondition=field_mutation_precondition,
            required_capabilities=field_required_capabilities,
            principal_claim=field_principal_claim,
        )


@dataclass(frozen=True, slots=True)
class JobAttempt:
    """One execution attempt of a job. A job that is retried has more than one attempt."""

    attempt_number: int
    started_at: Timestamp
    state: JobState
    finished_at: Timestamp | None = None
    error: ApiError | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["attempt_number"] = self.attempt_number
        wire["started_at"] = self.started_at
        if self.finished_at is not None:
            wire["finished_at"] = self.finished_at
        wire["state"] = self.state
        if self.error is not None:
            wire["error"] = self.error.to_wire()
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "JobAttempt") -> JobAttempt:
        """Decode a wire payload into a JobAttempt.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_attempt_number = _decode_int(
            _require_field(mapping, "attempt_number", path),
            f"{path}.attempt_number",
        )
        field_started_at = _decode_str(
            _require_field(mapping, "started_at", path),
            f"{path}.started_at",
        )
        field_finished_at: Timestamp | None = None
        if "finished_at" in mapping:
            raw_finished_at = mapping["finished_at"]
            if raw_finished_at is None:
                raise ContractDecodeError(
                    f"{path}.finished_at: null is not a valid value"
                )
            field_finished_at = _decode_str(raw_finished_at, f"{path}.finished_at")
        field_state = _decode_str(_require_field(mapping, "state", path), f"{path}.state")
        field_error: ApiError | None = None
        if "error" in mapping:
            raw_error = mapping["error"]
            if raw_error is None:
                raise ContractDecodeError(
                    f"{path}.error: null is not a valid value"
                )
            field_error = ApiError.from_wire(raw_error, f"{path}.error")
        return cls(
            attempt_number=field_attempt_number,
            started_at=field_started_at,
            finished_at=field_finished_at,
            state=field_state,
            error=field_error,
        )


@dataclass(frozen=True, slots=True)
class OperationMetadata:
    """The full declared contract characteristics of one operation: its scope, payload schemas,
    required capability, side effects, and
    job/pagination/idempotency/precondition/audit/error posture. This is the complete shape a
    future per-operation catalogue entry will carry, so publishing that catalogue is additive
    rather than requiring later required-field breaks. Binding this to a concrete
    request/result payload and publishing a catalogue of operations is out of scope for this
    document.
    """

    name: OperationName
    scope: OperationScope
    input_schema_ref: SchemaReference
    result_schema_ref: SchemaReference
    required_capability: CapabilityRequirement
    job: OperationJobMetadata
    pagination: OperationPaginationMetadata
    idempotency: OperationIdempotencyMetadata
    precondition: OperationPreconditionMetadata
    audit: OperationAuditMetadata
    allowed_errors: tuple[ErrorCode, ...]

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["name"] = self.name
        wire["scope"] = self.scope.to_wire()
        wire["input_schema_ref"] = self.input_schema_ref
        wire["result_schema_ref"] = self.result_schema_ref
        wire["required_capability"] = self.required_capability.to_wire()
        wire["job"] = self.job.to_wire()
        wire["pagination"] = self.pagination.to_wire()
        wire["idempotency"] = self.idempotency.to_wire()
        wire["precondition"] = self.precondition.to_wire()
        wire["audit"] = self.audit.to_wire()
        wire["allowed_errors"] = list(self.allowed_errors)
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "OperationMetadata") -> OperationMetadata:
        """Decode a wire payload into a OperationMetadata.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_name = _decode_str(_require_field(mapping, "name", path), f"{path}.name")
        field_scope = OperationScope.from_wire(
            _require_field(mapping, "scope", path),
            f"{path}.scope",
        )
        field_input_schema_ref = _decode_str(
            _require_field(mapping, "input_schema_ref", path),
            f"{path}.input_schema_ref",
        )
        field_result_schema_ref = _decode_str(
            _require_field(mapping, "result_schema_ref", path),
            f"{path}.result_schema_ref",
        )
        field_required_capability = CapabilityRequirement.from_wire(
            _require_field(mapping, "required_capability", path),
            f"{path}.required_capability",
        )
        field_job = OperationJobMetadata.from_wire(
            _require_field(mapping, "job", path),
            f"{path}.job",
        )
        field_pagination = OperationPaginationMetadata.from_wire(
            _require_field(mapping, "pagination", path),
            f"{path}.pagination",
        )
        field_idempotency = OperationIdempotencyMetadata.from_wire(
            _require_field(mapping, "idempotency", path),
            f"{path}.idempotency",
        )
        field_precondition = OperationPreconditionMetadata.from_wire(
            _require_field(mapping, "precondition", path),
            f"{path}.precondition",
        )
        field_audit = OperationAuditMetadata.from_wire(
            _require_field(mapping, "audit", path),
            f"{path}.audit",
        )
        field_allowed_errors_items = _decode_sequence(
            _require_field(mapping, "allowed_errors", path),
            f"{path}.allowed_errors",
        )
        field_allowed_errors = tuple(
            _decode_str(item, f"{path}.allowed_errors[{index}]")
            for index, item in enumerate(field_allowed_errors_items)
        )
        return cls(
            name=field_name,
            scope=field_scope,
            input_schema_ref=field_input_schema_ref,
            result_schema_ref=field_result_schema_ref,
            required_capability=field_required_capability,
            job=field_job,
            pagination=field_pagination,
            idempotency=field_idempotency,
            precondition=field_precondition,
            audit=field_audit,
            allowed_errors=field_allowed_errors,
        )


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    """A concrete piece of evidence supporting one claim in a record."""

    source: SourceReference
    span: SourceSpan | None = None
    excerpt: str | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["source"] = self.source.to_wire()
        if self.span is not None:
            wire["span"] = self.span.to_wire()
        if self.excerpt is not None:
            wire["excerpt"] = self.excerpt
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "EvidenceReference") -> EvidenceReference:
        """Decode a wire payload into a EvidenceReference.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_source = SourceReference.from_wire(
            _require_field(mapping, "source", path),
            f"{path}.source",
        )
        field_span: SourceSpan | None = None
        if "span" in mapping:
            raw_span = mapping["span"]
            if raw_span is None:
                raise ContractDecodeError(
                    f"{path}.span: null is not a valid value"
                )
            field_span = SourceSpan.from_wire(raw_span, f"{path}.span")
        field_excerpt: str | None = None
        if "excerpt" in mapping:
            raw_excerpt = mapping["excerpt"]
            if raw_excerpt is None:
                raise ContractDecodeError(
                    f"{path}.excerpt: null is not a valid value"
                )
            field_excerpt = _decode_str(raw_excerpt, f"{path}.excerpt")
        return cls(
            source=field_source,
            span=field_span,
            excerpt=field_excerpt,
        )


@dataclass(frozen=True, slots=True)
class RecordIdentity:
    """The identity, version, governance layer, governance state, and currentness of one record
    version.
    """

    record_id: RecordId
    version: RecordVersion
    layer: GovernanceLayer
    governance_state: GovernanceState
    currentness: RecordCurrentness
    supersedes: SupersessionReference | None = None
    superseded_by: SupersessionReference | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["record_id"] = self.record_id
        wire["version"] = self.version
        wire["layer"] = self.layer
        wire["governance_state"] = self.governance_state
        wire["currentness"] = self.currentness
        if self.supersedes is not None:
            wire["supersedes"] = self.supersedes.to_wire()
        if self.superseded_by is not None:
            wire["superseded_by"] = self.superseded_by.to_wire()
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "RecordIdentity") -> RecordIdentity:
        """Decode a wire payload into a RecordIdentity.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_record_id = _decode_str(
            _require_field(mapping, "record_id", path),
            f"{path}.record_id",
        )
        field_version = _decode_str(_require_field(mapping, "version", path), f"{path}.version")
        field_layer = _decode_str(_require_field(mapping, "layer", path), f"{path}.layer")
        field_governance_state = _decode_str(
            _require_field(mapping, "governance_state", path),
            f"{path}.governance_state",
        )
        field_currentness = _decode_str(
            _require_field(mapping, "currentness", path),
            f"{path}.currentness",
        )
        field_supersedes: SupersessionReference | None = None
        if "supersedes" in mapping:
            raw_supersedes = mapping["supersedes"]
            if raw_supersedes is None:
                raise ContractDecodeError(
                    f"{path}.supersedes: null is not a valid value"
                )
            field_supersedes = SupersessionReference.from_wire(raw_supersedes, f"{path}.supersedes")
        field_superseded_by: SupersessionReference | None = None
        if "superseded_by" in mapping:
            raw_superseded_by = mapping["superseded_by"]
            if raw_superseded_by is None:
                raise ContractDecodeError(
                    f"{path}.superseded_by: null is not a valid value"
                )
            field_superseded_by = SupersessionReference.from_wire(
                raw_superseded_by,
                f"{path}.superseded_by",
            )
        return cls(
            record_id=field_record_id,
            version=field_version,
            layer=field_layer,
            governance_state=field_governance_state,
            currentness=field_currentness,
            supersedes=field_supersedes,
            superseded_by=field_superseded_by,
        )


@dataclass(frozen=True, slots=True)
class ServiceProbeResult:
    """The answer to one runtime probe. Deliberately distinct from `SuccessResponseEnvelope` /
    `ErrorResponseEnvelope`: it carries no `result`/`error` branch and no negotiated
    authority, because a probe answers a transport-level question, not an application
    operation.
    """

    probe: ProbeKind
    status: ProbeStatus
    server_version: ReleaseVersion
    api_version: ContractVersion
    observed_at: Timestamp
    components: tuple[ServiceComponentStatus, ...] | None = None
    supported_capabilities: tuple[CapabilityRef, ...] | None = None
    details: JsonObject | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["probe"] = self.probe
        wire["status"] = self.status
        wire["server_version"] = self.server_version
        wire["api_version"] = self.api_version
        wire["observed_at"] = self.observed_at
        if self.components is not None:
            wire["components"] = [item.to_wire() for item in self.components]
        if self.supported_capabilities is not None:
            wire["supported_capabilities"] = [item.to_wire() for item in self.supported_capabilities]
        if self.details is not None:
            wire["details"] = _encode_json_object(self.details)
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "ServiceProbeResult") -> ServiceProbeResult:
        """Decode a wire payload into a ServiceProbeResult.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_probe = _decode_str(_require_field(mapping, "probe", path), f"{path}.probe")
        field_status = _decode_str(_require_field(mapping, "status", path), f"{path}.status")
        field_server_version = _decode_str(
            _require_field(mapping, "server_version", path),
            f"{path}.server_version",
        )
        field_api_version = _decode_str(
            _require_field(mapping, "api_version", path),
            f"{path}.api_version",
        )
        field_observed_at = _decode_str(
            _require_field(mapping, "observed_at", path),
            f"{path}.observed_at",
        )
        field_components: tuple[ServiceComponentStatus, ...] | None = None
        if "components" in mapping:
            raw_components = mapping["components"]
            if raw_components is None:
                raise ContractDecodeError(
                    f"{path}.components: null is not a valid value"
                )
            field_components_items = _decode_sequence(raw_components, f"{path}.components")
            field_components = tuple(
                ServiceComponentStatus.from_wire(item, f"{path}.components[{index}]")
                for index, item in enumerate(field_components_items)
            )
        field_supported_capabilities: tuple[CapabilityRef, ...] | None = None
        if "supported_capabilities" in mapping:
            raw_supported_capabilities = mapping["supported_capabilities"]
            if raw_supported_capabilities is None:
                raise ContractDecodeError(
                    f"{path}.supported_capabilities: null is not a valid value"
                )
            field_supported_capabilities_items = _decode_sequence(
                raw_supported_capabilities,
                f"{path}.supported_capabilities",
            )
            field_supported_capabilities = tuple(
                CapabilityRef.from_wire(item, f"{path}.supported_capabilities[{index}]")
                for index, item in enumerate(field_supported_capabilities_items)
            )
        field_details: JsonObject | None = None
        if "details" in mapping:
            raw_details = mapping["details"]
            if raw_details is None:
                raise ContractDecodeError(
                    f"{path}.details: null is not a valid value"
                )
            field_details = _decode_json_object(raw_details, f"{path}.details")
        return cls(
            probe=field_probe,
            status=field_status,
            server_version=field_server_version,
            api_version=field_api_version,
            observed_at=field_observed_at,
            components=field_components,
            supported_capabilities=field_supported_capabilities,
            details=field_details,
        )


@dataclass(frozen=True, slots=True)
class VersionCapabilityEnvelope:
    """Everything a caller needs to reason about what this server accepted and what it can do.
    Returned on every response, success or error.
    """

    api_version: ContractVersion
    server_version: ReleaseVersion
    workspace_format_version: ContractVersion
    compatibility: CompatibilityMetadata
    capabilities: CapabilitySet

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["api_version"] = self.api_version
        wire["server_version"] = self.server_version
        wire["workspace_format_version"] = self.workspace_format_version
        wire["compatibility"] = self.compatibility.to_wire()
        wire["capabilities"] = self.capabilities.to_wire()
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "VersionCapabilityEnvelope"
    ) -> VersionCapabilityEnvelope:
        """Decode a wire payload into a VersionCapabilityEnvelope.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_api_version = _decode_str(
            _require_field(mapping, "api_version", path),
            f"{path}.api_version",
        )
        field_server_version = _decode_str(
            _require_field(mapping, "server_version", path),
            f"{path}.server_version",
        )
        field_workspace_format_version = _decode_str(
            _require_field(mapping, "workspace_format_version", path),
            f"{path}.workspace_format_version",
        )
        field_compatibility = CompatibilityMetadata.from_wire(
            _require_field(mapping, "compatibility", path),
            f"{path}.compatibility",
        )
        field_capabilities = CapabilitySet.from_wire(
            _require_field(mapping, "capabilities", path),
            f"{path}.capabilities",
        )
        return cls(
            api_version=field_api_version,
            server_version=field_server_version,
            workspace_format_version=field_workspace_format_version,
            compatibility=field_compatibility,
            capabilities=field_capabilities,
        )


@dataclass(frozen=True, slots=True)
class CompatibilityMatrix:
    """The compatibility matrix foundation: every known release's supported version windows,
    every known operation's introduction version, lifecycle state, and qualification state,
    and every known capability's compatibility posture. This shape is not itself evidence
    that anything it lists is supported -- each entry's own `qualification_state` is the only
    thing a caller may treat as a support claim; neither an entry's mere presence nor an
    operation's lifecycle `state` implies qualification, and an empty or unverified matrix
    must never be read as 'nothing is unsupported'.
    """

    releases: tuple[ReleaseCompatibilityEntry, ...]
    operations: tuple[OperationCompatibilityEntry, ...]
    capabilities: tuple[CapabilityCompatibilityEntry, ...]

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["releases"] = [item.to_wire() for item in self.releases]
        wire["operations"] = [item.to_wire() for item in self.operations]
        wire["capabilities"] = [item.to_wire() for item in self.capabilities]
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "CompatibilityMatrix") -> CompatibilityMatrix:
        """Decode a wire payload into a CompatibilityMatrix.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_releases_items = _decode_sequence(
            _require_field(mapping, "releases", path),
            f"{path}.releases",
        )
        field_releases = tuple(
            ReleaseCompatibilityEntry.from_wire(item, f"{path}.releases[{index}]")
            for index, item in enumerate(field_releases_items)
        )
        field_operations_items = _decode_sequence(
            _require_field(mapping, "operations", path),
            f"{path}.operations",
        )
        field_operations = tuple(
            OperationCompatibilityEntry.from_wire(item, f"{path}.operations[{index}]")
            for index, item in enumerate(field_operations_items)
        )
        field_capabilities_items = _decode_sequence(
            _require_field(mapping, "capabilities", path),
            f"{path}.capabilities",
        )
        field_capabilities = tuple(
            CapabilityCompatibilityEntry.from_wire(item, f"{path}.capabilities[{index}]")
            for index, item in enumerate(field_capabilities_items)
        )
        return cls(
            releases=field_releases,
            operations=field_operations,
            capabilities=field_capabilities,
        )


@dataclass(frozen=True, slots=True)
class RequestEnvelope:
    """A single application request: what to do, under what conditions, with what payload."""

    operation: OperationName
    metadata: RequestMetadata
    input: JsonObject

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["operation"] = self.operation
        wire["metadata"] = self.metadata.to_wire()
        wire["input"] = _encode_json_object(self.input)
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "RequestEnvelope") -> RequestEnvelope:
        """Decode a wire payload into a RequestEnvelope.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_operation = _decode_str(
            _require_field(mapping, "operation", path),
            f"{path}.operation",
        )
        field_metadata = RequestMetadata.from_wire(
            _require_field(mapping, "metadata", path),
            f"{path}.metadata",
        )
        field_input = _decode_json_object(_require_field(mapping, "input", path), f"{path}.input")
        return cls(
            operation=field_operation,
            metadata=field_metadata,
            input=field_input,
        )


@dataclass(frozen=True, slots=True)
class JobHandle:
    """What a caller holds to track a job over time: its identity, current state, latest known
    progress and attempt, and which control actions are available.
    """

    identity: JobIdentity
    state: JobState
    created_at: Timestamp
    updated_at: Timestamp
    control: JobControl
    progress: JobProgress | None = None
    latest_attempt: JobAttempt | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["identity"] = self.identity.to_wire()
        wire["state"] = self.state
        wire["created_at"] = self.created_at
        wire["updated_at"] = self.updated_at
        wire["control"] = self.control.to_wire()
        if self.progress is not None:
            wire["progress"] = self.progress.to_wire()
        if self.latest_attempt is not None:
            wire["latest_attempt"] = self.latest_attempt.to_wire()
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "JobHandle") -> JobHandle:
        """Decode a wire payload into a JobHandle.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_identity = JobIdentity.from_wire(
            _require_field(mapping, "identity", path),
            f"{path}.identity",
        )
        field_state = _decode_str(_require_field(mapping, "state", path), f"{path}.state")
        field_created_at = _decode_str(
            _require_field(mapping, "created_at", path),
            f"{path}.created_at",
        )
        field_updated_at = _decode_str(
            _require_field(mapping, "updated_at", path),
            f"{path}.updated_at",
        )
        field_control = JobControl.from_wire(
            _require_field(mapping, "control", path),
            f"{path}.control",
        )
        field_progress: JobProgress | None = None
        if "progress" in mapping:
            raw_progress = mapping["progress"]
            if raw_progress is None:
                raise ContractDecodeError(
                    f"{path}.progress: null is not a valid value"
                )
            field_progress = JobProgress.from_wire(raw_progress, f"{path}.progress")
        field_latest_attempt: JobAttempt | None = None
        if "latest_attempt" in mapping:
            raw_latest_attempt = mapping["latest_attempt"]
            if raw_latest_attempt is None:
                raise ContractDecodeError(
                    f"{path}.latest_attempt: null is not a valid value"
                )
            field_latest_attempt = JobAttempt.from_wire(
                raw_latest_attempt,
                f"{path}.latest_attempt",
            )
        return cls(
            identity=field_identity,
            state=field_state,
            created_at=field_created_at,
            updated_at=field_updated_at,
            control=field_control,
            progress=field_progress,
            latest_attempt=field_latest_attempt,
        )


@dataclass(frozen=True, slots=True)
class JobTerminalSuccess:
    """The final outcome of a job that succeeded. Carries `result` and never `error` or
    `cancellation`.
    """

    identity: JobIdentity
    state: JobState
    finished_at: Timestamp
    attempts: tuple[JobAttempt, ...]
    result: JsonObject

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["identity"] = self.identity.to_wire()
        wire["state"] = self.state
        wire["finished_at"] = self.finished_at
        wire["attempts"] = [item.to_wire() for item in self.attempts]
        wire["result"] = _encode_json_object(self.result)
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "JobTerminalSuccess") -> JobTerminalSuccess:
        """Decode a wire payload into a JobTerminalSuccess.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_identity = JobIdentity.from_wire(
            _require_field(mapping, "identity", path),
            f"{path}.identity",
        )
        field_state = _decode_str(_require_field(mapping, "state", path), f"{path}.state")
        field_finished_at = _decode_str(
            _require_field(mapping, "finished_at", path),
            f"{path}.finished_at",
        )
        field_attempts_items = _decode_sequence(
            _require_field(mapping, "attempts", path),
            f"{path}.attempts",
        )
        field_attempts = tuple(
            JobAttempt.from_wire(item, f"{path}.attempts[{index}]")
            for index, item in enumerate(field_attempts_items)
        )
        field_result = _decode_json_object(
            _require_field(mapping, "result", path),
            f"{path}.result",
        )
        return cls(
            identity=field_identity,
            state=field_state,
            finished_at=field_finished_at,
            attempts=field_attempts,
            result=field_result,
        )


@dataclass(frozen=True, slots=True)
class JobTerminalFailure:
    """The final outcome of a job that failed. Carries `error` and never `result` or
    `cancellation`.
    """

    identity: JobIdentity
    state: JobState
    finished_at: Timestamp
    attempts: tuple[JobAttempt, ...]
    error: ApiError

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["identity"] = self.identity.to_wire()
        wire["state"] = self.state
        wire["finished_at"] = self.finished_at
        wire["attempts"] = [item.to_wire() for item in self.attempts]
        wire["error"] = self.error.to_wire()
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "JobTerminalFailure") -> JobTerminalFailure:
        """Decode a wire payload into a JobTerminalFailure.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_identity = JobIdentity.from_wire(
            _require_field(mapping, "identity", path),
            f"{path}.identity",
        )
        field_state = _decode_str(_require_field(mapping, "state", path), f"{path}.state")
        field_finished_at = _decode_str(
            _require_field(mapping, "finished_at", path),
            f"{path}.finished_at",
        )
        field_attempts_items = _decode_sequence(
            _require_field(mapping, "attempts", path),
            f"{path}.attempts",
        )
        field_attempts = tuple(
            JobAttempt.from_wire(item, f"{path}.attempts[{index}]")
            for index, item in enumerate(field_attempts_items)
        )
        field_error = ApiError.from_wire(_require_field(mapping, "error", path), f"{path}.error")
        return cls(
            identity=field_identity,
            state=field_state,
            finished_at=field_finished_at,
            attempts=field_attempts,
            error=field_error,
        )


@dataclass(frozen=True, slots=True)
class JobTerminalCancellation:
    """The final outcome of a job that was cancelled. Carries `cancellation` and never `result`
    or `error`.
    """

    identity: JobIdentity
    state: JobState
    finished_at: Timestamp
    attempts: tuple[JobAttempt, ...]
    cancellation: JobCancellationOutcome

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["identity"] = self.identity.to_wire()
        wire["state"] = self.state
        wire["finished_at"] = self.finished_at
        wire["attempts"] = [item.to_wire() for item in self.attempts]
        wire["cancellation"] = self.cancellation.to_wire()
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "JobTerminalCancellation"
    ) -> JobTerminalCancellation:
        """Decode a wire payload into a JobTerminalCancellation.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_identity = JobIdentity.from_wire(
            _require_field(mapping, "identity", path),
            f"{path}.identity",
        )
        field_state = _decode_str(_require_field(mapping, "state", path), f"{path}.state")
        field_finished_at = _decode_str(
            _require_field(mapping, "finished_at", path),
            f"{path}.finished_at",
        )
        field_attempts_items = _decode_sequence(
            _require_field(mapping, "attempts", path),
            f"{path}.attempts",
        )
        field_attempts = tuple(
            JobAttempt.from_wire(item, f"{path}.attempts[{index}]")
            for index, item in enumerate(field_attempts_items)
        )
        field_cancellation = JobCancellationOutcome.from_wire(
            _require_field(mapping, "cancellation", path),
            f"{path}.cancellation",
        )
        return cls(
            identity=field_identity,
            state=field_state,
            finished_at=field_finished_at,
            attempts=field_attempts,
            cancellation=field_cancellation,
        )


@dataclass(frozen=True, slots=True)
class ProvenanceEntry:
    """One step in a record's history: who or what did what, and when."""

    actor_id: Identifier
    actor_kind: OpenCode
    action: OpenCode
    occurred_at: Timestamp
    evidence: tuple[EvidenceReference, ...] | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["actor_id"] = self.actor_id
        wire["actor_kind"] = self.actor_kind
        wire["action"] = self.action
        wire["occurred_at"] = self.occurred_at
        if self.evidence is not None:
            wire["evidence"] = [item.to_wire() for item in self.evidence]
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "ProvenanceEntry") -> ProvenanceEntry:
        """Decode a wire payload into a ProvenanceEntry.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_actor_id = _decode_str(_require_field(mapping, "actor_id", path), f"{path}.actor_id")
        field_actor_kind = _decode_str(
            _require_field(mapping, "actor_kind", path),
            f"{path}.actor_kind",
        )
        field_action = _decode_str(_require_field(mapping, "action", path), f"{path}.action")
        field_occurred_at = _decode_str(
            _require_field(mapping, "occurred_at", path),
            f"{path}.occurred_at",
        )
        field_evidence: tuple[EvidenceReference, ...] | None = None
        if "evidence" in mapping:
            raw_evidence = mapping["evidence"]
            if raw_evidence is None:
                raise ContractDecodeError(
                    f"{path}.evidence: null is not a valid value"
                )
            field_evidence_items = _decode_sequence(raw_evidence, f"{path}.evidence")
            field_evidence = tuple(
                EvidenceReference.from_wire(item, f"{path}.evidence[{index}]")
                for index, item in enumerate(field_evidence_items)
            )
        return cls(
            actor_id=field_actor_id,
            actor_kind=field_actor_kind,
            action=field_action,
            occurred_at=field_occurred_at,
            evidence=field_evidence,
        )


@dataclass(frozen=True, slots=True)
class ResponseMetadata:
    """Operation-independent response metadata. Present on both success and error responses so a
    caller can always negotiate versions and read its own authority, even when the operation
    failed.
    """

    request_id: RequestId
    correlation_id: CorrelationId
    version: VersionCapabilityEnvelope
    authority: GrantedAuthority
    page: PageMetadata | None = None
    job: JobReference | None = None
    freshness: ProjectionFreshness | None = None
    canonical_resolution_time: Timestamp | None = None
    warnings: tuple[Warning, ...] | None = None
    omissions: tuple[Omission, ...] | None = None
    partial: PartialResult | None = None
    audit_reference: AuditReference | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["request_id"] = self.request_id
        wire["correlation_id"] = self.correlation_id
        wire["version"] = self.version.to_wire()
        wire["authority"] = self.authority.to_wire()
        if self.page is not None:
            wire["page"] = self.page.to_wire()
        if self.job is not None:
            wire["job"] = self.job.to_wire()
        if self.freshness is not None:
            wire["freshness"] = self.freshness.to_wire()
        if self.canonical_resolution_time is not None:
            wire["canonical_resolution_time"] = self.canonical_resolution_time
        if self.warnings is not None:
            wire["warnings"] = [item.to_wire() for item in self.warnings]
        if self.omissions is not None:
            wire["omissions"] = [item.to_wire() for item in self.omissions]
        if self.partial is not None:
            wire["partial"] = self.partial.to_wire()
        if self.audit_reference is not None:
            wire["audit_reference"] = self.audit_reference
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "ResponseMetadata") -> ResponseMetadata:
        """Decode a wire payload into a ResponseMetadata.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_request_id = _decode_str(
            _require_field(mapping, "request_id", path),
            f"{path}.request_id",
        )
        field_correlation_id = _decode_str(
            _require_field(mapping, "correlation_id", path),
            f"{path}.correlation_id",
        )
        field_version = VersionCapabilityEnvelope.from_wire(
            _require_field(mapping, "version", path),
            f"{path}.version",
        )
        field_authority = GrantedAuthority.from_wire(
            _require_field(mapping, "authority", path),
            f"{path}.authority",
        )
        field_page: PageMetadata | None = None
        if "page" in mapping:
            raw_page = mapping["page"]
            if raw_page is None:
                raise ContractDecodeError(
                    f"{path}.page: null is not a valid value"
                )
            field_page = PageMetadata.from_wire(raw_page, f"{path}.page")
        field_job: JobReference | None = None
        if "job" in mapping:
            raw_job = mapping["job"]
            if raw_job is None:
                raise ContractDecodeError(
                    f"{path}.job: null is not a valid value"
                )
            field_job = JobReference.from_wire(raw_job, f"{path}.job")
        field_freshness: ProjectionFreshness | None = None
        if "freshness" in mapping:
            raw_freshness = mapping["freshness"]
            if raw_freshness is None:
                raise ContractDecodeError(
                    f"{path}.freshness: null is not a valid value"
                )
            field_freshness = ProjectionFreshness.from_wire(raw_freshness, f"{path}.freshness")
        field_canonical_resolution_time: Timestamp | None = None
        if "canonical_resolution_time" in mapping:
            raw_canonical_resolution_time = mapping["canonical_resolution_time"]
            if raw_canonical_resolution_time is None:
                raise ContractDecodeError(
                    f"{path}.canonical_resolution_time: null is not a valid value"
                )
            field_canonical_resolution_time = _decode_str(
                raw_canonical_resolution_time,
                f"{path}.canonical_resolution_time",
            )
        field_warnings: tuple[Warning, ...] | None = None
        if "warnings" in mapping:
            raw_warnings = mapping["warnings"]
            if raw_warnings is None:
                raise ContractDecodeError(
                    f"{path}.warnings: null is not a valid value"
                )
            field_warnings_items = _decode_sequence(raw_warnings, f"{path}.warnings")
            field_warnings = tuple(
                Warning.from_wire(item, f"{path}.warnings[{index}]")
                for index, item in enumerate(field_warnings_items)
            )
        field_omissions: tuple[Omission, ...] | None = None
        if "omissions" in mapping:
            raw_omissions = mapping["omissions"]
            if raw_omissions is None:
                raise ContractDecodeError(
                    f"{path}.omissions: null is not a valid value"
                )
            field_omissions_items = _decode_sequence(raw_omissions, f"{path}.omissions")
            field_omissions = tuple(
                Omission.from_wire(item, f"{path}.omissions[{index}]")
                for index, item in enumerate(field_omissions_items)
            )
        field_partial: PartialResult | None = None
        if "partial" in mapping:
            raw_partial = mapping["partial"]
            if raw_partial is None:
                raise ContractDecodeError(
                    f"{path}.partial: null is not a valid value"
                )
            field_partial = PartialResult.from_wire(raw_partial, f"{path}.partial")
        field_audit_reference: AuditReference | None = None
        if "audit_reference" in mapping:
            raw_audit_reference = mapping["audit_reference"]
            if raw_audit_reference is None:
                raise ContractDecodeError(
                    f"{path}.audit_reference: null is not a valid value"
                )
            field_audit_reference = _decode_str(raw_audit_reference, f"{path}.audit_reference")
        return cls(
            request_id=field_request_id,
            correlation_id=field_correlation_id,
            version=field_version,
            authority=field_authority,
            page=field_page,
            job=field_job,
            freshness=field_freshness,
            canonical_resolution_time=field_canonical_resolution_time,
            warnings=field_warnings,
            omissions=field_omissions,
            partial=field_partial,
            audit_reference=field_audit_reference,
        )


JobTerminalResult: TypeAlias = JobTerminalSuccess | JobTerminalFailure | JobTerminalCancellation
"""The final outcome of a job once it has reached a terminal state, with the complete attempt
history that led there. Exactly one of a success, a failure, or a cancellation, never a mix: each
branch closes its property set and carries a unique required discriminator (`result`, `error`, or
`cancellation`), so a payload combining or omitting all three matches no branch.
"""


def job_terminal_result_from_wire(
    payload: object, path: str = "JobTerminalResult"
) -> JobTerminalResult:
    """Decode a wire payload into exactly one JobTerminalResult branch.

    The branches are mutually exclusive by construction: a payload carrying more than one
    discriminator, or none at all, is rejected rather than guessed at.
    """
    mapping = _require_mapping(payload, path)
    discriminators = ("result", "error", "cancellation")
    matched = tuple(key for key in discriminators if key in mapping)
    if len(matched) != 1:
        raise ContractDecodeError(
            f"{path}: expected exactly one of {discriminators}, found {matched}"
        )
    if matched[0] == "result":
        return JobTerminalSuccess.from_wire(mapping, path)
    if matched[0] == "error":
        return JobTerminalFailure.from_wire(mapping, path)
    if matched[0] == "cancellation":
        return JobTerminalCancellation.from_wire(mapping, path)
    raise ContractDecodeError(f"{path}: unreachable discriminator state")


def job_terminal_result_to_wire(value: JobTerminalResult) -> dict[str, Any]:
    """Render one JobTerminalResult branch as a JSON-compatible mapping."""
    return value.to_wire()


@dataclass(frozen=True, slots=True)
class RecordProvenance:
    """The full provenance envelope for one record version: identity, temporal metadata, its
    authoring history, and the sources it draws on.
    """

    identity: RecordIdentity
    temporal: RecordTemporalMetadata
    history: tuple[ProvenanceEntry, ...]
    evidence_disposition: EvidenceDisposition
    sources: tuple[SourceReference, ...]

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["identity"] = self.identity.to_wire()
        wire["temporal"] = self.temporal.to_wire()
        wire["history"] = [item.to_wire() for item in self.history]
        wire["evidence_disposition"] = self.evidence_disposition
        wire["sources"] = [item.to_wire() for item in self.sources]
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "RecordProvenance") -> RecordProvenance:
        """Decode a wire payload into a RecordProvenance.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_identity = RecordIdentity.from_wire(
            _require_field(mapping, "identity", path),
            f"{path}.identity",
        )
        field_temporal = RecordTemporalMetadata.from_wire(
            _require_field(mapping, "temporal", path),
            f"{path}.temporal",
        )
        field_history_items = _decode_sequence(
            _require_field(mapping, "history", path),
            f"{path}.history",
        )
        field_history = tuple(
            ProvenanceEntry.from_wire(item, f"{path}.history[{index}]")
            for index, item in enumerate(field_history_items)
        )
        field_evidence_disposition = _decode_str(
            _require_field(mapping, "evidence_disposition", path),
            f"{path}.evidence_disposition",
        )
        field_sources_items = _decode_sequence(
            _require_field(mapping, "sources", path),
            f"{path}.sources",
        )
        field_sources = tuple(
            SourceReference.from_wire(item, f"{path}.sources[{index}]")
            for index, item in enumerate(field_sources_items)
        )
        return cls(
            identity=field_identity,
            temporal=field_temporal,
            history=field_history,
            evidence_disposition=field_evidence_disposition,
            sources=field_sources,
        )


@dataclass(frozen=True, slots=True)
class SuccessResponseEnvelope:
    """A successful response. Carries `result` and never `error`."""

    metadata: ResponseMetadata
    result: JsonObject

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["metadata"] = self.metadata.to_wire()
        wire["result"] = _encode_json_object(self.result)
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "SuccessResponseEnvelope"
    ) -> SuccessResponseEnvelope:
        """Decode a wire payload into a SuccessResponseEnvelope.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_metadata = ResponseMetadata.from_wire(
            _require_field(mapping, "metadata", path),
            f"{path}.metadata",
        )
        field_result = _decode_json_object(
            _require_field(mapping, "result", path),
            f"{path}.result",
        )
        return cls(
            metadata=field_metadata,
            result=field_result,
        )


@dataclass(frozen=True, slots=True)
class ErrorResponseEnvelope:
    """A failed response. Carries `error` and never `result`."""

    metadata: ResponseMetadata
    error: ApiError

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["metadata"] = self.metadata.to_wire()
        wire["error"] = self.error.to_wire()
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "ErrorResponseEnvelope"
    ) -> ErrorResponseEnvelope:
        """Decode a wire payload into a ErrorResponseEnvelope.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_metadata = ResponseMetadata.from_wire(
            _require_field(mapping, "metadata", path),
            f"{path}.metadata",
        )
        field_error = ApiError.from_wire(_require_field(mapping, "error", path), f"{path}.error")
        return cls(
            metadata=field_metadata,
            error=field_error,
        )


ResponseEnvelope: TypeAlias = SuccessResponseEnvelope | ErrorResponseEnvelope
"""Exactly one of a success or an error response, never both. Both branches close their property
set, so a document carrying `result` and `error` together matches neither branch and is invalid.
"""


def response_envelope_from_wire(
    payload: object, path: str = "ResponseEnvelope"
) -> ResponseEnvelope:
    """Decode a wire payload into exactly one ResponseEnvelope branch.

    The branches are mutually exclusive by construction: a payload carrying more than one
    discriminator, or none at all, is rejected rather than guessed at.
    """
    mapping = _require_mapping(payload, path)
    discriminators = ("result", "error")
    matched = tuple(key for key in discriminators if key in mapping)
    if len(matched) != 1:
        raise ContractDecodeError(
            f"{path}: expected exactly one of {discriminators}, found {matched}"
        )
    if matched[0] == "result":
        return SuccessResponseEnvelope.from_wire(mapping, path)
    if matched[0] == "error":
        return ErrorResponseEnvelope.from_wire(mapping, path)
    raise ContractDecodeError(f"{path}: unreachable discriminator state")


def response_envelope_to_wire(value: ResponseEnvelope) -> dict[str, Any]:
    """Render one ResponseEnvelope branch as a JSON-compatible mapping."""
    return value.to_wire()
