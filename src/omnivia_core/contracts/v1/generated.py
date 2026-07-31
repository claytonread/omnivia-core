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
#   contracts/application/v1/schemas/workspace.schema.json
#   contracts/application/v1/schemas/memory.schema.json
#   contracts/application/v1/schemas/evidence.schema.json
#   contracts/application/v1/schemas/knowledge.schema.json
#   contracts/application/v1/schemas/graph.schema.json
#   contracts/application/v1/schemas/context-pack.schema.json
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
    "CONTENT_CHECKSUM_PATTERN",
    "CONTEXT_PACK_DIGEST_PATTERN",
    "CONTEXT_PACK_MODE_PATTERN",
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
    "ERROR_CODE_INVALID_REQUEST",
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
    "EVIDENCE_CHECKSUM_PATTERN",
    "EVIDENCE_DISPOSITION_PATTERN",
    "EVIDENCE_ID_PATTERN",
    "FROZEN_ERROR_CODES",
    "FROZEN_RETRY_CLASSES",
    "GOVERNANCE_LAYER_PATTERN",
    "GOVERNANCE_STATE_PATTERN",
    "GOVERNED_RECORD_TYPE_PATTERN",
    "GOVERNED_RECORD_VIEW_PATTERN",
    "GRAPH_BOUNDARY_REASON_PATTERN",
    "GRAPH_DIRECTION_PATTERN",
    "GRAPH_ORDERING_BASIS_PATTERN",
    "GRAPH_RELATION_TYPE_PATTERN",
    "IDEMPOTENCY_KEY_PATTERN",
    "IDENTIFIER_PATTERN",
    "JOB_CANCELLATION_AVAILABILITY_PATTERN",
    "JOB_CANCELLATION_DISPOSITION_PATTERN",
    "JOB_PROGRESS_UNIT_PATTERN",
    "JOB_RECOVERY_AVAILABILITY_PATTERN",
    "JOB_RECOVERY_DISPOSITION_PATTERN",
    "JOB_STATE_PATTERN",
    "MEDIA_TYPE_PATTERN",
    "MEMORY_SEARCH_ORDER_PATTERN",
    "OPAQUE_TOKEN_PATTERN",
    "OPEN_CODE_PATTERN",
    "OPERATION_CATALOGUE",
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
    "RECORD_DOMAIN_SCOPE_PATTERN",
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
    "WORKSPACE_STATUS_PATTERN",
    "ApiError",
    "AuditReference",
    "CandidateApproveInput",
    "CandidateApproveResult",
    "CandidateAssertion",
    "CandidateExtractionMetadata",
    "CandidateRejectInput",
    "CandidateRejectResult",
    "CapabilityCompatibilityEntry",
    "CapabilityId",
    "CapabilityRef",
    "CapabilityRequirement",
    "CapabilitySet",
    "ClientIdentity",
    "CompatibilityMatrix",
    "CompatibilityMetadata",
    "ComponentKind",
    "ContentChecksum",
    "ContextPackAuthorizationContext",
    "ContextPackAuthorizedCandidate",
    "ContextPackAuthorizedCandidateSetManifest",
    "ContextPackAuthorizedEvidenceCandidate",
    "ContextPackAuthorizedRecordCandidate",
    "ContextPackBudget",
    "ContextPackBuildInput",
    "ContextPackBuildResult",
    "ContextPackCitation",
    "ContextPackConflict",
    "ContextPackDigest",
    "ContextPackEvidenceCitation",
    "ContextPackEvidenceReference",
    "ContextPackMode",
    "ContextPackNormalizedRequest",
    "ContextPackRecordCitation",
    "ContextPackReproducibility",
    "ContextPackSection",
    "ContextPackTokenBudget",
    "ContextPackTokenCount",
    "ContextPackUncertainty",
    "ContractDecodeError",
    "ContractVersion",
    "CorrelationId",
    "Deprecation",
    "DurationMs",
    "ErrorCode",
    "ErrorResponseEnvelope",
    "EvidenceArtifact",
    "EvidenceChecksum",
    "EvidenceDisposition",
    "EvidenceId",
    "EvidenceQuery",
    "EvidenceReference",
    "EvidenceSearchInput",
    "EvidenceSearchResult",
    "GovernanceLayer",
    "GovernanceRationale",
    "GovernanceState",
    "GovernedRecord",
    "GovernedRecordType",
    "GovernedRecordView",
    "GrantedAuthority",
    "GraphBoundaryReason",
    "GraphDepthLimit",
    "GraphDirection",
    "GraphEdge",
    "GraphNode",
    "GraphOrderingBasis",
    "GraphRelationType",
    "GraphTraversalInput",
    "GraphTraversalResult",
    "IdempotencyKey",
    "Identifier",
    "ImportCompletionResult",
    "ImportSourceDescriptor",
    "ImportStartInput",
    "ImportStartResult",
    "JobAttempt",
    "JobCancelInput",
    "JobCancelResult",
    "JobCancellationAvailability",
    "JobCancellationDisposition",
    "JobCancellationOutcome",
    "JobControl",
    "JobEvent",
    "JobEventsInput",
    "JobEventsResult",
    "JobGetInput",
    "JobGetResult",
    "JobHandle",
    "JobIdentity",
    "JobProgress",
    "JobProgressUnit",
    "JobRecoveryAvailability",
    "JobRecoveryDisposition",
    "JobReference",
    "JobRetryInput",
    "JobRetryResult",
    "JobState",
    "JobTerminalCancellation",
    "JobTerminalFailure",
    "JobTerminalResult",
    "JobTerminalSuccess",
    "JsonObject",
    "KnowledgeProposeInput",
    "KnowledgeProposeResult",
    "KnowledgeSearchInput",
    "KnowledgeSearchResult",
    "MediaType",
    "MemoryCreateInput",
    "MemoryCreateResult",
    "MemoryGetInput",
    "MemoryGetResult",
    "MemoryListInput",
    "MemoryListResult",
    "MemoryQuery",
    "MemorySearchInput",
    "MemorySearchOrder",
    "MemorySearchResult",
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
    "PageLimit",
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
    "RecordDomainScope",
    "RecordId",
    "RecordIdentity",
    "RecordProvenance",
    "RecordSupersedeInput",
    "RecordSupersedeResult",
    "RecordTemporalMetadata",
    "RecordVersion",
    "RecordVersionReference",
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
    "WorkspaceCompatibility",
    "WorkspaceCreateInput",
    "WorkspaceCreateResult",
    "WorkspaceDescriptor",
    "WorkspaceId",
    "WorkspaceInspectInput",
    "WorkspaceInspectResult",
    "WorkspaceListInput",
    "WorkspaceListResult",
    "WorkspaceStatus",
    "context_pack_authorized_candidate_from_wire",
    "context_pack_authorized_candidate_to_wire",
    "context_pack_citation_from_wire",
    "context_pack_citation_to_wire",
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


def _decode_number(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractDecodeError(f"{path}: expected a number, got {type(value).__name__}")
    if isinstance(value, float) and not isfinite(value):
        raise ContractDecodeError(f"{path}: {value!r} is not representable in JSON")
    try:
        return float(value)
    except OverflowError as error:
        raise ContractDecodeError(
            f"{path}: {value!r} is too large to represent as a JSON number"
        ) from error


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

CONTRACT_VERSION: Final = "1.2"
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
ERROR_CODE_INVALID_REQUEST: Final = "invalid_request"
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
    ERROR_CODE_INVALID_REQUEST,
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
        ERROR_CODE_INVALID_REQUEST: RETRY_CLASS_NON_RETRYABLE,
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
OPAQUE_TOKEN_PATTERN: Final = '^[!-~]+$(?![\\s\\S])'
IDEMPOTENCY_KEY_PATTERN: Final = '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
TIMESTAMP_PATTERN: Final = (
    '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:'
    '[0-9]{2}:[0-9]{2}(?:\\.[0-9]{1,9})?Z$'
)
PROJECTION_VERSION_PATTERN: Final = '^[!-~]+$'
OPERATION_COMPATIBILITY_STATE_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
QUALIFICATION_STATE_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
COMPONENT_KIND_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
CONTEXT_PACK_MODE_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
CONTEXT_PACK_DIGEST_PATTERN: Final = '^sha256:[0-9a-f]{64}$'
OPERATION_NAME_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)+$'
ERROR_CODE_PATTERN: Final = '^[a-z][a-z0-9_]*$'
RETRY_CLASS_PATTERN: Final = '^[a-z][a-z0-9_]*$'
EVIDENCE_ID_PATTERN: Final = '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
EVIDENCE_CHECKSUM_PATTERN: Final = '^[a-z][a-z0-9_]*:[A-Za-z0-9+/=_-]+$'
MEDIA_TYPE_PATTERN: Final = '^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$'
GRAPH_DIRECTION_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
GRAPH_RELATION_TYPE_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
GRAPH_ORDERING_BASIS_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
GRAPH_BOUNDARY_REASON_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
JOB_STATE_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
JOB_PROGRESS_UNIT_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
JOB_CANCELLATION_AVAILABILITY_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
JOB_RECOVERY_AVAILABILITY_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
JOB_CANCELLATION_DISPOSITION_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
JOB_RECOVERY_DISPOSITION_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
CONTENT_CHECKSUM_PATTERN: Final = '^sha256:[0-9a-f]{64}$'
GOVERNED_RECORD_TYPE_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
GOVERNED_RECORD_VIEW_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
RECORD_DOMAIN_SCOPE_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
MEMORY_SEARCH_ORDER_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'
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
WORKSPACE_STATUS_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*$'


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
it. The pattern's trailing negative lookahead is an end-of-input assertion, not a widening of the
character domain: a bare `$` matches before a final line terminator in some conforming regex
engines, so a token spelled with a trailing newline would be schema-valid while the semantic
validators -- which match the whole string -- refuse it. The lookahead pins the anchor to
absolute end of input, so strict schema and semantic validation accept exactly the same tokens.
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
"""An opaque JSON object. The envelope carries domain payloads without inspecting them, which is a
statement about the envelope rather than about the payload: an operation's `input` and `result`
are each bound to their own definition by `operations.schema.json`'s `x-omnivia-operation-
catalogue` (`input_schema_ref` and `result_schema_ref`), and validating a payload against that
binding is a separate step from decoding the envelope carrying it.
"""

PageLimit: TypeAlias = int
"""A bounded positive page size a caller requests for a paginated read."""

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

ContextPackMode: TypeAlias = str
"""Open, dot-namespaced code naming how a Context Pack was produced. Wire-open by shape so a
compatible minor release can add vocabulary, but trust-sensitive: v1 recognizes exactly one
value, `deterministic_view` (a regenerated, non-persisted, deterministic view), and semantic
validation fails closed on every other value rather than guessing at it. `immutable_snapshot` is
deliberately not a v1 mode -- persistence is an operation posture this read does not have -- and
`returned_artifact` was never a wire mode at all.
"""

ContextPackTokenCount: TypeAlias = int
"""A bounded, non-negative count of tokens actually observed: the tokens one section's model-facing
content occupies, or the total a whole pack consumed. Distinct from `ContextPackTokenBudget`,
which is what a caller asked for: zero is a meaningful observation (an empty pack consumed
nothing) but never a meaningful request.
"""

ContextPackTokenBudget: TypeAlias = int
"""A bounded, strictly positive token budget a caller asks a pack to be built against. Zero is
excluded rather than merely discouraged: a pack built against no budget at all can carry no
content, so a zero budget states a request no build could usefully answer.
"""

ContextPackDigest: TypeAlias = str
"""A SHA-256 content digest, spelled `sha256:` followed by exactly 64 lowercase hexadecimal
characters. Deliberately narrower than the general `EvidenceChecksum`: this is not an opaque
server token a client round-trips but a value an independent implementation must be able to
recompute and compare byte for byte, so exactly one algorithm, one length, and one letter case
are admitted.
"""

OperationName: TypeAlias = str
"""Dot-namespaced operation identifier such as `memory.get`. The name is all this shape states; what
each name binds to -- its input and result schemas, and its scope, capability, completion,
pagination, idempotency, mutation-precondition, audit and allowed-error posture -- is published
per operation by `operations.schema.json`'s `x-omnivia-operation-catalogue`. The pattern admits
any well-formed name, including ones no catalogue entry defines: whether a name is a v1
application operation is a semantic question (see
`omnivia_core.contracts.v1.semantics_operations`), not a wire-shape one.
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

EvidenceId: TypeAlias = str
"""Stable identifier of one L0 evidence artifact, constant across its append-only provenance
history. Distinct from `RecordId`: an evidence artifact is never itself a governed record.
"""

EvidenceQuery: TypeAlias = str
"""A caller-supplied, normalized search query for `evidence.search`. Normalization (case-folding,
whitespace, tokenization) is caller-side; this document defines no normalization algorithm.
"""

EvidenceChecksum: TypeAlias = str
"""A content checksum, spelled `algorithm:hex-digest` (such as `sha256:9f86d0...`) so the digest is
never ambiguous about which algorithm produced it. Provider-neutral: this contract does not
mandate a specific algorithm.
"""

MediaType: TypeAlias = str
"""An IANA-style `type/subtype` media type string, such as `text/plain` or `application/json`."""

GraphDirection: TypeAlias = str
"""Open, dot-namespaced code naming which direction a traversal follows relations in: `outbound`,
`inbound`, or `both`. Wire-open by shape, but trust-sensitive: only the known values are accepted
by semantic validation, and an unrecognized value fails closed rather than being guessed at.
"""

GraphRelationType: TypeAlias = str
"""Open, dot-namespaced code naming a kind of relation between governed records, such as
`relates_to` or `derived_from`. Open by design so a compatible minor release can add relation
types without breaking existing decoders.
"""

GraphDepthLimit: TypeAlias = int
"""A bounded traversal depth a caller may request, or the server states it actually applied. Zero
means the seeds themselves with no traversal beyond them; absent on input means the server's
default depth of 1.
"""

GraphOrderingBasis: TypeAlias = str
"""Open, dot-namespaced code naming the deterministic key a traversal result was ordered by, such as
`record_id_asc`, so identical inputs against an unchanged projection reproduce identical
node/edge ordering.
"""

GraphBoundaryReason: TypeAlias = str
"""Open, dot-namespaced code justifying why one endpoint of an edge is absent from a traversal
result: `page_boundary` when the traversal stopped at the node limit and offers a continuation
token, or `depth_boundary` when the present endpoint sits exactly at the applied depth limit.
Wire-open by shape, but trust-sensitive: an absent endpoint is a claim that the projection
stopped, not that the relation lost an end, so only the recognized values are accepted by
semantic validation and an unrecognized reason fails closed rather than being guessed at.
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

JobCancellationAvailability: TypeAlias = str
"""Open, dot-namespaced code naming, on a `JobHandle`, whether this job may be cancelled right now
and where an already-requested cancellation stands, with four known values: `cancellable` (a
`job.cancel` would be accepted), `cancellation_pending` (a cancellation is already requested and
has not yet taken effect), `cancelled` (the job is already cancelled), and `not_cancellable` (a
`job.cancel` would be refused). This is an availability statement about the job as observed, not
the outcome of a control call: what a particular `job.cancel` did is reported by
`JobCancellationDisposition`. Open by design; an unrecognized value decodes and is preserved but
never implies cancellation is permitted, and carries no scheduler, worker, lease, or persistence
detail.
"""

JobRecoveryAvailability: TypeAlias = str
"""Open, dot-namespaced code naming, on a `JobHandle`, whether this job may be recovered right now,
with three known values: `retryable` (a failed job that `job.retry` would run again), `resumable`
(a cancelled job that `job.retry` would continue from its checkpoint), and `not_retryable` (a
`job.retry` would be refused). `job.retry` is the single recovery operation and carries no action
selector, so this code reports which recovery server state would choose rather than offering the
caller a choice; what a particular `job.retry` did is reported by `JobRecoveryDisposition`. Open
by design; an unrecognized value decodes and is preserved but never implies recovery is
permitted, and carries no scheduler, worker, lease, checkpoint, or persistence detail.
"""

JobCancellationDisposition: TypeAlias = str
"""Open, dot-namespaced code naming what one `job.cancel` call actually did, with three known
values: `cancellation_requested` (cancellation was accepted and the job will stop), `cancelled`
(the job is already cancelled, so the call changed nothing), and `not_cancellable` (the call was
refused and the job is unchanged). A state-based refusal is a successful, idempotent control
result rather than an API error: `not_cancellable` is returned alongside the current unchanged
handle, not raised as `conflict`. Open by design; an unrecognized value decodes and is preserved
but never implies cancellation was accepted, and carries no scheduler, worker, lease, or
persistence detail.
"""

JobRecoveryDisposition: TypeAlias = str
"""Open, dot-namespaced code naming what one `job.retry` call actually did, with three known values:
`retry_scheduled` (a failed job was scheduled to run again, from the beginning or from a
supported checkpoint), `resume_scheduled` (a cancelled resumable job was scheduled to continue
from its checkpoint), and `not_retryable` (no recovery was scheduled and the job is unchanged).
`job.retry` is the single recovery operation and carries no action selector: server state, not
the caller, decides between retrying and resuming, so this code reports that decision rather than
accepting it. A state-based refusal is a successful, idempotent control result rather than an API
error. Open by design; an unrecognized value decodes and is preserved but never implies recovery
was accepted, and carries no scheduler, worker, lease, checkpoint, or persistence detail.
"""

ContentChecksum: TypeAlias = str
"""A SHA-256 content digest, spelled `sha256:` followed by exactly 64 lowercase hexadecimal
characters. Deliberately narrower than the general `EvidenceChecksum`: this is not an opaque
server token a client round-trips but a value the caller and the server must be able to recompute
and compare byte for byte over the same staged bytes, so exactly one algorithm, one length, and
one letter case are admitted. Stated as what v1 initially requires: admitting a further algorithm
later is an additive widening of this pattern, not a redefinition of what a checksum means.
"""

GovernedRecordType: TypeAlias = str
"""Open, dot-namespaced code naming what kind of governed record this is, such as `memory.fact` or
`memory.entity` or `memory.relation`. Open by design so a compatible minor release can add record
types without breaking existing decoders.
"""

GovernedRecordView: TypeAlias = str
"""Open, dot-namespaced code selecting which slice of a governed record's versions a read considers:
`current_canonical` (the single active accepted version, the default when this field is absent),
`candidates` (proposed/candidate versions not yet accepted), or `history` (every version,
including superseded ones). Open by design so a compatible minor release can add views without
breaking existing decoders. Default resolution when absent is a semantic concern (see
`omnivia_core.contracts.v1.semantics`), not a wire-shape one.
"""

RecordDomainScope: TypeAlias = str
"""Open, bounded, non-empty, dot-namespaced record classification stating what domain a governed
record belongs to, such as `personal.preferences` or `project.roadmap`. Distinct from the caller-
authorization `Scope` vocabulary (e.g. `memory:read`): a domain scope never grants or checks a
permission, it only classifies what the record is about. Open by design so a compatible minor
release can add classifications without breaking existing decoders.
"""

MemoryQuery: TypeAlias = str
"""A caller-supplied, normalized search query for `memory.search`. Normalization (case-folding,
whitespace, tokenization) is caller-side; this document defines no normalization algorithm.
"""

MemorySearchOrder: TypeAlias = str
"""Open, dot-namespaced code naming how `memory.search` results are ordered, such as `relevance` or
`recency`. Open by design so a compatible minor release can add orders without breaking existing
decoders.
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
    """How this operation may safely be retried. The three fields are not independent:
    `required` entails `supports_idempotency_key` (an operation cannot demand a key it does
    not honour), `safe_to_retry` excludes `required` (a request that is safe to repeat
    without a key cannot also be rejected for lacking one), and an operation that does not
    support keys cannot require them. A combination breaking any of those is a metadata
    statement no implementation can satisfy.
    """

    supports_idempotency_key: bool
    required: bool
    safe_to_retry: bool

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["supports_idempotency_key"] = self.supports_idempotency_key
        wire["required"] = self.required
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
        field_required = _decode_bool(_require_field(mapping, "required", path), f"{path}.required")
        field_safe_to_retry = _decode_bool(
            _require_field(mapping, "safe_to_retry", path),
            f"{path}.safe_to_retry",
        )
        return cls(
            supports_idempotency_key=field_supports_idempotency_key,
            required=field_required,
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

WorkspaceStatus: TypeAlias = str
"""Open, dot-namespaced code naming a workspace's lifecycle status, such as `active` or
`provisioning` or `archived`. Open by design so a compatible minor release can add statuses
without breaking existing decoders.
"""

@dataclass(frozen=True, slots=True)
class WorkspaceCreateInput:
    """Input for `workspace.create`. Installation-scoped: carries only installation-level
    creation data, and never a caller-supplied workspace identifier.
    """

    display_name: str

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["display_name"] = self.display_name
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "WorkspaceCreateInput") -> WorkspaceCreateInput:
        """Decode a wire payload into a WorkspaceCreateInput.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_display_name = _decode_str(
            _require_field(mapping, "display_name", path),
            f"{path}.display_name",
        )
        return cls(
            display_name=field_display_name,
        )


@dataclass(frozen=True, slots=True)
class WorkspaceInspectInput:
    """Input for `workspace.inspect`. Workspace-scoped: the workspace to inspect is the request
    envelope's selected workspace; this payload never carries a second, independent workspace
    identifier.
    """


    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "WorkspaceInspectInput"
    ) -> WorkspaceInspectInput:
        """Decode a wire payload into a WorkspaceInspectInput.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        _require_mapping(payload, path)
        return cls(
        )


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
    """Staleness statement for reads served from a projection rather than the write model. Every
    projection this read was served from must be named in both `projection_versions` and
    `projection_watermarks`: the two maps are one statement about the same set of
    projections, so their key sets are required to be identical and neither may be empty. A
    read served from no named projection cannot state its own staleness, and a projection
    that states a version but no watermark (or the reverse) leaves the caller unable to tell
    how far behind the write model it actually is.
    """

    as_of: Timestamp
    projection_versions: Mapping[str, ProjectionVersion]
    projection_watermarks: Mapping[str, ProjectionVersion]
    stale: bool

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["as_of"] = self.as_of
        wire["projection_versions"] = dict(self.projection_versions)
        wire["projection_watermarks"] = dict(self.projection_watermarks)
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
        field_projection_watermarks_entries = _require_mapping(
            _require_field(mapping, "projection_watermarks", path),
            f"{path}.projection_watermarks",
        )
        field_projection_watermarks = MappingProxyType(
            {
                key: _decode_str(value, f"{path}.projection_watermarks.{key}")
                for key, value in field_projection_watermarks_entries.items()
            }
        )
        field_stale = _decode_bool(_require_field(mapping, "stale", path), f"{path}.stale")
        return cls(
            as_of=field_as_of,
            projection_versions=field_projection_versions,
            projection_watermarks=field_projection_watermarks,
            stale=field_stale,
        )


@dataclass(frozen=True, slots=True)
class PageMetadata:
    """A pagination position. Direction-neutral: the same shape is read differently on a request
    than on a result, and neither reading is the other's default. On a request, an absent
    `page` asks for the first page, and a present `page` must actually name a continuation
    token -- `{}` states nothing to continue from and is invalid. On a result, `page` is
    always present and states the position this read reached: a continuation token means more
    remains, and `{}` means the read is exhausted. Exhaustion is therefore stated, never
    implied by an absent field -- one spelling on every paginated result, so a caller never
    has to know which result type it is holding to know what 'no next page' looks like. Token
    issuance, encoding, expiry, and the bindings a token proves are deliberately out of scope
    here; a token is opaque, and a reader that needs to prove what one was bound to takes
    that binding as separate trusted input rather than parsing the token.
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
    """Reference to asynchronous work started by an operation. The reference carries the
    identifier only, deliberately: the job's own lifecycle -- its states, progress, attempts,
    events, cancellation and retry -- is published separately in `jobs.schema.json`, and is
    read and controlled through the `job.get`, `job.events`, `job.cancel` and `job.retry`
    operations rather than by widening the handle a response hands back.
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
class ContextPackEvidenceReference:
    """A precise pointer to one exact L0 evidence artifact: which artifact, and the content
    checksum that artifact carried. Both are required, so the pointer names a specific
    immutable content state rather than whatever the identifier resolves to later. Distinct
    from `records.EvidenceReference`, which points at a source a record drew on rather than
    at a captured L0 artifact.
    """

    evidence_id: EvidenceId
    content_checksum: EvidenceChecksum

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["evidence_id"] = self.evidence_id
        wire["content_checksum"] = self.content_checksum
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "ContextPackEvidenceReference"
    ) -> ContextPackEvidenceReference:
        """Decode a wire payload into a ContextPackEvidenceReference.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_evidence_id = _decode_str(
            _require_field(mapping, "evidence_id", path),
            f"{path}.evidence_id",
        )
        field_content_checksum = _decode_str(
            _require_field(mapping, "content_checksum", path),
            f"{path}.content_checksum",
        )
        return cls(
            evidence_id=field_evidence_id,
            content_checksum=field_content_checksum,
        )


@dataclass(frozen=True, slots=True)
class ContextPackSection:
    """One model-facing section of a Context Pack: its identity, what kind of section it is, its
    content, the citations that content rests on, and the tokens that content occupies. Every
    section is substantiated: `citation_ids` is never empty, so no part of a pack's model-
    facing content is unattributable.
    """

    section_id: Identifier
    kind: OpenCode
    content: str
    citation_ids: tuple[Identifier, ...]
    token_count: ContextPackTokenCount
    title: str | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["section_id"] = self.section_id
        wire["kind"] = self.kind
        if self.title is not None:
            wire["title"] = self.title
        wire["content"] = self.content
        wire["citation_ids"] = list(self.citation_ids)
        wire["token_count"] = self.token_count
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "ContextPackSection") -> ContextPackSection:
        """Decode a wire payload into a ContextPackSection.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_section_id = _decode_str(
            _require_field(mapping, "section_id", path),
            f"{path}.section_id",
        )
        field_kind = _decode_str(_require_field(mapping, "kind", path), f"{path}.kind")
        field_title: str | None = None
        if "title" in mapping:
            raw_title = mapping["title"]
            if raw_title is None:
                raise ContractDecodeError(
                    f"{path}.title: null is not a valid value"
                )
            field_title = _decode_str(raw_title, f"{path}.title")
        field_content = _decode_str(_require_field(mapping, "content", path), f"{path}.content")
        field_citation_ids_items = _decode_sequence(
            _require_field(mapping, "citation_ids", path),
            f"{path}.citation_ids",
        )
        field_citation_ids = tuple(
            _decode_str(item, f"{path}.citation_ids[{index}]")
            for index, item in enumerate(field_citation_ids_items)
        )
        field_token_count = _decode_int(
            _require_field(mapping, "token_count", path),
            f"{path}.token_count",
        )
        return cls(
            section_id=field_section_id,
            kind=field_kind,
            title=field_title,
            content=field_content,
            citation_ids=field_citation_ids,
            token_count=field_token_count,
        )


@dataclass(frozen=True, slots=True)
class ContextPackConflict:
    """A stated conflict between two or more citations this pack returned, which the pack
    surfaces rather than resolving on the caller's behalf. Stated in citation identifiers
    rather than record references so evidence and governed records are addressed by the one
    reference system this pack already publishes, instead of a second, competing one that
    could name something the pack never cited.
    """

    description: str
    conflicting_citation_ids: tuple[Identifier, ...]

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["description"] = self.description
        wire["conflicting_citation_ids"] = list(self.conflicting_citation_ids)
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "ContextPackConflict") -> ContextPackConflict:
        """Decode a wire payload into a ContextPackConflict.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_description = _decode_str(
            _require_field(mapping, "description", path),
            f"{path}.description",
        )
        field_conflicting_citation_ids_items = _decode_sequence(
            _require_field(mapping, "conflicting_citation_ids", path),
            f"{path}.conflicting_citation_ids",
        )
        field_conflicting_citation_ids = tuple(
            _decode_str(item, f"{path}.conflicting_citation_ids[{index}]")
            for index, item in enumerate(field_conflicting_citation_ids_items)
        )
        return cls(
            description=field_description,
            conflicting_citation_ids=field_conflicting_citation_ids,
        )


@dataclass(frozen=True, slots=True)
class ContextPackUncertainty:
    """A stated uncertainty this pack surfaces rather than silently resolving or hiding,
    anchored to the citations it concerns.
    """

    description: str
    related_citation_ids: tuple[Identifier, ...]

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["description"] = self.description
        wire["related_citation_ids"] = list(self.related_citation_ids)
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "ContextPackUncertainty"
    ) -> ContextPackUncertainty:
        """Decode a wire payload into a ContextPackUncertainty.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_description = _decode_str(
            _require_field(mapping, "description", path),
            f"{path}.description",
        )
        field_related_citation_ids_items = _decode_sequence(
            _require_field(mapping, "related_citation_ids", path),
            f"{path}.related_citation_ids",
        )
        field_related_citation_ids = tuple(
            _decode_str(item, f"{path}.related_citation_ids[{index}]")
            for index, item in enumerate(field_related_citation_ids_items)
        )
        return cls(
            description=field_description,
            related_citation_ids=field_related_citation_ids,
        )


@dataclass(frozen=True, slots=True)
class ContextPackBudget:
    """Token budget accounting for one Context Pack: the positive budget it was built against,
    and the non-negative amount its sections actually consumed.
    """

    token_budget: ContextPackTokenBudget
    tokens_used: ContextPackTokenCount

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["token_budget"] = self.token_budget
        wire["tokens_used"] = self.tokens_used
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "ContextPackBudget") -> ContextPackBudget:
        """Decode a wire payload into a ContextPackBudget.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_token_budget = _decode_int(
            _require_field(mapping, "token_budget", path),
            f"{path}.token_budget",
        )
        field_tokens_used = _decode_int(
            _require_field(mapping, "tokens_used", path),
            f"{path}.tokens_used",
        )
        return cls(
            token_budget=field_token_budget,
            tokens_used=field_tokens_used,
        )


@dataclass(frozen=True, slots=True)
class ContextPackAuthorizedEvidenceCandidate:
    """One L0 evidence artifact on the authorized candidate frontier, named by immutable
    identity alone. Exactly three members and nothing else: the partition it was authorized
    in, the artifact, and the exact content state that artifact carried. Content, excerpts,
    provenance, spans, scores, distances, ranks, tie-breaks, selection flags, citations,
    sections, query and normalization state, and every authority, policy, configuration, or
    projection version are all deliberately absent -- a candidate-set digest must depend on
    which authorized material existed and on nothing a later ranking or selection step could
    change.
    """

    partition: str
    evidence_id: EvidenceId
    content_checksum: EvidenceChecksum

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["partition"] = self.partition
        wire["evidence_id"] = self.evidence_id
        wire["content_checksum"] = self.content_checksum
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "ContextPackAuthorizedEvidenceCandidate"
    ) -> ContextPackAuthorizedEvidenceCandidate:
        """Decode a wire payload into a ContextPackAuthorizedEvidenceCandidate.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_partition = _decode_str(
            _require_field(mapping, "partition", path),
            f"{path}.partition",
        )
        field_evidence_id = _decode_str(
            _require_field(mapping, "evidence_id", path),
            f"{path}.evidence_id",
        )
        field_content_checksum = _decode_str(
            _require_field(mapping, "content_checksum", path),
            f"{path}.content_checksum",
        )
        return cls(
            partition=field_partition,
            evidence_id=field_evidence_id,
            content_checksum=field_content_checksum,
        )


@dataclass(frozen=True, slots=True)
class ContextPackAuthorizedRecordCandidate:
    """One governed record version on the authorized candidate frontier, named by immutable
    identity alone. Exactly three members and nothing else: which governed partition it was
    authorized in, the record, and the version. The same exclusions
    `ContextPackAuthorizedEvidenceCandidate` states apply here for the same reason. Two
    different versions of one record are two independent candidates whenever both were
    independently eligible; the same version twice, in one partition or across two, is a
    contradiction rather than a set.
    """

    partition: str
    record_id: RecordId
    version: RecordVersion

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["partition"] = self.partition
        wire["record_id"] = self.record_id
        wire["version"] = self.version
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "ContextPackAuthorizedRecordCandidate"
    ) -> ContextPackAuthorizedRecordCandidate:
        """Decode a wire payload into a ContextPackAuthorizedRecordCandidate.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_partition = _decode_str(
            _require_field(mapping, "partition", path),
            f"{path}.partition",
        )
        field_record_id = _decode_str(
            _require_field(mapping, "record_id", path),
            f"{path}.record_id",
        )
        field_version = _decode_str(_require_field(mapping, "version", path), f"{path}.version")
        return cls(
            partition=field_partition,
            record_id=field_record_id,
            version=field_version,
        )


@dataclass(frozen=True, slots=True)
class ContextPackNormalizedRequest:
    """The exact normalized request one Context Pack was built from: the server-produced
    normalized query and the version of the normalization that produced it, the mode, the
    resolved record view, the token budget, and any selection filters. The single normalized
    form of a request -- the original caller query stays on the result's own `query` field,
    and nothing else restates it. Query normalization itself is server-owned and versioned:
    this contract requires the normalized query to be non-empty and pins which normalization
    produced it, and deliberately specifies no normalization algorithm of its own.
    """

    normalized_query: str
    mode: ContextPackMode
    view: GovernedRecordView
    token_budget: ContextPackTokenBudget
    normalization_version: Identifier
    domain_scope: RecordDomainScope | None = None
    record_type: GovernedRecordType | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["normalized_query"] = self.normalized_query
        wire["mode"] = self.mode
        wire["view"] = self.view
        wire["token_budget"] = self.token_budget
        wire["normalization_version"] = self.normalization_version
        if self.domain_scope is not None:
            wire["domain_scope"] = self.domain_scope
        if self.record_type is not None:
            wire["record_type"] = self.record_type
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "ContextPackNormalizedRequest"
    ) -> ContextPackNormalizedRequest:
        """Decode a wire payload into a ContextPackNormalizedRequest.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_normalized_query = _decode_str(
            _require_field(mapping, "normalized_query", path),
            f"{path}.normalized_query",
        )
        field_mode = _decode_str(_require_field(mapping, "mode", path), f"{path}.mode")
        field_view = _decode_str(_require_field(mapping, "view", path), f"{path}.view")
        field_token_budget = _decode_int(
            _require_field(mapping, "token_budget", path),
            f"{path}.token_budget",
        )
        field_normalization_version = _decode_str(
            _require_field(mapping, "normalization_version", path),
            f"{path}.normalization_version",
        )
        field_domain_scope: RecordDomainScope | None = None
        if "domain_scope" in mapping:
            raw_domain_scope = mapping["domain_scope"]
            if raw_domain_scope is None:
                raise ContractDecodeError(
                    f"{path}.domain_scope: null is not a valid value"
                )
            field_domain_scope = _decode_str(raw_domain_scope, f"{path}.domain_scope")
        field_record_type: GovernedRecordType | None = None
        if "record_type" in mapping:
            raw_record_type = mapping["record_type"]
            if raw_record_type is None:
                raise ContractDecodeError(
                    f"{path}.record_type: null is not a valid value"
                )
            field_record_type = _decode_str(raw_record_type, f"{path}.record_type")
        return cls(
            normalized_query=field_normalized_query,
            mode=field_mode,
            view=field_view,
            token_budget=field_token_budget,
            normalization_version=field_normalization_version,
            domain_scope=field_domain_scope,
            record_type=field_record_type,
        )


@dataclass(frozen=True, slots=True)
class ContextPackBuildInput:
    """Input for `context_pack.build`. Workspace-scoped: the workspace, principal, scopes, and
    purpose are the request envelope's; this payload never carries a second, independent copy
    of any of them, and selecting content never grants new authority beyond what the envelope
    already carries. Deliberately minimal: no view selector, no point-in-time selector, no
    pagination, and no persistence, expiry, retention, snapshot, or job control. The v1
    operation resolves the current canonical view synchronously and persists nothing, so none
    of those controls has a meaning here, and a payload that smuggles one in is rejected
    rather than ignored.
    """

    query: MemoryQuery
    mode: ContextPackMode
    token_budget: ContextPackTokenBudget
    domain_scope: RecordDomainScope | None = None
    record_type: GovernedRecordType | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["query"] = self.query
        wire["mode"] = self.mode
        wire["token_budget"] = self.token_budget
        if self.domain_scope is not None:
            wire["domain_scope"] = self.domain_scope
        if self.record_type is not None:
            wire["record_type"] = self.record_type
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "ContextPackBuildInput"
    ) -> ContextPackBuildInput:
        """Decode a wire payload into a ContextPackBuildInput.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_query = _decode_str(_require_field(mapping, "query", path), f"{path}.query")
        field_mode = _decode_str(_require_field(mapping, "mode", path), f"{path}.mode")
        field_token_budget = _decode_int(
            _require_field(mapping, "token_budget", path),
            f"{path}.token_budget",
        )
        field_domain_scope: RecordDomainScope | None = None
        if "domain_scope" in mapping:
            raw_domain_scope = mapping["domain_scope"]
            if raw_domain_scope is None:
                raise ContractDecodeError(
                    f"{path}.domain_scope: null is not a valid value"
                )
            field_domain_scope = _decode_str(raw_domain_scope, f"{path}.domain_scope")
        field_record_type: GovernedRecordType | None = None
        if "record_type" in mapping:
            raw_record_type = mapping["record_type"]
            if raw_record_type is None:
                raise ContractDecodeError(
                    f"{path}.record_type: null is not a valid value"
                )
            field_record_type = _decode_str(raw_record_type, f"{path}.record_type")
        return cls(
            query=field_query,
            mode=field_mode,
            token_budget=field_token_budget,
            domain_scope=field_domain_scope,
            record_type=field_record_type,
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
    """The control actions a caller may take on a job right now: cancellation and recovery.
    There are exactly two, because there are exactly two control operations -- `job.cancel`
    and `job.retry`. There is deliberately no `resume` member and no `job.resume` operation:
    retrying a failed job and resuming a cancelled resumable one are two readings of the same
    single recovery operation, chosen from server state rather than selected by the caller,
    so a separate resume disposition would have offered a control the contract does not have.
    Deliberately exposes only these caller-facing availabilities, never scheduler, worker,
    lease, checkpoint, or persistence detail.
    """

    cancellation: JobCancellationAvailability
    recovery: JobRecoveryAvailability

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["cancellation"] = self.cancellation
        wire["recovery"] = self.recovery
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
        field_recovery = _decode_str(_require_field(mapping, "recovery", path), f"{path}.recovery")
        return cls(
            cancellation=field_cancellation,
            recovery=field_recovery,
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
class ImportSourceDescriptor:
    """The immutable description of one already-staged import source. Provider-neutral by
    construction: it names a server-issued staging handle and the content facts that handle
    resolves to, and nothing about how the content got there or how it will be read. It
    carries no filesystem path, URL, inline archive, credential, connector configuration,
    parser implementation name, or runtime/storage option, so an import cannot be steered
    from the wire into reading something the server did not already stage. Immutable: the
    descriptor accepted by `import.start` is the exact descriptor the resulting
    `ImportCompletionResult` reports back, so what was imported is never in question after
    the fact.
    """

    staged_source_ref: OpaqueToken
    source_kind: OpenCode
    content_checksum: ContentChecksum
    content_length_bytes: int
    media_type: MediaType
    source_version: Identifier | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["staged_source_ref"] = self.staged_source_ref
        wire["source_kind"] = self.source_kind
        wire["content_checksum"] = self.content_checksum
        wire["content_length_bytes"] = self.content_length_bytes
        wire["media_type"] = self.media_type
        if self.source_version is not None:
            wire["source_version"] = self.source_version
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "ImportSourceDescriptor"
    ) -> ImportSourceDescriptor:
        """Decode a wire payload into a ImportSourceDescriptor.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_staged_source_ref = _decode_str(
            _require_field(mapping, "staged_source_ref", path),
            f"{path}.staged_source_ref",
        )
        field_source_kind = _decode_str(
            _require_field(mapping, "source_kind", path),
            f"{path}.source_kind",
        )
        field_content_checksum = _decode_str(
            _require_field(mapping, "content_checksum", path),
            f"{path}.content_checksum",
        )
        field_content_length_bytes = _decode_int(
            _require_field(mapping, "content_length_bytes", path),
            f"{path}.content_length_bytes",
        )
        field_media_type = _decode_str(
            _require_field(mapping, "media_type", path),
            f"{path}.media_type",
        )
        field_source_version: Identifier | None = None
        if "source_version" in mapping:
            raw_source_version = mapping["source_version"]
            if raw_source_version is None:
                raise ContractDecodeError(
                    f"{path}.source_version: null is not a valid value"
                )
            field_source_version = _decode_str(raw_source_version, f"{path}.source_version")
        return cls(
            staged_source_ref=field_staged_source_ref,
            source_kind=field_source_kind,
            content_checksum=field_content_checksum,
            content_length_bytes=field_content_length_bytes,
            media_type=field_media_type,
            source_version=field_source_version,
        )


@dataclass(frozen=True, slots=True)
class JobGetInput:
    """Input for `job.get`. Names one job. Workspace-scoped through the request envelope's
    selected workspace, so this payload never carries a second, independent workspace
    identifier.
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
    def from_wire(cls, payload: object, path: str = "JobGetInput") -> JobGetInput:
        """Decode a wire payload into a JobGetInput.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_job_id = _decode_str(_require_field(mapping, "job_id", path), f"{path}.job_id")
        return cls(
            job_id=field_job_id,
        )


@dataclass(frozen=True, slots=True)
class JobCancelInput:
    """Input for `job.cancel`. Names one job and, optionally, why. Workspace-scoped through the
    request envelope's selected workspace, so this payload never carries a second,
    independent workspace identifier.
    """

    job_id: OpaqueToken
    reason: OpenCode | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["job_id"] = self.job_id
        if self.reason is not None:
            wire["reason"] = self.reason
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "JobCancelInput") -> JobCancelInput:
        """Decode a wire payload into a JobCancelInput.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_job_id = _decode_str(_require_field(mapping, "job_id", path), f"{path}.job_id")
        field_reason: OpenCode | None = None
        if "reason" in mapping:
            raw_reason = mapping["reason"]
            if raw_reason is None:
                raise ContractDecodeError(
                    f"{path}.reason: null is not a valid value"
                )
            field_reason = _decode_str(raw_reason, f"{path}.reason")
        return cls(
            job_id=field_job_id,
            reason=field_reason,
        )


@dataclass(frozen=True, slots=True)
class JobRetryInput:
    """Input for `job.retry`, the single recovery operation. Names one job and nothing else:
    there is deliberately no action selector and no checkpoint reference, because whether
    recovery is a retry from the beginning or a resume from a supported checkpoint is chosen
    from server state, not requested by the caller. Workspace-scoped through the request
    envelope's selected workspace, so this payload never carries a second, independent
    workspace identifier.
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
    def from_wire(cls, payload: object, path: str = "JobRetryInput") -> JobRetryInput:
        """Decode a wire payload into a JobRetryInput.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_job_id = _decode_str(_require_field(mapping, "job_id", path), f"{path}.job_id")
        return cls(
            job_id=field_job_id,
        )


@dataclass(frozen=True, slots=True)
class GovernanceRationale:
    """A caller-supplied, auditable reason for a governance decision or transition: an open
    reason code plus an optional bounded human-readable comment. Carries no reviewer
    identity, authority level, or governance-state field of its own -- those are server-owned
    and never asserted through this shape.
    """

    reason_code: OpenCode
    comment: str | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["reason_code"] = self.reason_code
        if self.comment is not None:
            wire["comment"] = self.comment
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "GovernanceRationale") -> GovernanceRationale:
        """Decode a wire payload into a GovernanceRationale.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_reason_code = _decode_str(
            _require_field(mapping, "reason_code", path),
            f"{path}.reason_code",
        )
        field_comment: str | None = None
        if "comment" in mapping:
            raw_comment = mapping["comment"]
            if raw_comment is None:
                raise ContractDecodeError(
                    f"{path}.comment: null is not a valid value"
                )
            field_comment = _decode_str(raw_comment, f"{path}.comment")
        return cls(
            reason_code=field_reason_code,
            comment=field_comment,
        )


@dataclass(frozen=True, slots=True)
class MemoryGetInput:
    """Input for `memory.get`. Workspace-scoped: the workspace is the request envelope's
    selected workspace; this payload never carries a second, independent workspace
    identifier.
    """

    record_id: RecordId
    version: RecordVersion | None = None
    view: GovernedRecordView | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["record_id"] = self.record_id
        if self.version is not None:
            wire["version"] = self.version
        if self.view is not None:
            wire["view"] = self.view
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "MemoryGetInput") -> MemoryGetInput:
        """Decode a wire payload into a MemoryGetInput.

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
        field_view: GovernedRecordView | None = None
        if "view" in mapping:
            raw_view = mapping["view"]
            if raw_view is None:
                raise ContractDecodeError(
                    f"{path}.view: null is not a valid value"
                )
            field_view = _decode_str(raw_view, f"{path}.view")
        return cls(
            record_id=field_record_id,
            version=field_version,
            view=field_view,
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
    terminal_result_schema_ref: SchemaReference | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["completion_mode"] = self.completion_mode
        if self.job_kind is not None:
            wire["job_kind"] = self.job_kind
        if self.terminal_result_schema_ref is not None:
            wire["terminal_result_schema_ref"] = self.terminal_result_schema_ref
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
        field_terminal_result_schema_ref: SchemaReference | None = None
        if "terminal_result_schema_ref" in mapping:
            raw_terminal_result_schema_ref = mapping["terminal_result_schema_ref"]
            if raw_terminal_result_schema_ref is None:
                raise ContractDecodeError(
                    f"{path}.terminal_result_schema_ref: null is not a valid value"
                )
            field_terminal_result_schema_ref = _decode_str(
                raw_terminal_result_schema_ref,
                f"{path}.terminal_result_schema_ref",
            )
        return cls(
            completion_mode=field_completion_mode,
            job_kind=field_job_kind,
            terminal_result_schema_ref=field_terminal_result_schema_ref,
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
class CandidateExtractionMetadata:
    """Optional provenance about the automated extractor that produced a governed record's
    claim, when one did. Absent entirely for a claim a human asserted directly. Defined here
    rather than in `memory.schema.json` so `RecordProvenance` can preserve it without
    `records.schema.json` depending on a document that already depends on it.
    """

    extractor_id: Identifier
    extracted_at: Timestamp
    extractor_version: Identifier | None = None
    model_version: Identifier | None = None
    prompt_version: Identifier | None = None
    confidence: float | None = None
    reconciliation_state: OpenCode | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["extractor_id"] = self.extractor_id
        if self.extractor_version is not None:
            wire["extractor_version"] = self.extractor_version
        if self.model_version is not None:
            wire["model_version"] = self.model_version
        if self.prompt_version is not None:
            wire["prompt_version"] = self.prompt_version
        wire["extracted_at"] = self.extracted_at
        if self.confidence is not None:
            wire["confidence"] = self.confidence
        if self.reconciliation_state is not None:
            wire["reconciliation_state"] = self.reconciliation_state
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "CandidateExtractionMetadata"
    ) -> CandidateExtractionMetadata:
        """Decode a wire payload into a CandidateExtractionMetadata.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_extractor_id = _decode_str(
            _require_field(mapping, "extractor_id", path),
            f"{path}.extractor_id",
        )
        field_extractor_version: Identifier | None = None
        if "extractor_version" in mapping:
            raw_extractor_version = mapping["extractor_version"]
            if raw_extractor_version is None:
                raise ContractDecodeError(
                    f"{path}.extractor_version: null is not a valid value"
                )
            field_extractor_version = _decode_str(
                raw_extractor_version,
                f"{path}.extractor_version",
            )
        field_model_version: Identifier | None = None
        if "model_version" in mapping:
            raw_model_version = mapping["model_version"]
            if raw_model_version is None:
                raise ContractDecodeError(
                    f"{path}.model_version: null is not a valid value"
                )
            field_model_version = _decode_str(raw_model_version, f"{path}.model_version")
        field_prompt_version: Identifier | None = None
        if "prompt_version" in mapping:
            raw_prompt_version = mapping["prompt_version"]
            if raw_prompt_version is None:
                raise ContractDecodeError(
                    f"{path}.prompt_version: null is not a valid value"
                )
            field_prompt_version = _decode_str(raw_prompt_version, f"{path}.prompt_version")
        field_extracted_at = _decode_str(
            _require_field(mapping, "extracted_at", path),
            f"{path}.extracted_at",
        )
        field_confidence: float | None = None
        if "confidence" in mapping:
            raw_confidence = mapping["confidence"]
            if raw_confidence is None:
                raise ContractDecodeError(
                    f"{path}.confidence: null is not a valid value"
                )
            field_confidence = _decode_number(raw_confidence, f"{path}.confidence")
        field_reconciliation_state: OpenCode | None = None
        if "reconciliation_state" in mapping:
            raw_reconciliation_state = mapping["reconciliation_state"]
            if raw_reconciliation_state is None:
                raise ContractDecodeError(
                    f"{path}.reconciliation_state: null is not a valid value"
                )
            field_reconciliation_state = _decode_str(
                raw_reconciliation_state,
                f"{path}.reconciliation_state",
            )
        return cls(
            extractor_id=field_extractor_id,
            extractor_version=field_extractor_version,
            model_version=field_model_version,
            prompt_version=field_prompt_version,
            extracted_at=field_extracted_at,
            confidence=field_confidence,
            reconciliation_state=field_reconciliation_state,
        )


@dataclass(frozen=True, slots=True)
class RecordTemporalMetadata:
    """The distinct instants a governed record's lifecycle turns on: when the underlying fact
    occurred in the world, when it was observed, when the system ingested it, when this
    version was persisted, the window it is asserted valid for, and when it was superseded.
    """

    ingested_at: Timestamp
    recorded_at: Timestamp
    event_at: Timestamp | None = None
    observed_at: Timestamp | None = None
    valid_from: Timestamp | None = None
    valid_until: Timestamp | None = None
    superseded_at: Timestamp | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        if self.event_at is not None:
            wire["event_at"] = self.event_at
        if self.observed_at is not None:
            wire["observed_at"] = self.observed_at
        wire["ingested_at"] = self.ingested_at
        wire["recorded_at"] = self.recorded_at
        if self.valid_from is not None:
            wire["valid_from"] = self.valid_from
        if self.valid_until is not None:
            wire["valid_until"] = self.valid_until
        if self.superseded_at is not None:
            wire["superseded_at"] = self.superseded_at
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
        field_event_at: Timestamp | None = None
        if "event_at" in mapping:
            raw_event_at = mapping["event_at"]
            if raw_event_at is None:
                raise ContractDecodeError(
                    f"{path}.event_at: null is not a valid value"
                )
            field_event_at = _decode_str(raw_event_at, f"{path}.event_at")
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
        field_superseded_at: Timestamp | None = None
        if "superseded_at" in mapping:
            raw_superseded_at = mapping["superseded_at"]
            if raw_superseded_at is None:
                raise ContractDecodeError(
                    f"{path}.superseded_at: null is not a valid value"
                )
            field_superseded_at = _decode_str(raw_superseded_at, f"{path}.superseded_at")
        return cls(
            event_at=field_event_at,
            observed_at=field_observed_at,
            ingested_at=field_ingested_at,
            recorded_at=field_recorded_at,
            valid_from=field_valid_from,
            valid_until=field_valid_until,
            superseded_at=field_superseded_at,
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
class RecordVersionReference:
    """A precise, non-directional pointer to one exact record version: both `record_id` and
    `version` are always required. Distinct from `SupersessionReference`, which is direction-
    bearing (its meaning comes from which `RecordIdentity` field carries it) and whose
    `version` is optional. Used wherever a payload must name a specific existing record
    version -- a graph traversal start point or edge endpoint, a context pack citation or
    source-version list -- without asserting any supersession relationship.
    """

    record_id: RecordId
    version: RecordVersion

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["record_id"] = self.record_id
        wire["version"] = self.version
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "RecordVersionReference"
    ) -> RecordVersionReference:
        """Decode a wire payload into a RecordVersionReference.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_record_id = _decode_str(
            _require_field(mapping, "record_id", path),
            f"{path}.record_id",
        )
        field_version = _decode_str(_require_field(mapping, "version", path), f"{path}.version")
        return cls(
            record_id=field_record_id,
            version=field_version,
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
class ContextPackEvidenceCitation:
    """One citation binding a pack section to an exact L0 evidence artifact, optionally at a
    precise location inside it. Possessing this citation grants no access on its own:
    following it always requires fresh authorization against the cited evidence.
    """

    citation_id: Identifier
    evidence_reference: ContextPackEvidenceReference
    content_pointer: str | None = None
    source_span: SourceSpan | None = None
    excerpt: str | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["citation_id"] = self.citation_id
        wire["evidence_reference"] = self.evidence_reference.to_wire()
        if self.content_pointer is not None:
            wire["content_pointer"] = self.content_pointer
        if self.source_span is not None:
            wire["source_span"] = self.source_span.to_wire()
        if self.excerpt is not None:
            wire["excerpt"] = self.excerpt
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "ContextPackEvidenceCitation"
    ) -> ContextPackEvidenceCitation:
        """Decode a wire payload into a ContextPackEvidenceCitation.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_citation_id = _decode_str(
            _require_field(mapping, "citation_id", path),
            f"{path}.citation_id",
        )
        field_evidence_reference = ContextPackEvidenceReference.from_wire(
            _require_field(mapping, "evidence_reference", path),
            f"{path}.evidence_reference",
        )
        field_content_pointer: str | None = None
        if "content_pointer" in mapping:
            raw_content_pointer = mapping["content_pointer"]
            if raw_content_pointer is None:
                raise ContractDecodeError(
                    f"{path}.content_pointer: null is not a valid value"
                )
            field_content_pointer = _decode_str(raw_content_pointer, f"{path}.content_pointer")
        field_source_span: SourceSpan | None = None
        if "source_span" in mapping:
            raw_source_span = mapping["source_span"]
            if raw_source_span is None:
                raise ContractDecodeError(
                    f"{path}.source_span: null is not a valid value"
                )
            field_source_span = SourceSpan.from_wire(raw_source_span, f"{path}.source_span")
        field_excerpt: str | None = None
        if "excerpt" in mapping:
            raw_excerpt = mapping["excerpt"]
            if raw_excerpt is None:
                raise ContractDecodeError(
                    f"{path}.excerpt: null is not a valid value"
                )
            field_excerpt = _decode_str(raw_excerpt, f"{path}.excerpt")
        return cls(
            citation_id=field_citation_id,
            evidence_reference=field_evidence_reference,
            content_pointer=field_content_pointer,
            source_span=field_source_span,
            excerpt=field_excerpt,
        )


@dataclass(frozen=True, slots=True)
class ContextPackRecordCitation:
    """One citation binding a pack section to an exact governed record version, optionally at a
    precise location inside it. Possessing this citation grants no access on its own:
    following it always requires fresh authorization against the cited record.
    """

    citation_id: Identifier
    record_reference: RecordVersionReference
    content_pointer: str | None = None
    source_span: SourceSpan | None = None
    excerpt: str | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["citation_id"] = self.citation_id
        wire["record_reference"] = self.record_reference.to_wire()
        if self.content_pointer is not None:
            wire["content_pointer"] = self.content_pointer
        if self.source_span is not None:
            wire["source_span"] = self.source_span.to_wire()
        if self.excerpt is not None:
            wire["excerpt"] = self.excerpt
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "ContextPackRecordCitation"
    ) -> ContextPackRecordCitation:
        """Decode a wire payload into a ContextPackRecordCitation.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_citation_id = _decode_str(
            _require_field(mapping, "citation_id", path),
            f"{path}.citation_id",
        )
        field_record_reference = RecordVersionReference.from_wire(
            _require_field(mapping, "record_reference", path),
            f"{path}.record_reference",
        )
        field_content_pointer: str | None = None
        if "content_pointer" in mapping:
            raw_content_pointer = mapping["content_pointer"]
            if raw_content_pointer is None:
                raise ContractDecodeError(
                    f"{path}.content_pointer: null is not a valid value"
                )
            field_content_pointer = _decode_str(raw_content_pointer, f"{path}.content_pointer")
        field_source_span: SourceSpan | None = None
        if "source_span" in mapping:
            raw_source_span = mapping["source_span"]
            if raw_source_span is None:
                raise ContractDecodeError(
                    f"{path}.source_span: null is not a valid value"
                )
            field_source_span = SourceSpan.from_wire(raw_source_span, f"{path}.source_span")
        field_excerpt: str | None = None
        if "excerpt" in mapping:
            raw_excerpt = mapping["excerpt"]
            if raw_excerpt is None:
                raise ContractDecodeError(
                    f"{path}.excerpt: null is not a valid value"
                )
            field_excerpt = _decode_str(raw_excerpt, f"{path}.excerpt")
        return cls(
            citation_id=field_citation_id,
            record_reference=field_record_reference,
            content_pointer=field_content_pointer,
            source_span=field_source_span,
            excerpt=field_excerpt,
        )


ContextPackAuthorizedCandidate: TypeAlias = ContextPackAuthorizedEvidenceCandidate | ContextPackAuthorizedRecordCandidate
"""One member of the authorized candidate frontier: either an L0 evidence candidate or a governed
record candidate, never both and never neither. Two distinct object shapes rather than one shape
with optional pointers, so what a candidate names is settled structurally by the document instead
of by a later agreement check.
"""


def context_pack_authorized_candidate_from_wire(
    payload: object, path: str = "ContextPackAuthorizedCandidate"
) -> ContextPackAuthorizedCandidate:
    """Decode a wire payload into exactly one ContextPackAuthorizedCandidate branch.

    The branches are mutually exclusive by construction: a payload carrying more than one
    discriminator, or none at all, is rejected rather than guessed at.
    """
    mapping = _require_mapping(payload, path)
    discriminators = ("content_checksum", "record_id")
    matched = tuple(key for key in discriminators if key in mapping)
    if len(matched) != 1:
        raise ContractDecodeError(
            f"{path}: expected exactly one of {discriminators}, found {matched}"
        )
    if matched[0] == "content_checksum":
        return ContextPackAuthorizedEvidenceCandidate.from_wire(mapping, path)
    if matched[0] == "record_id":
        return ContextPackAuthorizedRecordCandidate.from_wire(mapping, path)
    raise ContractDecodeError(f"{path}: unreachable discriminator state")


def context_pack_authorized_candidate_to_wire(value: ContextPackAuthorizedCandidate) -> dict[str, Any]:
    """Render one ContextPackAuthorizedCandidate branch as a JSON-compatible mapping."""
    return value.to_wire()


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
    scopes: tuple[Scope, ...]
    purpose: Purpose
    required_capabilities: tuple[CapabilityRequirement, ...]
    workspace_id: WorkspaceId | None = None
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
        if self.workspace_id is not None:
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
        field_workspace_id: WorkspaceId | None = None
        if "workspace_id" in mapping:
            raw_workspace_id = mapping["workspace_id"]
            if raw_workspace_id is None:
                raise ContractDecodeError(
                    f"{path}.workspace_id: null is not a valid value"
                )
            field_workspace_id = _decode_str(raw_workspace_id, f"{path}.workspace_id")
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
class EvidenceSearchInput:
    """Input for `evidence.search`. Workspace-scoped: the workspace is the request envelope's
    selected workspace; this payload never carries a second, independent workspace
    identifier.
    """

    query: EvidenceQuery
    sensitivity: OpenCode | None = None
    include_tombstoned: bool | None = None
    limit: PageLimit | None = None
    page: PageMetadata | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["query"] = self.query
        if self.sensitivity is not None:
            wire["sensitivity"] = self.sensitivity
        if self.include_tombstoned is not None:
            wire["include_tombstoned"] = self.include_tombstoned
        if self.limit is not None:
            wire["limit"] = self.limit
        if self.page is not None:
            wire["page"] = self.page.to_wire()
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "EvidenceSearchInput") -> EvidenceSearchInput:
        """Decode a wire payload into a EvidenceSearchInput.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_query = _decode_str(_require_field(mapping, "query", path), f"{path}.query")
        field_sensitivity: OpenCode | None = None
        if "sensitivity" in mapping:
            raw_sensitivity = mapping["sensitivity"]
            if raw_sensitivity is None:
                raise ContractDecodeError(
                    f"{path}.sensitivity: null is not a valid value"
                )
            field_sensitivity = _decode_str(raw_sensitivity, f"{path}.sensitivity")
        field_include_tombstoned: bool | None = None
        if "include_tombstoned" in mapping:
            raw_include_tombstoned = mapping["include_tombstoned"]
            if raw_include_tombstoned is None:
                raise ContractDecodeError(
                    f"{path}.include_tombstoned: null is not a valid value"
                )
            field_include_tombstoned = _decode_bool(
                raw_include_tombstoned,
                f"{path}.include_tombstoned",
            )
        field_limit: PageLimit | None = None
        if "limit" in mapping:
            raw_limit = mapping["limit"]
            if raw_limit is None:
                raise ContractDecodeError(
                    f"{path}.limit: null is not a valid value"
                )
            field_limit = _decode_int(raw_limit, f"{path}.limit")
        field_page: PageMetadata | None = None
        if "page" in mapping:
            raw_page = mapping["page"]
            if raw_page is None:
                raise ContractDecodeError(
                    f"{path}.page: null is not a valid value"
                )
            field_page = PageMetadata.from_wire(raw_page, f"{path}.page")
        return cls(
            query=field_query,
            sensitivity=field_sensitivity,
            include_tombstoned=field_include_tombstoned,
            limit=field_limit,
            page=field_page,
        )


@dataclass(frozen=True, slots=True)
class GraphTraversalInput:
    """Input for `graph.traverse`. Workspace-scoped: the workspace is the request envelope's
    selected workspace; this payload never carries a second, independent workspace
    identifier. Absent `view` defaults to `current_canonical`; only an explicit `view`
    selector may request `candidates` or `history`. Absent `direction` defaults to
    `outbound`.
    """

    start: tuple[RecordVersionReference, ...]
    direction: GraphDirection | None = None
    relation_types: tuple[GraphRelationType, ...] | None = None
    domain_scope: RecordDomainScope | None = None
    view: GovernedRecordView | None = None
    as_of: Timestamp | None = None
    depth_limit: GraphDepthLimit | None = None
    node_limit: PageLimit | None = None
    edge_limit: PageLimit | None = None
    page: PageMetadata | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["start"] = [item.to_wire() for item in self.start]
        if self.direction is not None:
            wire["direction"] = self.direction
        if self.relation_types is not None:
            wire["relation_types"] = list(self.relation_types)
        if self.domain_scope is not None:
            wire["domain_scope"] = self.domain_scope
        if self.view is not None:
            wire["view"] = self.view
        if self.as_of is not None:
            wire["as_of"] = self.as_of
        if self.depth_limit is not None:
            wire["depth_limit"] = self.depth_limit
        if self.node_limit is not None:
            wire["node_limit"] = self.node_limit
        if self.edge_limit is not None:
            wire["edge_limit"] = self.edge_limit
        if self.page is not None:
            wire["page"] = self.page.to_wire()
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "GraphTraversalInput") -> GraphTraversalInput:
        """Decode a wire payload into a GraphTraversalInput.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_start_items = _decode_sequence(
            _require_field(mapping, "start", path),
            f"{path}.start",
        )
        field_start = tuple(
            RecordVersionReference.from_wire(item, f"{path}.start[{index}]")
            for index, item in enumerate(field_start_items)
        )
        field_direction: GraphDirection | None = None
        if "direction" in mapping:
            raw_direction = mapping["direction"]
            if raw_direction is None:
                raise ContractDecodeError(
                    f"{path}.direction: null is not a valid value"
                )
            field_direction = _decode_str(raw_direction, f"{path}.direction")
        field_relation_types: tuple[GraphRelationType, ...] | None = None
        if "relation_types" in mapping:
            raw_relation_types = mapping["relation_types"]
            if raw_relation_types is None:
                raise ContractDecodeError(
                    f"{path}.relation_types: null is not a valid value"
                )
            field_relation_types_items = _decode_sequence(
                raw_relation_types,
                f"{path}.relation_types",
            )
            field_relation_types = tuple(
                _decode_str(item, f"{path}.relation_types[{index}]")
                for index, item in enumerate(field_relation_types_items)
            )
        field_domain_scope: RecordDomainScope | None = None
        if "domain_scope" in mapping:
            raw_domain_scope = mapping["domain_scope"]
            if raw_domain_scope is None:
                raise ContractDecodeError(
                    f"{path}.domain_scope: null is not a valid value"
                )
            field_domain_scope = _decode_str(raw_domain_scope, f"{path}.domain_scope")
        field_view: GovernedRecordView | None = None
        if "view" in mapping:
            raw_view = mapping["view"]
            if raw_view is None:
                raise ContractDecodeError(
                    f"{path}.view: null is not a valid value"
                )
            field_view = _decode_str(raw_view, f"{path}.view")
        field_as_of: Timestamp | None = None
        if "as_of" in mapping:
            raw_as_of = mapping["as_of"]
            if raw_as_of is None:
                raise ContractDecodeError(
                    f"{path}.as_of: null is not a valid value"
                )
            field_as_of = _decode_str(raw_as_of, f"{path}.as_of")
        field_depth_limit: GraphDepthLimit | None = None
        if "depth_limit" in mapping:
            raw_depth_limit = mapping["depth_limit"]
            if raw_depth_limit is None:
                raise ContractDecodeError(
                    f"{path}.depth_limit: null is not a valid value"
                )
            field_depth_limit = _decode_int(raw_depth_limit, f"{path}.depth_limit")
        field_node_limit: PageLimit | None = None
        if "node_limit" in mapping:
            raw_node_limit = mapping["node_limit"]
            if raw_node_limit is None:
                raise ContractDecodeError(
                    f"{path}.node_limit: null is not a valid value"
                )
            field_node_limit = _decode_int(raw_node_limit, f"{path}.node_limit")
        field_edge_limit: PageLimit | None = None
        if "edge_limit" in mapping:
            raw_edge_limit = mapping["edge_limit"]
            if raw_edge_limit is None:
                raise ContractDecodeError(
                    f"{path}.edge_limit: null is not a valid value"
                )
            field_edge_limit = _decode_int(raw_edge_limit, f"{path}.edge_limit")
        field_page: PageMetadata | None = None
        if "page" in mapping:
            raw_page = mapping["page"]
            if raw_page is None:
                raise ContractDecodeError(
                    f"{path}.page: null is not a valid value"
                )
            field_page = PageMetadata.from_wire(raw_page, f"{path}.page")
        return cls(
            start=field_start,
            direction=field_direction,
            relation_types=field_relation_types,
            domain_scope=field_domain_scope,
            view=field_view,
            as_of=field_as_of,
            depth_limit=field_depth_limit,
            node_limit=field_node_limit,
            edge_limit=field_edge_limit,
            page=field_page,
        )


@dataclass(frozen=True, slots=True)
class JobAttempt:
    """One execution attempt of a job. A job that is retried has more than one attempt. An
    attempt exists because execution started, so `queued` is not an attempt state: waiting to
    run is a state of the *job*, not of an execution of it, and an attempt numbered against a
    job that never ran would make the attempt history unreadable. Within one job's history
    attempts are numbered `1..N` contiguously, never overlap, and only a `failed` or
    `cancelled` attempt may be followed by another one -- a `succeeded` attempt is final.
    """

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
class ImportStartInput:
    """Input for `import.start`. Carries exactly one thing: the immutable descriptor of an
    already-staged source. Workspace-scoped through the request envelope's selected
    workspace, so this payload never carries a second, independent workspace identifier, and
    it accepts no path, URL, inline archive, credential, parser implementation name, or
    runtime/storage option.
    """

    source: ImportSourceDescriptor

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["source"] = self.source.to_wire()
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "ImportStartInput") -> ImportStartInput:
        """Decode a wire payload into a ImportStartInput.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_source = ImportSourceDescriptor.from_wire(
            _require_field(mapping, "source", path),
            f"{path}.source",
        )
        return cls(
            source=field_source,
        )


@dataclass(frozen=True, slots=True)
class ImportCompletionResult:
    """The typed terminal result of a successful `ingestion.import` job: what was imported, and
    what it produced. It reports the creation of L0 evidence only -- candidate extraction and
    any governed record that later cites this evidence are separate, later operations, so
    nothing here asserts that knowledge was proposed, approved, or accepted.
    `discovered_items` is the total the import saw and must equal `evidence_records_created +
    skipped_items + failed_items`: an item is accounted for exactly once, and a count that
    does not add up is a report of an import nobody can audit. `partial` is not an
    independent claim either; it is exactly `failed_items > 0`. `source` is byte-for-byte the
    descriptor `import.start` accepted.
    """

    import_run_id: OpaqueToken
    source: ImportSourceDescriptor
    discovered_items: int
    evidence_records_created: int
    skipped_items: int
    failed_items: int
    partial: bool

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["import_run_id"] = self.import_run_id
        wire["source"] = self.source.to_wire()
        wire["discovered_items"] = self.discovered_items
        wire["evidence_records_created"] = self.evidence_records_created
        wire["skipped_items"] = self.skipped_items
        wire["failed_items"] = self.failed_items
        wire["partial"] = self.partial
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "ImportCompletionResult"
    ) -> ImportCompletionResult:
        """Decode a wire payload into a ImportCompletionResult.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_import_run_id = _decode_str(
            _require_field(mapping, "import_run_id", path),
            f"{path}.import_run_id",
        )
        field_source = ImportSourceDescriptor.from_wire(
            _require_field(mapping, "source", path),
            f"{path}.source",
        )
        field_discovered_items = _decode_int(
            _require_field(mapping, "discovered_items", path),
            f"{path}.discovered_items",
        )
        field_evidence_records_created = _decode_int(
            _require_field(mapping, "evidence_records_created", path),
            f"{path}.evidence_records_created",
        )
        field_skipped_items = _decode_int(
            _require_field(mapping, "skipped_items", path),
            f"{path}.skipped_items",
        )
        field_failed_items = _decode_int(
            _require_field(mapping, "failed_items", path),
            f"{path}.failed_items",
        )
        field_partial = _decode_bool(_require_field(mapping, "partial", path), f"{path}.partial")
        return cls(
            import_run_id=field_import_run_id,
            source=field_source,
            discovered_items=field_discovered_items,
            evidence_records_created=field_evidence_records_created,
            skipped_items=field_skipped_items,
            failed_items=field_failed_items,
            partial=field_partial,
        )


@dataclass(frozen=True, slots=True)
class JobEventsInput:
    """Input for `job.events`: a bounded, snapshot-stable read of one job's ordered event
    stream. A request carrying no `page` starts a new pagination session and captures the
    job's current event count as that session's snapshot; a request carrying one continues
    the session that token names and can never widen it. Transport-level streaming is out of
    scope: this is a paged read, not a subscription. Workspace-scoped through the request
    envelope's selected workspace, so this payload never carries a second, independent
    workspace identifier.
    """

    job_id: OpaqueToken
    limit: PageLimit | None = None
    page: PageMetadata | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["job_id"] = self.job_id
        if self.limit is not None:
            wire["limit"] = self.limit
        if self.page is not None:
            wire["page"] = self.page.to_wire()
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "JobEventsInput") -> JobEventsInput:
        """Decode a wire payload into a JobEventsInput.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_job_id = _decode_str(_require_field(mapping, "job_id", path), f"{path}.job_id")
        field_limit: PageLimit | None = None
        if "limit" in mapping:
            raw_limit = mapping["limit"]
            if raw_limit is None:
                raise ContractDecodeError(
                    f"{path}.limit: null is not a valid value"
                )
            field_limit = _decode_int(raw_limit, f"{path}.limit")
        field_page: PageMetadata | None = None
        if "page" in mapping:
            raw_page = mapping["page"]
            if raw_page is None:
                raise ContractDecodeError(
                    f"{path}.page: null is not a valid value"
                )
            field_page = PageMetadata.from_wire(raw_page, f"{path}.page")
        return cls(
            job_id=field_job_id,
            limit=field_limit,
            page=field_page,
        )


@dataclass(frozen=True, slots=True)
class JobEventsResult:
    """Result of `job.events`: one page of a snapshot-stable event read. `snapshot_event_count`
    is the event count captured when this pagination session began, so the session's
    sequences are exactly `0 .. snapshot_event_count - 1` and events recorded after the
    snapshot never appear in it; the same count is repeated on every page of the session and
    never changes within one. A fresh tokenless request captures a new snapshot and may see
    more. Page events are strictly increasing, duplicate-free, and contiguous from the
    position the request continued from. `page` is always present: a continuation token means
    more of the snapshot remains, and no token means the snapshot is exhausted.
    """

    job_id: OpaqueToken
    events: tuple[JobEvent, ...]
    snapshot_event_count: int
    page: PageMetadata

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["job_id"] = self.job_id
        wire["events"] = [item.to_wire() for item in self.events]
        wire["snapshot_event_count"] = self.snapshot_event_count
        wire["page"] = self.page.to_wire()
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "JobEventsResult") -> JobEventsResult:
        """Decode a wire payload into a JobEventsResult.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_job_id = _decode_str(_require_field(mapping, "job_id", path), f"{path}.job_id")
        field_events_items = _decode_sequence(
            _require_field(mapping, "events", path),
            f"{path}.events",
        )
        field_events = tuple(
            JobEvent.from_wire(item, f"{path}.events[{index}]")
            for index, item in enumerate(field_events_items)
        )
        field_snapshot_event_count = _decode_int(
            _require_field(mapping, "snapshot_event_count", path),
            f"{path}.snapshot_event_count",
        )
        field_page = PageMetadata.from_wire(_require_field(mapping, "page", path), f"{path}.page")
        return cls(
            job_id=field_job_id,
            events=field_events,
            snapshot_event_count=field_snapshot_event_count,
            page=field_page,
        )


@dataclass(frozen=True, slots=True)
class KnowledgeSearchInput:
    """Input for `knowledge.search`. Workspace-scoped: the workspace is the request envelope's
    selected workspace; this payload never carries a second, independent workspace
    identifier. Absent `view` defaults to `current_canonical`; only an explicit `view`
    selector may request `candidates` or `history`, so a caller can never receive candidate,
    rejected, superseded, or otherwise non-canonical governed knowledge by omission.
    """

    query: MemoryQuery
    order: MemorySearchOrder | None = None
    view: GovernedRecordView | None = None
    record_type: GovernedRecordType | None = None
    domain_scope: RecordDomainScope | None = None
    limit: PageLimit | None = None
    page: PageMetadata | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["query"] = self.query
        if self.order is not None:
            wire["order"] = self.order
        if self.view is not None:
            wire["view"] = self.view
        if self.record_type is not None:
            wire["record_type"] = self.record_type
        if self.domain_scope is not None:
            wire["domain_scope"] = self.domain_scope
        if self.limit is not None:
            wire["limit"] = self.limit
        if self.page is not None:
            wire["page"] = self.page.to_wire()
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "KnowledgeSearchInput") -> KnowledgeSearchInput:
        """Decode a wire payload into a KnowledgeSearchInput.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_query = _decode_str(_require_field(mapping, "query", path), f"{path}.query")
        field_order: MemorySearchOrder | None = None
        if "order" in mapping:
            raw_order = mapping["order"]
            if raw_order is None:
                raise ContractDecodeError(
                    f"{path}.order: null is not a valid value"
                )
            field_order = _decode_str(raw_order, f"{path}.order")
        field_view: GovernedRecordView | None = None
        if "view" in mapping:
            raw_view = mapping["view"]
            if raw_view is None:
                raise ContractDecodeError(
                    f"{path}.view: null is not a valid value"
                )
            field_view = _decode_str(raw_view, f"{path}.view")
        field_record_type: GovernedRecordType | None = None
        if "record_type" in mapping:
            raw_record_type = mapping["record_type"]
            if raw_record_type is None:
                raise ContractDecodeError(
                    f"{path}.record_type: null is not a valid value"
                )
            field_record_type = _decode_str(raw_record_type, f"{path}.record_type")
        field_domain_scope: RecordDomainScope | None = None
        if "domain_scope" in mapping:
            raw_domain_scope = mapping["domain_scope"]
            if raw_domain_scope is None:
                raise ContractDecodeError(
                    f"{path}.domain_scope: null is not a valid value"
                )
            field_domain_scope = _decode_str(raw_domain_scope, f"{path}.domain_scope")
        field_limit: PageLimit | None = None
        if "limit" in mapping:
            raw_limit = mapping["limit"]
            if raw_limit is None:
                raise ContractDecodeError(
                    f"{path}.limit: null is not a valid value"
                )
            field_limit = _decode_int(raw_limit, f"{path}.limit")
        field_page: PageMetadata | None = None
        if "page" in mapping:
            raw_page = mapping["page"]
            if raw_page is None:
                raise ContractDecodeError(
                    f"{path}.page: null is not a valid value"
                )
            field_page = PageMetadata.from_wire(raw_page, f"{path}.page")
        return cls(
            query=field_query,
            order=field_order,
            view=field_view,
            record_type=field_record_type,
            domain_scope=field_domain_scope,
            limit=field_limit,
            page=field_page,
        )


@dataclass(frozen=True, slots=True)
class KnowledgeProposeInput:
    """Input for `knowledge.propose`: transitions an existing proposed (`l1`/`proposed`) record
    into a candidate (`l1`/`candidate`) awaiting a governance decision. Never duplicates
    `memory.create`: it identifies an already-existing record by `record_id` rather than
    proposing new content, and never carries content, evidence, or assertion fields. The
    target version is the envelope's `MutationPrecondition.record_version`, not duplicated
    here. Like every other governance transition, the rationale is required: no governance
    decision on this contract is ever recorded without an explicit, auditable reason.
    """

    record_id: RecordId
    rationale: GovernanceRationale

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["record_id"] = self.record_id
        wire["rationale"] = self.rationale.to_wire()
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "KnowledgeProposeInput"
    ) -> KnowledgeProposeInput:
        """Decode a wire payload into a KnowledgeProposeInput.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_record_id = _decode_str(
            _require_field(mapping, "record_id", path),
            f"{path}.record_id",
        )
        field_rationale = GovernanceRationale.from_wire(
            _require_field(mapping, "rationale", path),
            f"{path}.rationale",
        )
        return cls(
            record_id=field_record_id,
            rationale=field_rationale,
        )


@dataclass(frozen=True, slots=True)
class CandidateApproveInput:
    """Input for `candidate.approve`: creates a new accepted (`l2`/`accepted`) governed version
    of an existing candidate record, with reviewer authority attributed server-side. The
    target version is the envelope's `MutationPrecondition.record_version`, not duplicated
    here. Carries no authority-level, reviewer identity, or governance-state field of its own
    -- only the explicit, auditable rationale for the decision.
    """

    record_id: RecordId
    rationale: GovernanceRationale

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["record_id"] = self.record_id
        wire["rationale"] = self.rationale.to_wire()
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "CandidateApproveInput"
    ) -> CandidateApproveInput:
        """Decode a wire payload into a CandidateApproveInput.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_record_id = _decode_str(
            _require_field(mapping, "record_id", path),
            f"{path}.record_id",
        )
        field_rationale = GovernanceRationale.from_wire(
            _require_field(mapping, "rationale", path),
            f"{path}.rationale",
        )
        return cls(
            record_id=field_record_id,
            rationale=field_rationale,
        )


@dataclass(frozen=True, slots=True)
class CandidateRejectInput:
    """Input for `candidate.reject`: creates a new rejected (`l1`/`rejected`) governed version
    of an existing candidate record, with reviewer authority attributed server-side. The
    target version is the envelope's `MutationPrecondition.record_version`, not duplicated
    here. `rejected` is never treated as a favourable or accepted authority decision.
    """

    record_id: RecordId
    rationale: GovernanceRationale

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["record_id"] = self.record_id
        wire["rationale"] = self.rationale.to_wire()
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "CandidateRejectInput") -> CandidateRejectInput:
        """Decode a wire payload into a CandidateRejectInput.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_record_id = _decode_str(
            _require_field(mapping, "record_id", path),
            f"{path}.record_id",
        )
        field_rationale = GovernanceRationale.from_wire(
            _require_field(mapping, "rationale", path),
            f"{path}.rationale",
        )
        return cls(
            record_id=field_record_id,
            rationale=field_rationale,
        )


@dataclass(frozen=True, slots=True)
class MemoryListInput:
    """Input for `memory.list`. Workspace-scoped: the workspace is the request envelope's
    selected workspace; this payload never carries a second, independent workspace
    identifier.
    """

    view: GovernedRecordView | None = None
    record_type: GovernedRecordType | None = None
    limit: PageLimit | None = None
    page: PageMetadata | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        if self.view is not None:
            wire["view"] = self.view
        if self.record_type is not None:
            wire["record_type"] = self.record_type
        if self.limit is not None:
            wire["limit"] = self.limit
        if self.page is not None:
            wire["page"] = self.page.to_wire()
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "MemoryListInput") -> MemoryListInput:
        """Decode a wire payload into a MemoryListInput.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_view: GovernedRecordView | None = None
        if "view" in mapping:
            raw_view = mapping["view"]
            if raw_view is None:
                raise ContractDecodeError(
                    f"{path}.view: null is not a valid value"
                )
            field_view = _decode_str(raw_view, f"{path}.view")
        field_record_type: GovernedRecordType | None = None
        if "record_type" in mapping:
            raw_record_type = mapping["record_type"]
            if raw_record_type is None:
                raise ContractDecodeError(
                    f"{path}.record_type: null is not a valid value"
                )
            field_record_type = _decode_str(raw_record_type, f"{path}.record_type")
        field_limit: PageLimit | None = None
        if "limit" in mapping:
            raw_limit = mapping["limit"]
            if raw_limit is None:
                raise ContractDecodeError(
                    f"{path}.limit: null is not a valid value"
                )
            field_limit = _decode_int(raw_limit, f"{path}.limit")
        field_page: PageMetadata | None = None
        if "page" in mapping:
            raw_page = mapping["page"]
            if raw_page is None:
                raise ContractDecodeError(
                    f"{path}.page: null is not a valid value"
                )
            field_page = PageMetadata.from_wire(raw_page, f"{path}.page")
        return cls(
            view=field_view,
            record_type=field_record_type,
            limit=field_limit,
            page=field_page,
        )


@dataclass(frozen=True, slots=True)
class MemorySearchInput:
    """Input for `memory.search`. Workspace-scoped: the workspace is the request envelope's
    selected workspace; this payload never carries a second, independent workspace
    identifier. Carries the normalized query and requested order a later stateful conformance
    slice will bind an issued continuation token to, alongside principal/workspace/operation
    binding.
    """

    query: MemoryQuery
    order: MemorySearchOrder | None = None
    view: GovernedRecordView | None = None
    record_type: GovernedRecordType | None = None
    limit: PageLimit | None = None
    page: PageMetadata | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["query"] = self.query
        if self.order is not None:
            wire["order"] = self.order
        if self.view is not None:
            wire["view"] = self.view
        if self.record_type is not None:
            wire["record_type"] = self.record_type
        if self.limit is not None:
            wire["limit"] = self.limit
        if self.page is not None:
            wire["page"] = self.page.to_wire()
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "MemorySearchInput") -> MemorySearchInput:
        """Decode a wire payload into a MemorySearchInput.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_query = _decode_str(_require_field(mapping, "query", path), f"{path}.query")
        field_order: MemorySearchOrder | None = None
        if "order" in mapping:
            raw_order = mapping["order"]
            if raw_order is None:
                raise ContractDecodeError(
                    f"{path}.order: null is not a valid value"
                )
            field_order = _decode_str(raw_order, f"{path}.order")
        field_view: GovernedRecordView | None = None
        if "view" in mapping:
            raw_view = mapping["view"]
            if raw_view is None:
                raise ContractDecodeError(
                    f"{path}.view: null is not a valid value"
                )
            field_view = _decode_str(raw_view, f"{path}.view")
        field_record_type: GovernedRecordType | None = None
        if "record_type" in mapping:
            raw_record_type = mapping["record_type"]
            if raw_record_type is None:
                raise ContractDecodeError(
                    f"{path}.record_type: null is not a valid value"
                )
            field_record_type = _decode_str(raw_record_type, f"{path}.record_type")
        field_limit: PageLimit | None = None
        if "limit" in mapping:
            raw_limit = mapping["limit"]
            if raw_limit is None:
                raise ContractDecodeError(
                    f"{path}.limit: null is not a valid value"
                )
            field_limit = _decode_int(raw_limit, f"{path}.limit")
        field_page: PageMetadata | None = None
        if "page" in mapping:
            raw_page = mapping["page"]
            if raw_page is None:
                raise ContractDecodeError(
                    f"{path}.page: null is not a valid value"
                )
            field_page = PageMetadata.from_wire(raw_page, f"{path}.page")
        return cls(
            query=field_query,
            order=field_order,
            view=field_view,
            record_type=field_record_type,
            limit=field_limit,
            page=field_page,
        )


@dataclass(frozen=True, slots=True)
class OperationMetadata:
    """The full declared contract characteristics of one operation: its scope, payload schemas,
    required capability, side effects, and
    job/pagination/idempotency/precondition/audit/error posture. Every entry of this
    document's `x-omnivia-operation-catalogue` is exactly one of these, so the catalogue is
    metadata a caller can read rather than behaviour it has to discover. `allowed_errors` is
    materialized per operation: reusable error postures exist in the specification that froze
    this catalogue, but never on the wire, so no decoder has to resolve a profile name to
    know what an operation may fail with.
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
class WorkspaceCompatibility:
    """The concrete workspace-format version a workspace is stored at, and the inclusive version
    window this server build can read and write. Reuses the same `VersionWindow` and
    `OpenCode` primitives `VersionCapabilityEnvelope` negotiates with, rather than inventing
    a second version model.
    """

    workspace_format_version: ContractVersion
    supported_workspace_versions: VersionWindow
    status: OpenCode

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["workspace_format_version"] = self.workspace_format_version
        wire["supported_workspace_versions"] = self.supported_workspace_versions.to_wire()
        wire["status"] = self.status
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "WorkspaceCompatibility"
    ) -> WorkspaceCompatibility:
        """Decode a wire payload into a WorkspaceCompatibility.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_workspace_format_version = _decode_str(
            _require_field(mapping, "workspace_format_version", path),
            f"{path}.workspace_format_version",
        )
        field_supported_workspace_versions = VersionWindow.from_wire(
            _require_field(mapping, "supported_workspace_versions", path),
            f"{path}.supported_workspace_versions",
        )
        field_status = _decode_str(_require_field(mapping, "status", path), f"{path}.status")
        return cls(
            workspace_format_version=field_workspace_format_version,
            supported_workspace_versions=field_supported_workspace_versions,
            status=field_status,
        )


@dataclass(frozen=True, slots=True)
class WorkspaceListInput:
    """Input for `workspace.list`. Installation-scoped: carries no workspace identifier, since
    it lists every workspace the caller's installation-level authority can see.
    """

    limit: PageLimit | None = None
    page: PageMetadata | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        if self.limit is not None:
            wire["limit"] = self.limit
        if self.page is not None:
            wire["page"] = self.page.to_wire()
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "WorkspaceListInput") -> WorkspaceListInput:
        """Decode a wire payload into a WorkspaceListInput.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_limit: PageLimit | None = None
        if "limit" in mapping:
            raw_limit = mapping["limit"]
            if raw_limit is None:
                raise ContractDecodeError(
                    f"{path}.limit: null is not a valid value"
                )
            field_limit = _decode_int(raw_limit, f"{path}.limit")
        field_page: PageMetadata | None = None
        if "page" in mapping:
            raw_page = mapping["page"]
            if raw_page is None:
                raise ContractDecodeError(
                    f"{path}.page: null is not a valid value"
                )
            field_page = PageMetadata.from_wire(raw_page, f"{path}.page")
        return cls(
            limit=field_limit,
            page=field_page,
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


ContextPackCitation: TypeAlias = ContextPackEvidenceCitation | ContextPackRecordCitation
"""One exact citation in a pack: either an evidence citation or a governed-record citation, never
both and never neither. The two branches are distinct object shapes rather than one shape with
two optional pointers, so what a citation points at is settled structurally by the wire document
instead of being left to a semantic agreement check.
"""


def context_pack_citation_from_wire(
    payload: object, path: str = "ContextPackCitation"
) -> ContextPackCitation:
    """Decode a wire payload into exactly one ContextPackCitation branch.

    The branches are mutually exclusive by construction: a payload carrying more than one
    discriminator, or none at all, is rejected rather than guessed at.
    """
    mapping = _require_mapping(payload, path)
    discriminators = ("evidence_reference", "record_reference")
    matched = tuple(key for key in discriminators if key in mapping)
    if len(matched) != 1:
        raise ContractDecodeError(
            f"{path}: expected exactly one of {discriminators}, found {matched}"
        )
    if matched[0] == "evidence_reference":
        return ContextPackEvidenceCitation.from_wire(mapping, path)
    if matched[0] == "record_reference":
        return ContextPackRecordCitation.from_wire(mapping, path)
    raise ContractDecodeError(f"{path}: unreachable discriminator state")


def context_pack_citation_to_wire(value: ContextPackCitation) -> dict[str, Any]:
    """Render one ContextPackCitation branch as a JSON-compatible mapping."""
    return value.to_wire()


@dataclass(frozen=True, slots=True)
class ContextPackAuthorizationContext:
    """The complete authority context one Context Pack was produced under, recorded so the build
    can be reproduced and audited. Historical reproducibility context only, and never a live
    grant: possessing it authorizes nothing, and following any citation still requires fresh
    authorization against the cited evidence or record. Recorded structurally rather than as
    an opaque fingerprint so a reviewer can actually check which principal, roles,
    capabilities, scopes, purpose, and policy versions were in force, instead of comparing
    two hashes and learning only that they differ.
    """

    workspace_id: WorkspaceId
    authority: GrantedAuthority
    scopes: tuple[Scope, ...]
    purpose: Purpose
    policy_versions: Mapping[str, OpaqueToken]
    pre_ranking_authorization_enforced: bool
    authorized_candidate_set_checksum: ContextPackDigest

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["workspace_id"] = self.workspace_id
        wire["authority"] = self.authority.to_wire()
        wire["scopes"] = list(self.scopes)
        wire["purpose"] = self.purpose
        wire["policy_versions"] = dict(self.policy_versions)
        wire["pre_ranking_authorization_enforced"] = self.pre_ranking_authorization_enforced
        wire["authorized_candidate_set_checksum"] = self.authorized_candidate_set_checksum
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "ContextPackAuthorizationContext"
    ) -> ContextPackAuthorizationContext:
        """Decode a wire payload into a ContextPackAuthorizationContext.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_workspace_id = _decode_str(
            _require_field(mapping, "workspace_id", path),
            f"{path}.workspace_id",
        )
        field_authority = GrantedAuthority.from_wire(
            _require_field(mapping, "authority", path),
            f"{path}.authority",
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
        field_policy_versions_entries = _require_mapping(
            _require_field(mapping, "policy_versions", path),
            f"{path}.policy_versions",
        )
        field_policy_versions = MappingProxyType(
            {
                key: _decode_str(value, f"{path}.policy_versions.{key}")
                for key, value in field_policy_versions_entries.items()
            }
        )
        field_pre_ranking_authorization_enforced = _decode_bool(
            _require_field(mapping, "pre_ranking_authorization_enforced", path),
            f"{path}.pre_ranking_authorization_enforced",
        )
        field_authorized_candidate_set_checksum = _decode_str(
            _require_field(mapping, "authorized_candidate_set_checksum", path),
            f"{path}.authorized_candidate_set_checksum",
        )
        return cls(
            workspace_id=field_workspace_id,
            authority=field_authority,
            scopes=field_scopes,
            purpose=field_purpose,
            policy_versions=field_policy_versions,
            pre_ranking_authorization_enforced=field_pre_ranking_authorization_enforced,
            authorized_candidate_set_checksum=field_authorized_candidate_set_checksum,
        )


@dataclass(frozen=True, slots=True)
class ContextPackAuthorizedCandidateSetManifest:
    """The exact preimage `ContextPackAuthorizationContext.authorized_candidate_set_checksum` is
    a digest of. It names the complete post-retrieval, post-request-scope, post-
    workspace/scope/purpose/capability/policy/ACL/sensitivity-authorization candidate
    frontier, frozen before the first ranking, reranking, selection, or budget decision: not
    the whole workspace, and not merely the items a pack ended up selecting. Unauthorized,
    request-filtered, tombstoned, and invalid-at-resolution material is absent; nothing may
    be introduced into a pack after this frontier is frozen, so every selected item is a
    member of it under its own exact partition. This is trusted in-process input to a
    verifier, never a response field and never logged: it is the independent statement a
    checksum copied out of the pack itself could never be. The digest is `sha256:` followed
    by the lowercase hex SHA-256 of the RFC 8785 canonical UTF-8 bytes of this document, with
    `candidates` sorted by partition and then by the remaining identity members in the order
    they are declared, comparing each component by unsigned UTF-16 code unit with no Unicode
    normalization. That sort makes the digest order-insensitive; a duplicate is refused
    outright rather than collapsed, so sorting never has to decide what a repeated identity
    meant. RFC 8785 orders object member names but never array elements, so that element sort
    is part of this definition rather than of the canonicalization. UTF-16 code-unit
    comparison is normative because it is the ordering RFC 8785 already imposes on member
    names, and a preimage ordered by one rule and canonicalized under another would be two
    rules pretending to be one; note that every v1 identity alphabet here is ASCII
    (`EvidenceId`, `RecordId`), printable ASCII (`RecordVersion`), or an ASCII-restricted
    checksum (`EvidenceChecksum`), so no valid v1 candidate can distinguish UTF-16 order from
    code-point order -- the rule is stated for the canonicalizer it must agree with, not
    because a v1 identity could exercise the difference. The empty candidate set is valid and
    has a well-defined digest.
    """

    format: str
    workspace_id: WorkspaceId
    candidates: tuple[ContextPackAuthorizedCandidate, ...]

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["format"] = self.format
        wire["workspace_id"] = self.workspace_id
        wire["candidates"] = [context_pack_authorized_candidate_to_wire(item) for item in self.candidates]
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "ContextPackAuthorizedCandidateSetManifest"
    ) -> ContextPackAuthorizedCandidateSetManifest:
        """Decode a wire payload into a ContextPackAuthorizedCandidateSetManifest.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_format = _decode_str(_require_field(mapping, "format", path), f"{path}.format")
        field_workspace_id = _decode_str(
            _require_field(mapping, "workspace_id", path),
            f"{path}.workspace_id",
        )
        field_candidates_items = _decode_sequence(
            _require_field(mapping, "candidates", path),
            f"{path}.candidates",
        )
        field_candidates = tuple(
            context_pack_authorized_candidate_from_wire(item, f"{path}.candidates[{index}]")
            for index, item in enumerate(field_candidates_items)
        )
        return cls(
            format=field_format,
            workspace_id=field_workspace_id,
            candidates=field_candidates,
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
    progress and attempt, and which control actions are available. `latest_attempt` is the
    job's attempt *N*, not a history: a `running` job reports the running attempt it executes
    under, a `succeeded` or `failed` job reports the finished attempt that produced that
    outcome, and a `queued` job either has never executed (no attempt at all) or reports the
    finished `failed`/`cancelled` attempt retained after an accepted `job.retry` scheduled
    recovery -- never a succeeded, running, queued, or unfinished one, since none of those
    describes a job waiting to start.
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
    `cancellation`. Terminal success is typed rather than opaque: `result_kind` names which
    frozen result shape `result` carries, so a caller reads a success payload by matching a
    declared kind instead of guessing from the job kind. `result` stays an opaque JSON object
    in this document because JSON Schema cannot bind it to a per-kind shape within the subset
    the generator supports; `result_kind` and `semantics_jobs`
    (`omnivia_core.contracts.v1.semantics_jobs`) still validate the frozen result-kind
    mapping. The operation catalogue additionally binds a job-starting operation to its
    terminal result schema through `OperationJobMetadata.terminal_result_schema_ref`. The one
    kind frozen in v1 is `import_completion`: `import.start` binds it to
    `ImportCompletionResult`.
    """

    identity: JobIdentity
    state: JobState
    finished_at: Timestamp
    attempts: tuple[JobAttempt, ...]
    result_kind: OpenCode
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
        wire["result_kind"] = self.result_kind
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
        field_result_kind = _decode_str(
            _require_field(mapping, "result_kind", path),
            f"{path}.result_kind",
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
            result_kind=field_result_kind,
            result=field_result,
        )


@dataclass(frozen=True, slots=True)
class JobTerminalFailure:
    """The final outcome of a job that failed. Carries `error` and never `result` or
    `cancellation`. `attempts` is non-empty -- a job cannot fail without having executed --
    and `error` is exactly the final attempt's own `error`: the failure that ended the last
    attempt is the failure that ended the job, and two spellings of it that could disagree
    would leave a caller unable to say which one is the outcome.
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
    or `error`. `attempts` may be empty, and only here: a job cancelled while still queued
    never executed, so it has no attempt to report. When it does carry attempts, the final
    one is the `cancelled` attempt that ended it and finished when the job did.
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
class CandidateAssertion:
    """Who is asserting a governed record's claim, when, and on what evidence, plus the validity
    window they propose for it. This is caller-supplied provenance for the claim -- carried
    into `memory.create` and `record.supersede` inputs, and preserved on the resulting
    record's `RecordProvenance` -- not the server-owned governance decision: it never carries
    authority level, reviewer/policy identity, or any other field a least-authority-
    escalating mutation input is forbidden from carrying. Defined here rather than in
    `memory.schema.json` so `RecordProvenance` can preserve it without `records.schema.json`
    depending on a document that already depends on it.
    """

    actor_id: Identifier
    actor_kind: OpenCode
    actor_role: OpenCode
    asserted_at: Timestamp
    evidence: tuple[EvidenceReference, ...]
    proposed_valid_from: Timestamp | None = None
    proposed_valid_until: Timestamp | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["actor_id"] = self.actor_id
        wire["actor_kind"] = self.actor_kind
        wire["actor_role"] = self.actor_role
        wire["asserted_at"] = self.asserted_at
        if self.proposed_valid_from is not None:
            wire["proposed_valid_from"] = self.proposed_valid_from
        if self.proposed_valid_until is not None:
            wire["proposed_valid_until"] = self.proposed_valid_until
        wire["evidence"] = [item.to_wire() for item in self.evidence]
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "CandidateAssertion") -> CandidateAssertion:
        """Decode a wire payload into a CandidateAssertion.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_actor_id = _decode_str(_require_field(mapping, "actor_id", path), f"{path}.actor_id")
        field_actor_kind = _decode_str(
            _require_field(mapping, "actor_kind", path),
            f"{path}.actor_kind",
        )
        field_actor_role = _decode_str(
            _require_field(mapping, "actor_role", path),
            f"{path}.actor_role",
        )
        field_asserted_at = _decode_str(
            _require_field(mapping, "asserted_at", path),
            f"{path}.asserted_at",
        )
        field_proposed_valid_from: Timestamp | None = None
        if "proposed_valid_from" in mapping:
            raw_proposed_valid_from = mapping["proposed_valid_from"]
            if raw_proposed_valid_from is None:
                raise ContractDecodeError(
                    f"{path}.proposed_valid_from: null is not a valid value"
                )
            field_proposed_valid_from = _decode_str(
                raw_proposed_valid_from,
                f"{path}.proposed_valid_from",
            )
        field_proposed_valid_until: Timestamp | None = None
        if "proposed_valid_until" in mapping:
            raw_proposed_valid_until = mapping["proposed_valid_until"]
            if raw_proposed_valid_until is None:
                raise ContractDecodeError(
                    f"{path}.proposed_valid_until: null is not a valid value"
                )
            field_proposed_valid_until = _decode_str(
                raw_proposed_valid_until,
                f"{path}.proposed_valid_until",
            )
        field_evidence_items = _decode_sequence(
            _require_field(mapping, "evidence", path),
            f"{path}.evidence",
        )
        field_evidence = tuple(
            EvidenceReference.from_wire(item, f"{path}.evidence[{index}]")
            for index, item in enumerate(field_evidence_items)
        )
        return cls(
            actor_id=field_actor_id,
            actor_kind=field_actor_kind,
            actor_role=field_actor_role,
            asserted_at=field_asserted_at,
            proposed_valid_from=field_proposed_valid_from,
            proposed_valid_until=field_proposed_valid_until,
            evidence=field_evidence,
        )


@dataclass(frozen=True, slots=True)
class ProvenanceEntry:
    """One step in a record's history: who or what did what, when, and -- for a governance
    transition -- the explicit rationale it was taken under.
    """

    actor_id: Identifier
    actor_kind: OpenCode
    action: OpenCode
    occurred_at: Timestamp
    reason_code: OpenCode | None = None
    reason_comment: str | None = None
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
        if self.reason_code is not None:
            wire["reason_code"] = self.reason_code
        if self.reason_comment is not None:
            wire["reason_comment"] = self.reason_comment
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
        field_reason_code: OpenCode | None = None
        if "reason_code" in mapping:
            raw_reason_code = mapping["reason_code"]
            if raw_reason_code is None:
                raise ContractDecodeError(
                    f"{path}.reason_code: null is not a valid value"
                )
            field_reason_code = _decode_str(raw_reason_code, f"{path}.reason_code")
        field_reason_comment: str | None = None
        if "reason_comment" in mapping:
            raw_reason_comment = mapping["reason_comment"]
            if raw_reason_comment is None:
                raise ContractDecodeError(
                    f"{path}.reason_comment: null is not a valid value"
                )
            field_reason_comment = _decode_str(raw_reason_comment, f"{path}.reason_comment")
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
            reason_code=field_reason_code,
            reason_comment=field_reason_comment,
            evidence=field_evidence,
        )


@dataclass(frozen=True, slots=True)
class WorkspaceDescriptor:
    """A workspace's identity, display name, lifecycle status, format compatibility, and
    lifecycle timestamps.
    """

    workspace_id: WorkspaceId
    display_name: str
    status: WorkspaceStatus
    compatibility: WorkspaceCompatibility
    created_at: Timestamp
    updated_at: Timestamp | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["workspace_id"] = self.workspace_id
        wire["display_name"] = self.display_name
        wire["status"] = self.status
        wire["compatibility"] = self.compatibility.to_wire()
        wire["created_at"] = self.created_at
        if self.updated_at is not None:
            wire["updated_at"] = self.updated_at
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "WorkspaceDescriptor") -> WorkspaceDescriptor:
        """Decode a wire payload into a WorkspaceDescriptor.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_workspace_id = _decode_str(
            _require_field(mapping, "workspace_id", path),
            f"{path}.workspace_id",
        )
        field_display_name = _decode_str(
            _require_field(mapping, "display_name", path),
            f"{path}.display_name",
        )
        field_status = _decode_str(_require_field(mapping, "status", path), f"{path}.status")
        field_compatibility = WorkspaceCompatibility.from_wire(
            _require_field(mapping, "compatibility", path),
            f"{path}.compatibility",
        )
        field_created_at = _decode_str(
            _require_field(mapping, "created_at", path),
            f"{path}.created_at",
        )
        field_updated_at: Timestamp | None = None
        if "updated_at" in mapping:
            raw_updated_at = mapping["updated_at"]
            if raw_updated_at is None:
                raise ContractDecodeError(
                    f"{path}.updated_at: null is not a valid value"
                )
            field_updated_at = _decode_str(raw_updated_at, f"{path}.updated_at")
        return cls(
            workspace_id=field_workspace_id,
            display_name=field_display_name,
            status=field_status,
            compatibility=field_compatibility,
            created_at=field_created_at,
            updated_at=field_updated_at,
        )


@dataclass(frozen=True, slots=True)
class ContextPackReproducibility:
    """Everything a second build needs to reproduce one Context Pack byte for byte: the pack
    format version, the builder, the normalized request, the authority context the build ran
    under, the exact evidence and record versions selected, the projection the read was
    served from, the retrieval/ranking/reranking/selection/tokenizer/summarizer/model
    versions applied, the instant the canonical knowledge was resolved at, and the
    canonicalization and checksum that make the result content-addressed. With every one of
    these unchanged, rebuilding must reproduce the identical pack. Carries no audit
    reference: the response envelope owns audit linkage, and folding a per-request audit
    identifier into a content-addressed artifact would make two identical builds hash
    differently.
    """

    pack_format_version: ContractVersion
    builder_version: Identifier
    normalized_request: ContextPackNormalizedRequest
    authorization_context: ContextPackAuthorizationContext
    evidence_versions: tuple[ContextPackEvidenceReference, ...]
    record_versions: tuple[RecordVersionReference, ...]
    freshness: ProjectionFreshness
    retrieval_version: Identifier
    ranking_version: Identifier
    reranking_version: Identifier
    selection_version: Identifier
    tokenizer_id: Identifier
    tokenizer_version: Identifier
    summarizer_version: Identifier
    model_versions: Mapping[str, Identifier]
    canonical_resolution_time: Timestamp
    generated_at: Timestamp
    artifact_canonicalization: OpenCode
    artifact_checksum: ContextPackDigest

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["pack_format_version"] = self.pack_format_version
        wire["builder_version"] = self.builder_version
        wire["normalized_request"] = self.normalized_request.to_wire()
        wire["authorization_context"] = self.authorization_context.to_wire()
        wire["evidence_versions"] = [item.to_wire() for item in self.evidence_versions]
        wire["record_versions"] = [item.to_wire() for item in self.record_versions]
        wire["freshness"] = self.freshness.to_wire()
        wire["retrieval_version"] = self.retrieval_version
        wire["ranking_version"] = self.ranking_version
        wire["reranking_version"] = self.reranking_version
        wire["selection_version"] = self.selection_version
        wire["tokenizer_id"] = self.tokenizer_id
        wire["tokenizer_version"] = self.tokenizer_version
        wire["summarizer_version"] = self.summarizer_version
        wire["model_versions"] = dict(self.model_versions)
        wire["canonical_resolution_time"] = self.canonical_resolution_time
        wire["generated_at"] = self.generated_at
        wire["artifact_canonicalization"] = self.artifact_canonicalization
        wire["artifact_checksum"] = self.artifact_checksum
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "ContextPackReproducibility"
    ) -> ContextPackReproducibility:
        """Decode a wire payload into a ContextPackReproducibility.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_pack_format_version = _decode_str(
            _require_field(mapping, "pack_format_version", path),
            f"{path}.pack_format_version",
        )
        field_builder_version = _decode_str(
            _require_field(mapping, "builder_version", path),
            f"{path}.builder_version",
        )
        field_normalized_request = ContextPackNormalizedRequest.from_wire(
            _require_field(mapping, "normalized_request", path),
            f"{path}.normalized_request",
        )
        field_authorization_context = ContextPackAuthorizationContext.from_wire(
            _require_field(mapping, "authorization_context", path),
            f"{path}.authorization_context",
        )
        field_evidence_versions_items = _decode_sequence(
            _require_field(mapping, "evidence_versions", path),
            f"{path}.evidence_versions",
        )
        field_evidence_versions = tuple(
            ContextPackEvidenceReference.from_wire(item, f"{path}.evidence_versions[{index}]")
            for index, item in enumerate(field_evidence_versions_items)
        )
        field_record_versions_items = _decode_sequence(
            _require_field(mapping, "record_versions", path),
            f"{path}.record_versions",
        )
        field_record_versions = tuple(
            RecordVersionReference.from_wire(item, f"{path}.record_versions[{index}]")
            for index, item in enumerate(field_record_versions_items)
        )
        field_freshness = ProjectionFreshness.from_wire(
            _require_field(mapping, "freshness", path),
            f"{path}.freshness",
        )
        field_retrieval_version = _decode_str(
            _require_field(mapping, "retrieval_version", path),
            f"{path}.retrieval_version",
        )
        field_ranking_version = _decode_str(
            _require_field(mapping, "ranking_version", path),
            f"{path}.ranking_version",
        )
        field_reranking_version = _decode_str(
            _require_field(mapping, "reranking_version", path),
            f"{path}.reranking_version",
        )
        field_selection_version = _decode_str(
            _require_field(mapping, "selection_version", path),
            f"{path}.selection_version",
        )
        field_tokenizer_id = _decode_str(
            _require_field(mapping, "tokenizer_id", path),
            f"{path}.tokenizer_id",
        )
        field_tokenizer_version = _decode_str(
            _require_field(mapping, "tokenizer_version", path),
            f"{path}.tokenizer_version",
        )
        field_summarizer_version = _decode_str(
            _require_field(mapping, "summarizer_version", path),
            f"{path}.summarizer_version",
        )
        field_model_versions_entries = _require_mapping(
            _require_field(mapping, "model_versions", path),
            f"{path}.model_versions",
        )
        field_model_versions = MappingProxyType(
            {
                key: _decode_str(value, f"{path}.model_versions.{key}")
                for key, value in field_model_versions_entries.items()
            }
        )
        field_canonical_resolution_time = _decode_str(
            _require_field(mapping, "canonical_resolution_time", path),
            f"{path}.canonical_resolution_time",
        )
        field_generated_at = _decode_str(
            _require_field(mapping, "generated_at", path),
            f"{path}.generated_at",
        )
        field_artifact_canonicalization = _decode_str(
            _require_field(mapping, "artifact_canonicalization", path),
            f"{path}.artifact_canonicalization",
        )
        field_artifact_checksum = _decode_str(
            _require_field(mapping, "artifact_checksum", path),
            f"{path}.artifact_checksum",
        )
        return cls(
            pack_format_version=field_pack_format_version,
            builder_version=field_builder_version,
            normalized_request=field_normalized_request,
            authorization_context=field_authorization_context,
            evidence_versions=field_evidence_versions,
            record_versions=field_record_versions,
            freshness=field_freshness,
            retrieval_version=field_retrieval_version,
            ranking_version=field_ranking_version,
            reranking_version=field_reranking_version,
            selection_version=field_selection_version,
            tokenizer_id=field_tokenizer_id,
            tokenizer_version=field_tokenizer_version,
            summarizer_version=field_summarizer_version,
            model_versions=field_model_versions,
            canonical_resolution_time=field_canonical_resolution_time,
            generated_at=field_generated_at,
            artifact_canonicalization=field_artifact_canonicalization,
            artifact_checksum=field_artifact_checksum,
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


@dataclass(frozen=True, slots=True)
class EvidenceArtifact:
    """One complete, append-preserving L0 evidence artifact: stable identity, workspace, exact
    source/native locator, applicable temporal instants, content checksum and media type,
    opaque metadata, permission/sensitivity labels, tombstone status, parser/ingestion
    status, and append-only provenance history. Carries no `GovernanceLayer`,
    `GovernanceState`, `RecordCurrentness`, or `authority_level` field: an evidence artifact
    is raw L0 material, never governed knowledge, and this shape must never be mistaken for a
    `GovernedRecord`.
    """

    evidence_id: EvidenceId
    workspace_id: WorkspaceId
    source: SourceReference
    temporal: RecordTemporalMetadata
    content_checksum: EvidenceChecksum
    media_type: MediaType
    metadata: JsonObject
    permission_labels: tuple[OpenCode, ...]
    sensitivity: OpenCode
    tombstoned: bool
    parser_status: OpenCode
    ingestion_status: OpenCode
    provenance_history: tuple[ProvenanceEntry, ...]
    import_run_id: OpaqueToken | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["evidence_id"] = self.evidence_id
        wire["workspace_id"] = self.workspace_id
        wire["source"] = self.source.to_wire()
        wire["temporal"] = self.temporal.to_wire()
        wire["content_checksum"] = self.content_checksum
        wire["media_type"] = self.media_type
        wire["metadata"] = _encode_json_object(self.metadata)
        wire["permission_labels"] = list(self.permission_labels)
        wire["sensitivity"] = self.sensitivity
        wire["tombstoned"] = self.tombstoned
        wire["parser_status"] = self.parser_status
        wire["ingestion_status"] = self.ingestion_status
        wire["provenance_history"] = [item.to_wire() for item in self.provenance_history]
        if self.import_run_id is not None:
            wire["import_run_id"] = self.import_run_id
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "EvidenceArtifact") -> EvidenceArtifact:
        """Decode a wire payload into a EvidenceArtifact.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_evidence_id = _decode_str(
            _require_field(mapping, "evidence_id", path),
            f"{path}.evidence_id",
        )
        field_workspace_id = _decode_str(
            _require_field(mapping, "workspace_id", path),
            f"{path}.workspace_id",
        )
        field_source = SourceReference.from_wire(
            _require_field(mapping, "source", path),
            f"{path}.source",
        )
        field_temporal = RecordTemporalMetadata.from_wire(
            _require_field(mapping, "temporal", path),
            f"{path}.temporal",
        )
        field_content_checksum = _decode_str(
            _require_field(mapping, "content_checksum", path),
            f"{path}.content_checksum",
        )
        field_media_type = _decode_str(
            _require_field(mapping, "media_type", path),
            f"{path}.media_type",
        )
        field_metadata = _decode_json_object(
            _require_field(mapping, "metadata", path),
            f"{path}.metadata",
        )
        field_permission_labels_items = _decode_sequence(
            _require_field(mapping, "permission_labels", path),
            f"{path}.permission_labels",
        )
        field_permission_labels = tuple(
            _decode_str(item, f"{path}.permission_labels[{index}]")
            for index, item in enumerate(field_permission_labels_items)
        )
        field_sensitivity = _decode_str(
            _require_field(mapping, "sensitivity", path),
            f"{path}.sensitivity",
        )
        field_tombstoned = _decode_bool(
            _require_field(mapping, "tombstoned", path),
            f"{path}.tombstoned",
        )
        field_parser_status = _decode_str(
            _require_field(mapping, "parser_status", path),
            f"{path}.parser_status",
        )
        field_ingestion_status = _decode_str(
            _require_field(mapping, "ingestion_status", path),
            f"{path}.ingestion_status",
        )
        field_provenance_history_items = _decode_sequence(
            _require_field(mapping, "provenance_history", path),
            f"{path}.provenance_history",
        )
        field_provenance_history = tuple(
            ProvenanceEntry.from_wire(item, f"{path}.provenance_history[{index}]")
            for index, item in enumerate(field_provenance_history_items)
        )
        field_import_run_id: OpaqueToken | None = None
        if "import_run_id" in mapping:
            raw_import_run_id = mapping["import_run_id"]
            if raw_import_run_id is None:
                raise ContractDecodeError(
                    f"{path}.import_run_id: null is not a valid value"
                )
            field_import_run_id = _decode_str(raw_import_run_id, f"{path}.import_run_id")
        return cls(
            evidence_id=field_evidence_id,
            workspace_id=field_workspace_id,
            source=field_source,
            temporal=field_temporal,
            content_checksum=field_content_checksum,
            media_type=field_media_type,
            metadata=field_metadata,
            permission_labels=field_permission_labels,
            sensitivity=field_sensitivity,
            tombstoned=field_tombstoned,
            parser_status=field_parser_status,
            ingestion_status=field_ingestion_status,
            provenance_history=field_provenance_history,
            import_run_id=field_import_run_id,
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
class ImportStartResult:
    """Result of `import.start`. Carries exactly one thing: the handle for the durable job that
    was started. `import.start` always returns a job and never a synchronous import outcome,
    so there is nothing else honest to return here. The response envelope's
    `ResponseMetadata.job` names the same job as this handle: one operation started one job,
    and the two statements of that fact must agree.
    """

    job: JobHandle

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["job"] = self.job.to_wire()
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "ImportStartResult") -> ImportStartResult:
        """Decode a wire payload into a ImportStartResult.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_job = JobHandle.from_wire(_require_field(mapping, "job", path), f"{path}.job")
        return cls(
            job=field_job,
        )


@dataclass(frozen=True, slots=True)
class JobCancelResult:
    """Result of `job.cancel`: what the call did, and the handle as it now stands. A state-based
    refusal is a successful, idempotent control result rather than an API error -- a job that
    cannot be cancelled returns `not_cancellable` alongside its current unchanged handle, and
    is never reported as `conflict` merely for being terminal. Authorization failures, a
    missing job, and workspace failures stay typed API errors.
    """

    job: JobHandle
    cancellation_disposition: JobCancellationDisposition

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["job"] = self.job.to_wire()
        wire["cancellation_disposition"] = self.cancellation_disposition
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "JobCancelResult") -> JobCancelResult:
        """Decode a wire payload into a JobCancelResult.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_job = JobHandle.from_wire(_require_field(mapping, "job", path), f"{path}.job")
        field_cancellation_disposition = _decode_str(
            _require_field(mapping, "cancellation_disposition", path),
            f"{path}.cancellation_disposition",
        )
        return cls(
            job=field_job,
            cancellation_disposition=field_cancellation_disposition,
        )


@dataclass(frozen=True, slots=True)
class JobRetryResult:
    """Result of `job.retry`: which recovery the server chose, and the handle as it now stands.
    An accepted recovery keeps the same job identity and returns the job to `queued`; it
    starts no new attempt until execution actually begins, so the previous terminal attempt
    remains `latest_attempt` while the recovered handle is queued. A state-based refusal is a
    successful, idempotent control result rather than an API error -- `not_retryable` is
    returned alongside the current unchanged handle, and is never reported as `conflict`
    merely for being terminal. Authorization failures, a missing job, and workspace failures
    stay typed API errors.
    """

    job: JobHandle
    recovery_disposition: JobRecoveryDisposition

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["job"] = self.job.to_wire()
        wire["recovery_disposition"] = self.recovery_disposition
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "JobRetryResult") -> JobRetryResult:
        """Decode a wire payload into a JobRetryResult.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_job = JobHandle.from_wire(_require_field(mapping, "job", path), f"{path}.job")
        field_recovery_disposition = _decode_str(
            _require_field(mapping, "recovery_disposition", path),
            f"{path}.recovery_disposition",
        )
        return cls(
            job=field_job,
            recovery_disposition=field_recovery_disposition,
        )


@dataclass(frozen=True, slots=True)
class MemoryCreateInput:
    """Input for `memory.create`: the proposed record's type, domain scope, content,
    evidence/provenance, and assertion. Carries no authority-level, reviewer/policy decision,
    governance-state, currentness, record id, version, recorded time, or supersession field,
    so a caller can never assert accepted, current-canonical, superseded, or historical
    authority through this payload; every `memory.create` result is proposed-only.
    """

    record_type: GovernedRecordType
    domain_scope: RecordDomainScope
    content: JsonObject
    evidence_disposition: EvidenceDisposition
    sources: tuple[SourceReference, ...]
    assertion: CandidateAssertion
    extraction: CandidateExtractionMetadata | None = None
    event_at: Timestamp | None = None
    observed_at: Timestamp | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["record_type"] = self.record_type
        wire["domain_scope"] = self.domain_scope
        wire["content"] = _encode_json_object(self.content)
        wire["evidence_disposition"] = self.evidence_disposition
        wire["sources"] = [item.to_wire() for item in self.sources]
        wire["assertion"] = self.assertion.to_wire()
        if self.extraction is not None:
            wire["extraction"] = self.extraction.to_wire()
        if self.event_at is not None:
            wire["event_at"] = self.event_at
        if self.observed_at is not None:
            wire["observed_at"] = self.observed_at
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "MemoryCreateInput") -> MemoryCreateInput:
        """Decode a wire payload into a MemoryCreateInput.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_record_type = _decode_str(
            _require_field(mapping, "record_type", path),
            f"{path}.record_type",
        )
        field_domain_scope = _decode_str(
            _require_field(mapping, "domain_scope", path),
            f"{path}.domain_scope",
        )
        field_content = _decode_json_object(
            _require_field(mapping, "content", path),
            f"{path}.content",
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
        field_assertion = CandidateAssertion.from_wire(
            _require_field(mapping, "assertion", path),
            f"{path}.assertion",
        )
        field_extraction: CandidateExtractionMetadata | None = None
        if "extraction" in mapping:
            raw_extraction = mapping["extraction"]
            if raw_extraction is None:
                raise ContractDecodeError(
                    f"{path}.extraction: null is not a valid value"
                )
            field_extraction = CandidateExtractionMetadata.from_wire(
                raw_extraction,
                f"{path}.extraction",
            )
        field_event_at: Timestamp | None = None
        if "event_at" in mapping:
            raw_event_at = mapping["event_at"]
            if raw_event_at is None:
                raise ContractDecodeError(
                    f"{path}.event_at: null is not a valid value"
                )
            field_event_at = _decode_str(raw_event_at, f"{path}.event_at")
        field_observed_at: Timestamp | None = None
        if "observed_at" in mapping:
            raw_observed_at = mapping["observed_at"]
            if raw_observed_at is None:
                raise ContractDecodeError(
                    f"{path}.observed_at: null is not a valid value"
                )
            field_observed_at = _decode_str(raw_observed_at, f"{path}.observed_at")
        return cls(
            record_type=field_record_type,
            domain_scope=field_domain_scope,
            content=field_content,
            evidence_disposition=field_evidence_disposition,
            sources=field_sources,
            assertion=field_assertion,
            extraction=field_extraction,
            event_at=field_event_at,
            observed_at=field_observed_at,
        )


@dataclass(frozen=True, slots=True)
class RecordProvenance:
    """The full provenance envelope for one record version: identity, temporal metadata, its
    authoring history, the sources it draws on, and the caller-supplied assertion/extraction
    lineage the claim in this version came from. `assertion`/`extraction` are structurally
    optional so a record written before they existed still decodes, but a governance
    transition that replaces or carries forward a claim must bind them; enforcing that is a
    semantic-validation concern, not a wire-shape one.
    """

    identity: RecordIdentity
    temporal: RecordTemporalMetadata
    history: tuple[ProvenanceEntry, ...]
    evidence_disposition: EvidenceDisposition
    sources: tuple[SourceReference, ...]
    assertion: CandidateAssertion | None = None
    extraction: CandidateExtractionMetadata | None = None

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
        if self.assertion is not None:
            wire["assertion"] = self.assertion.to_wire()
        if self.extraction is not None:
            wire["extraction"] = self.extraction.to_wire()
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
        field_assertion: CandidateAssertion | None = None
        if "assertion" in mapping:
            raw_assertion = mapping["assertion"]
            if raw_assertion is None:
                raise ContractDecodeError(
                    f"{path}.assertion: null is not a valid value"
                )
            field_assertion = CandidateAssertion.from_wire(raw_assertion, f"{path}.assertion")
        field_extraction: CandidateExtractionMetadata | None = None
        if "extraction" in mapping:
            raw_extraction = mapping["extraction"]
            if raw_extraction is None:
                raise ContractDecodeError(
                    f"{path}.extraction: null is not a valid value"
                )
            field_extraction = CandidateExtractionMetadata.from_wire(
                raw_extraction,
                f"{path}.extraction",
            )
        return cls(
            identity=field_identity,
            temporal=field_temporal,
            history=field_history,
            evidence_disposition=field_evidence_disposition,
            sources=field_sources,
            assertion=field_assertion,
            extraction=field_extraction,
        )


@dataclass(frozen=True, slots=True)
class WorkspaceListResult:
    """Result of `workspace.list`."""

    workspaces: tuple[WorkspaceDescriptor, ...]
    page: PageMetadata

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["workspaces"] = [item.to_wire() for item in self.workspaces]
        wire["page"] = self.page.to_wire()
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "WorkspaceListResult") -> WorkspaceListResult:
        """Decode a wire payload into a WorkspaceListResult.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_workspaces_items = _decode_sequence(
            _require_field(mapping, "workspaces", path),
            f"{path}.workspaces",
        )
        field_workspaces = tuple(
            WorkspaceDescriptor.from_wire(item, f"{path}.workspaces[{index}]")
            for index, item in enumerate(field_workspaces_items)
        )
        field_page = PageMetadata.from_wire(_require_field(mapping, "page", path), f"{path}.page")
        return cls(
            workspaces=field_workspaces,
            page=field_page,
        )


@dataclass(frozen=True, slots=True)
class WorkspaceCreateResult:
    """Result of `workspace.create`: the concrete created workspace, including its server-
    assigned identifier and format compatibility. Never a sentinel or placeholder workspace
    identifier.
    """

    workspace: WorkspaceDescriptor

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["workspace"] = self.workspace.to_wire()
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "WorkspaceCreateResult"
    ) -> WorkspaceCreateResult:
        """Decode a wire payload into a WorkspaceCreateResult.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_workspace = WorkspaceDescriptor.from_wire(
            _require_field(mapping, "workspace", path),
            f"{path}.workspace",
        )
        return cls(
            workspace=field_workspace,
        )


@dataclass(frozen=True, slots=True)
class WorkspaceInspectResult:
    """Result of `workspace.inspect`: the envelope-selected workspace's concrete descriptor."""

    workspace: WorkspaceDescriptor

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["workspace"] = self.workspace.to_wire()
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "WorkspaceInspectResult"
    ) -> WorkspaceInspectResult:
        """Decode a wire payload into a WorkspaceInspectResult.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_workspace = WorkspaceDescriptor.from_wire(
            _require_field(mapping, "workspace", path),
            f"{path}.workspace",
        )
        return cls(
            workspace=field_workspace,
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


@dataclass(frozen=True, slots=True)
class EvidenceSearchResult:
    """Result of `evidence.search`: complete, append-preserving L0 evidence artifacts with exact
    provenance. Never substitutes a `GovernedRecord` for an evidence artifact.
    """

    evidence: tuple[EvidenceArtifact, ...]
    page: PageMetadata

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["evidence"] = [item.to_wire() for item in self.evidence]
        wire["page"] = self.page.to_wire()
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "EvidenceSearchResult") -> EvidenceSearchResult:
        """Decode a wire payload into a EvidenceSearchResult.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_evidence_items = _decode_sequence(
            _require_field(mapping, "evidence", path),
            f"{path}.evidence",
        )
        field_evidence = tuple(
            EvidenceArtifact.from_wire(item, f"{path}.evidence[{index}]")
            for index, item in enumerate(field_evidence_items)
        )
        field_page = PageMetadata.from_wire(_require_field(mapping, "page", path), f"{path}.page")
        return cls(
            evidence=field_evidence,
            page=field_page,
        )


@dataclass(frozen=True, slots=True)
class JobGetResult:
    """Result of `job.get`: the current handle, plus the terminal result when the job has one.
    `terminal_result` is present exactly when `job.state` is a known terminal state, and it
    is closed against the handle it accompanies: identity and state match exactly, every
    attempt instant in the terminal history falls inside the handle's own
    `created_at`/`updated_at` lifetime, and the handle's `latest_attempt` is exactly the
    final attempt of that history (and is absent exactly when the history is). One read
    describes one job, never two disagreeing statements about it. An unknown state is
    preserved but implies nothing: a handle in a state this build has never seen carries no
    `terminal_result`, because this build cannot know whether that state is terminal.
    """

    job: JobHandle
    terminal_result: JobTerminalResult | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["job"] = self.job.to_wire()
        if self.terminal_result is not None:
            wire["terminal_result"] = job_terminal_result_to_wire(self.terminal_result)
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "JobGetResult") -> JobGetResult:
        """Decode a wire payload into a JobGetResult.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_job = JobHandle.from_wire(_require_field(mapping, "job", path), f"{path}.job")
        field_terminal_result: JobTerminalResult | None = None
        if "terminal_result" in mapping:
            raw_terminal_result = mapping["terminal_result"]
            if raw_terminal_result is None:
                raise ContractDecodeError(
                    f"{path}.terminal_result: null is not a valid value"
                )
            field_terminal_result = job_terminal_result_from_wire(
                raw_terminal_result,
                f"{path}.terminal_result",
            )
        return cls(
            job=field_job,
            terminal_result=field_terminal_result,
        )


@dataclass(frozen=True, slots=True)
class RecordSupersedeInput:
    """Input for `record.supersede`: an explicit, authorized replacement of a current accepted
    (`l2`/`accepted`/`current`) governed record with a new accepted version. The target
    version is the envelope's `MutationPrecondition.record_version`, not duplicated here.
    Exactly three fields: which record, the complete replacement claim, and why. The
    replacement is a whole `MemoryCreateInput` rather than a loose bag of content/evidence
    fields, so the new version's content, evidence disposition, sources, assertion,
    extraction lineage, and proposed validity window are supplied and validated as one
    coherent claim under exactly the rules `memory.create` already enforces. It inherits that
    shape's least-authority-escalating guarantee: no governance-state, currentness,
    authority-level, reviewer, or supersession field is accepted from a caller. The server
    alone produces the new version's identity, authority, temporal envelope, and the
    reciprocal `supersedes`/`superseded_by` pointers, and the replacement's
    `record_type`/`domain_scope` must equal the superseded record's -- superseding replaces a
    claim, it never silently reclassifies the record.
    """

    record_id: RecordId
    replacement: MemoryCreateInput
    rationale: GovernanceRationale

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["record_id"] = self.record_id
        wire["replacement"] = self.replacement.to_wire()
        wire["rationale"] = self.rationale.to_wire()
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "RecordSupersedeInput") -> RecordSupersedeInput:
        """Decode a wire payload into a RecordSupersedeInput.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_record_id = _decode_str(
            _require_field(mapping, "record_id", path),
            f"{path}.record_id",
        )
        field_replacement = MemoryCreateInput.from_wire(
            _require_field(mapping, "replacement", path),
            f"{path}.replacement",
        )
        field_rationale = GovernanceRationale.from_wire(
            _require_field(mapping, "rationale", path),
            f"{path}.rationale",
        )
        return cls(
            record_id=field_record_id,
            replacement=field_replacement,
            rationale=field_rationale,
        )


@dataclass(frozen=True, slots=True)
class GovernedRecord:
    """A provider-neutral governed record: which workspace it belongs to, what kind of record it
    is, its domain scope and authority level, its full L0-L4 governance, temporal, evidence,
    and provenance envelope, and its opaque JSON content. Carries no reference to, and is not
    a substitute for, any repo-local `Memory`, `MemoryFact`, or `SourceRef` domain class.
    """

    workspace_id: WorkspaceId
    record_type: GovernedRecordType
    domain_scope: RecordDomainScope
    authority_level: OpenCode
    provenance: RecordProvenance
    content: JsonObject
    reviewer: Identifier | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["workspace_id"] = self.workspace_id
        wire["record_type"] = self.record_type
        wire["domain_scope"] = self.domain_scope
        wire["authority_level"] = self.authority_level
        if self.reviewer is not None:
            wire["reviewer"] = self.reviewer
        wire["provenance"] = self.provenance.to_wire()
        wire["content"] = _encode_json_object(self.content)
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "GovernedRecord") -> GovernedRecord:
        """Decode a wire payload into a GovernedRecord.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_workspace_id = _decode_str(
            _require_field(mapping, "workspace_id", path),
            f"{path}.workspace_id",
        )
        field_record_type = _decode_str(
            _require_field(mapping, "record_type", path),
            f"{path}.record_type",
        )
        field_domain_scope = _decode_str(
            _require_field(mapping, "domain_scope", path),
            f"{path}.domain_scope",
        )
        field_authority_level = _decode_str(
            _require_field(mapping, "authority_level", path),
            f"{path}.authority_level",
        )
        field_reviewer: Identifier | None = None
        if "reviewer" in mapping:
            raw_reviewer = mapping["reviewer"]
            if raw_reviewer is None:
                raise ContractDecodeError(
                    f"{path}.reviewer: null is not a valid value"
                )
            field_reviewer = _decode_str(raw_reviewer, f"{path}.reviewer")
        field_provenance = RecordProvenance.from_wire(
            _require_field(mapping, "provenance", path),
            f"{path}.provenance",
        )
        field_content = _decode_json_object(
            _require_field(mapping, "content", path),
            f"{path}.content",
        )
        return cls(
            workspace_id=field_workspace_id,
            record_type=field_record_type,
            domain_scope=field_domain_scope,
            authority_level=field_authority_level,
            reviewer=field_reviewer,
            provenance=field_provenance,
            content=field_content,
        )


@dataclass(frozen=True, slots=True)
class ContextPackBuildResult:
    """Result of `context_pack.build`: the original query, the model-facing sections, the
    selected L0 evidence, current canonical L2 records, supporting history and L3 context
    models, the exact citations every section and selected item rests on, the conflicts and
    uncertainties the pack surfaces rather than resolving, the policy and budget omissions,
    token accounting, and the complete reproducibility record. Selecting and citing this
    content never grants new authority: `fresh_authorization_required` is always true, and
    possessing `pack_id` grants nothing on its own -- it is a content digest anyone can
    recompute, not a capability.
    """

    pack_id: ContextPackDigest
    mode: ContextPackMode
    query: MemoryQuery
    sections: tuple[ContextPackSection, ...]
    evidence: tuple[EvidenceArtifact, ...]
    records: tuple[GovernedRecord, ...]
    history: tuple[GovernedRecord, ...]
    context_models: tuple[GovernedRecord, ...]
    citations: tuple[ContextPackCitation, ...]
    conflicts: tuple[ContextPackConflict, ...]
    uncertainties: tuple[ContextPackUncertainty, ...]
    omissions: tuple[Omission, ...]
    budget: ContextPackBudget
    reproducibility: ContextPackReproducibility
    fresh_authorization_required: bool

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["pack_id"] = self.pack_id
        wire["mode"] = self.mode
        wire["query"] = self.query
        wire["sections"] = [item.to_wire() for item in self.sections]
        wire["evidence"] = [item.to_wire() for item in self.evidence]
        wire["records"] = [item.to_wire() for item in self.records]
        wire["history"] = [item.to_wire() for item in self.history]
        wire["context_models"] = [item.to_wire() for item in self.context_models]
        wire["citations"] = [context_pack_citation_to_wire(item) for item in self.citations]
        wire["conflicts"] = [item.to_wire() for item in self.conflicts]
        wire["uncertainties"] = [item.to_wire() for item in self.uncertainties]
        wire["omissions"] = [item.to_wire() for item in self.omissions]
        wire["budget"] = self.budget.to_wire()
        wire["reproducibility"] = self.reproducibility.to_wire()
        wire["fresh_authorization_required"] = self.fresh_authorization_required
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "ContextPackBuildResult"
    ) -> ContextPackBuildResult:
        """Decode a wire payload into a ContextPackBuildResult.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_pack_id = _decode_str(_require_field(mapping, "pack_id", path), f"{path}.pack_id")
        field_mode = _decode_str(_require_field(mapping, "mode", path), f"{path}.mode")
        field_query = _decode_str(_require_field(mapping, "query", path), f"{path}.query")
        field_sections_items = _decode_sequence(
            _require_field(mapping, "sections", path),
            f"{path}.sections",
        )
        field_sections = tuple(
            ContextPackSection.from_wire(item, f"{path}.sections[{index}]")
            for index, item in enumerate(field_sections_items)
        )
        field_evidence_items = _decode_sequence(
            _require_field(mapping, "evidence", path),
            f"{path}.evidence",
        )
        field_evidence = tuple(
            EvidenceArtifact.from_wire(item, f"{path}.evidence[{index}]")
            for index, item in enumerate(field_evidence_items)
        )
        field_records_items = _decode_sequence(
            _require_field(mapping, "records", path),
            f"{path}.records",
        )
        field_records = tuple(
            GovernedRecord.from_wire(item, f"{path}.records[{index}]")
            for index, item in enumerate(field_records_items)
        )
        field_history_items = _decode_sequence(
            _require_field(mapping, "history", path),
            f"{path}.history",
        )
        field_history = tuple(
            GovernedRecord.from_wire(item, f"{path}.history[{index}]")
            for index, item in enumerate(field_history_items)
        )
        field_context_models_items = _decode_sequence(
            _require_field(mapping, "context_models", path),
            f"{path}.context_models",
        )
        field_context_models = tuple(
            GovernedRecord.from_wire(item, f"{path}.context_models[{index}]")
            for index, item in enumerate(field_context_models_items)
        )
        field_citations_items = _decode_sequence(
            _require_field(mapping, "citations", path),
            f"{path}.citations",
        )
        field_citations = tuple(
            context_pack_citation_from_wire(item, f"{path}.citations[{index}]")
            for index, item in enumerate(field_citations_items)
        )
        field_conflicts_items = _decode_sequence(
            _require_field(mapping, "conflicts", path),
            f"{path}.conflicts",
        )
        field_conflicts = tuple(
            ContextPackConflict.from_wire(item, f"{path}.conflicts[{index}]")
            for index, item in enumerate(field_conflicts_items)
        )
        field_uncertainties_items = _decode_sequence(
            _require_field(mapping, "uncertainties", path),
            f"{path}.uncertainties",
        )
        field_uncertainties = tuple(
            ContextPackUncertainty.from_wire(item, f"{path}.uncertainties[{index}]")
            for index, item in enumerate(field_uncertainties_items)
        )
        field_omissions_items = _decode_sequence(
            _require_field(mapping, "omissions", path),
            f"{path}.omissions",
        )
        field_omissions = tuple(
            Omission.from_wire(item, f"{path}.omissions[{index}]")
            for index, item in enumerate(field_omissions_items)
        )
        field_budget = ContextPackBudget.from_wire(
            _require_field(mapping, "budget", path),
            f"{path}.budget",
        )
        field_reproducibility = ContextPackReproducibility.from_wire(
            _require_field(mapping, "reproducibility", path),
            f"{path}.reproducibility",
        )
        field_fresh_authorization_required = _decode_bool(
            _require_field(mapping, "fresh_authorization_required", path),
            f"{path}.fresh_authorization_required",
        )
        return cls(
            pack_id=field_pack_id,
            mode=field_mode,
            query=field_query,
            sections=field_sections,
            evidence=field_evidence,
            records=field_records,
            history=field_history,
            context_models=field_context_models,
            citations=field_citations,
            conflicts=field_conflicts,
            uncertainties=field_uncertainties,
            omissions=field_omissions,
            budget=field_budget,
            reproducibility=field_reproducibility,
            fresh_authorization_required=field_fresh_authorization_required,
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


@dataclass(frozen=True, slots=True)
class GraphNode:
    """One node in a traversal result: a precise reference to the canonical governed record
    version it represents, the full governed record it wraps, and the depth at which this
    traversal reached it. Never carries a competing identity, provenance, lifecycle,
    authority, or governance state of its own -- `reference` and `record` are the only
    sources of truth, and they must agree.
    """

    reference: RecordVersionReference
    record: GovernedRecord
    depth: GraphDepthLimit

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["reference"] = self.reference.to_wire()
        wire["record"] = self.record.to_wire()
        wire["depth"] = self.depth
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "GraphNode") -> GraphNode:
        """Decode a wire payload into a GraphNode.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_reference = RecordVersionReference.from_wire(
            _require_field(mapping, "reference", path),
            f"{path}.reference",
        )
        field_record = GovernedRecord.from_wire(
            _require_field(mapping, "record", path),
            f"{path}.record",
        )
        field_depth = _decode_int(_require_field(mapping, "depth", path), f"{path}.depth")
        return cls(
            reference=field_reference,
            record=field_record,
            depth=field_depth,
        )


@dataclass(frozen=True, slots=True)
class GraphEdge:
    """One edge in a traversal result: the relation type, its source and target governed-record
    versions, the relation's own full governed record, and the precise reference identifying
    that relation record. Never carries a competing identity, provenance, lifecycle,
    authority, or governance state of its own beyond that wrapped record:
    `relation_reference` must identify `record.provenance.identity` exactly, so the relation
    record is referenced, never re-identified. `source` and `target` are structurally
    optional so a result can represent a justified page/depth boundary where one end of a
    relation was not reached; at least one must be present, and exactly one may be absent
    only together with a coherent `boundary_reason`. Both endpoints present means a fully
    materialized edge and forbids `boundary_reason`; both absent is never representable,
    since an edge that names no returned node states nothing this result can be trusted
    about.
    """

    relation_type: GraphRelationType
    record: GovernedRecord
    relation_reference: RecordVersionReference
    source: RecordVersionReference | None = None
    target: RecordVersionReference | None = None
    boundary_reason: GraphBoundaryReason | None = None

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["relation_type"] = self.relation_type
        if self.source is not None:
            wire["source"] = self.source.to_wire()
        if self.target is not None:
            wire["target"] = self.target.to_wire()
        wire["record"] = self.record.to_wire()
        wire["relation_reference"] = self.relation_reference.to_wire()
        if self.boundary_reason is not None:
            wire["boundary_reason"] = self.boundary_reason
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "GraphEdge") -> GraphEdge:
        """Decode a wire payload into a GraphEdge.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_relation_type = _decode_str(
            _require_field(mapping, "relation_type", path),
            f"{path}.relation_type",
        )
        field_source: RecordVersionReference | None = None
        if "source" in mapping:
            raw_source = mapping["source"]
            if raw_source is None:
                raise ContractDecodeError(
                    f"{path}.source: null is not a valid value"
                )
            field_source = RecordVersionReference.from_wire(raw_source, f"{path}.source")
        field_target: RecordVersionReference | None = None
        if "target" in mapping:
            raw_target = mapping["target"]
            if raw_target is None:
                raise ContractDecodeError(
                    f"{path}.target: null is not a valid value"
                )
            field_target = RecordVersionReference.from_wire(raw_target, f"{path}.target")
        field_record = GovernedRecord.from_wire(
            _require_field(mapping, "record", path),
            f"{path}.record",
        )
        field_relation_reference = RecordVersionReference.from_wire(
            _require_field(mapping, "relation_reference", path),
            f"{path}.relation_reference",
        )
        field_boundary_reason: GraphBoundaryReason | None = None
        if "boundary_reason" in mapping:
            raw_boundary_reason = mapping["boundary_reason"]
            if raw_boundary_reason is None:
                raise ContractDecodeError(
                    f"{path}.boundary_reason: null is not a valid value"
                )
            field_boundary_reason = _decode_str(raw_boundary_reason, f"{path}.boundary_reason")
        return cls(
            relation_type=field_relation_type,
            source=field_source,
            target=field_target,
            record=field_record,
            relation_reference=field_relation_reference,
            boundary_reason=field_boundary_reason,
        )


@dataclass(frozen=True, slots=True)
class KnowledgeSearchResult:
    """Result of `knowledge.search`. When the request's `view` was absent or
    `current_canonical`, every returned record must be the exact accepted, current, canonical
    version; no candidate, rejected, superseded, or non-canonical-layer record may appear.
    """

    records: tuple[GovernedRecord, ...]
    page: PageMetadata

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["records"] = [item.to_wire() for item in self.records]
        wire["page"] = self.page.to_wire()
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "KnowledgeSearchResult"
    ) -> KnowledgeSearchResult:
        """Decode a wire payload into a KnowledgeSearchResult.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_records_items = _decode_sequence(
            _require_field(mapping, "records", path),
            f"{path}.records",
        )
        field_records = tuple(
            GovernedRecord.from_wire(item, f"{path}.records[{index}]")
            for index, item in enumerate(field_records_items)
        )
        field_page = PageMetadata.from_wire(_require_field(mapping, "page", path), f"{path}.page")
        return cls(
            records=field_records,
            page=field_page,
        )


@dataclass(frozen=True, slots=True)
class KnowledgeProposeResult:
    """Result of `knowledge.propose`: both versions of the record as they stand *after* the
    transition, so a caller can validate the transition and confirm no history or provenance
    was lost. `previous_record` is the prior version, which the transition has itself marked
    superseded and pointed at the new one; `updated_record` is the newly current version.
    Neither is a pre-transition snapshot: `previous_record` is what that version now is, not
    what it looked like before the call.
    """

    previous_record: GovernedRecord
    updated_record: GovernedRecord

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["previous_record"] = self.previous_record.to_wire()
        wire["updated_record"] = self.updated_record.to_wire()
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "KnowledgeProposeResult"
    ) -> KnowledgeProposeResult:
        """Decode a wire payload into a KnowledgeProposeResult.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_previous_record = GovernedRecord.from_wire(
            _require_field(mapping, "previous_record", path),
            f"{path}.previous_record",
        )
        field_updated_record = GovernedRecord.from_wire(
            _require_field(mapping, "updated_record", path),
            f"{path}.updated_record",
        )
        return cls(
            previous_record=field_previous_record,
            updated_record=field_updated_record,
        )


@dataclass(frozen=True, slots=True)
class CandidateApproveResult:
    """Result of `candidate.approve`: both versions of the record as they stand *after*
    approval, so a caller can validate the transition and confirm no history or provenance
    was lost. `previous_record` is the prior candidate version, which the approval has itself
    marked superseded and pointed at the new one; `updated_record` is the newly current
    version. Neither is a pre-transition snapshot: `previous_record` is what that version now
    is, not what it looked like before the call.
    """

    previous_record: GovernedRecord
    updated_record: GovernedRecord

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["previous_record"] = self.previous_record.to_wire()
        wire["updated_record"] = self.updated_record.to_wire()
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "CandidateApproveResult"
    ) -> CandidateApproveResult:
        """Decode a wire payload into a CandidateApproveResult.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_previous_record = GovernedRecord.from_wire(
            _require_field(mapping, "previous_record", path),
            f"{path}.previous_record",
        )
        field_updated_record = GovernedRecord.from_wire(
            _require_field(mapping, "updated_record", path),
            f"{path}.updated_record",
        )
        return cls(
            previous_record=field_previous_record,
            updated_record=field_updated_record,
        )


@dataclass(frozen=True, slots=True)
class CandidateRejectResult:
    """Result of `candidate.reject`: both versions of the record as they stand *after*
    rejection, so a caller can validate the transition and confirm no history or provenance
    was lost. `previous_record` is the prior candidate version, which the rejection has
    itself marked superseded and pointed at the new one; `updated_record` is the newly
    current version. Neither is a pre-transition snapshot: `previous_record` is what that
    version now is, not what it looked like before the call.
    """

    previous_record: GovernedRecord
    updated_record: GovernedRecord

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["previous_record"] = self.previous_record.to_wire()
        wire["updated_record"] = self.updated_record.to_wire()
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "CandidateRejectResult"
    ) -> CandidateRejectResult:
        """Decode a wire payload into a CandidateRejectResult.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_previous_record = GovernedRecord.from_wire(
            _require_field(mapping, "previous_record", path),
            f"{path}.previous_record",
        )
        field_updated_record = GovernedRecord.from_wire(
            _require_field(mapping, "updated_record", path),
            f"{path}.updated_record",
        )
        return cls(
            previous_record=field_previous_record,
            updated_record=field_updated_record,
        )


@dataclass(frozen=True, slots=True)
class RecordSupersedeResult:
    """Result of `record.supersede`: the prior current record (now superseded) and the new
    current record, so a caller can validate the reciprocal `supersedes`/`superseded_by`
    pointers, the preserved stable record identity, and the complete, unerased provenance
    history.
    """

    previous_record: GovernedRecord
    updated_record: GovernedRecord

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["previous_record"] = self.previous_record.to_wire()
        wire["updated_record"] = self.updated_record.to_wire()
        return wire

    @classmethod
    def from_wire(
        cls, payload: object, path: str = "RecordSupersedeResult"
    ) -> RecordSupersedeResult:
        """Decode a wire payload into a RecordSupersedeResult.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_previous_record = GovernedRecord.from_wire(
            _require_field(mapping, "previous_record", path),
            f"{path}.previous_record",
        )
        field_updated_record = GovernedRecord.from_wire(
            _require_field(mapping, "updated_record", path),
            f"{path}.updated_record",
        )
        return cls(
            previous_record=field_previous_record,
            updated_record=field_updated_record,
        )


@dataclass(frozen=True, slots=True)
class MemoryCreateResult:
    """Result of `memory.create`: the resulting proposed governed record.
    `provenance.identity.governance_state` is always `proposed`; this operation never creates
    accepted canonical knowledge.
    """

    record: GovernedRecord

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["record"] = self.record.to_wire()
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "MemoryCreateResult") -> MemoryCreateResult:
        """Decode a wire payload into a MemoryCreateResult.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_record = GovernedRecord.from_wire(
            _require_field(mapping, "record", path),
            f"{path}.record",
        )
        return cls(
            record=field_record,
        )


@dataclass(frozen=True, slots=True)
class MemoryGetResult:
    """Result of `memory.get`: the governed record."""

    record: GovernedRecord

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["record"] = self.record.to_wire()
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "MemoryGetResult") -> MemoryGetResult:
        """Decode a wire payload into a MemoryGetResult.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_record = GovernedRecord.from_wire(
            _require_field(mapping, "record", path),
            f"{path}.record",
        )
        return cls(
            record=field_record,
        )


@dataclass(frozen=True, slots=True)
class MemoryListResult:
    """Result of `memory.list`."""

    records: tuple[GovernedRecord, ...]
    page: PageMetadata

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["records"] = [item.to_wire() for item in self.records]
        wire["page"] = self.page.to_wire()
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "MemoryListResult") -> MemoryListResult:
        """Decode a wire payload into a MemoryListResult.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_records_items = _decode_sequence(
            _require_field(mapping, "records", path),
            f"{path}.records",
        )
        field_records = tuple(
            GovernedRecord.from_wire(item, f"{path}.records[{index}]")
            for index, item in enumerate(field_records_items)
        )
        field_page = PageMetadata.from_wire(_require_field(mapping, "page", path), f"{path}.page")
        return cls(
            records=field_records,
            page=field_page,
        )


@dataclass(frozen=True, slots=True)
class MemorySearchResult:
    """Result of `memory.search`."""

    records: tuple[GovernedRecord, ...]
    page: PageMetadata

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["records"] = [item.to_wire() for item in self.records]
        wire["page"] = self.page.to_wire()
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "MemorySearchResult") -> MemorySearchResult:
        """Decode a wire payload into a MemorySearchResult.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_records_items = _decode_sequence(
            _require_field(mapping, "records", path),
            f"{path}.records",
        )
        field_records = tuple(
            GovernedRecord.from_wire(item, f"{path}.records[{index}]")
            for index, item in enumerate(field_records_items)
        )
        field_page = PageMetadata.from_wire(_require_field(mapping, "page", path), f"{path}.page")
        return cls(
            records=field_records,
            page=field_page,
        )


@dataclass(frozen=True, slots=True)
class GraphTraversalResult:
    """Result of `graph.traverse`: the traversed nodes and edges, the traversal limits actually
    applied (which may be tighter than requested but never looser), the projection
    metadata/watermark this traversal was served from, and deterministic ordering evidence.
    Boundaries are stated, never implied: an edge whose source or target this traversal did
    not reach carries the endpoint absent plus a `boundary_reason` that must actually hold
    here -- `page_boundary` only when `page` offers a continuation token and `nodes` reached
    `applied_node_limit` exactly, `depth_boundary` only when the endpoint that *is* present
    is a returned node sitting at `applied_depth_limit`. Projection loss is never canonical-
    data loss: an absent endpoint says this page stopped, not that the relation lost an end.
    """

    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    applied_depth_limit: GraphDepthLimit
    applied_node_limit: PageLimit
    applied_edge_limit: PageLimit
    freshness: ProjectionFreshness
    ordering_basis: GraphOrderingBasis
    page: PageMetadata

    def to_wire(self) -> dict[str, Any]:
        """Render this value as a JSON-compatible mapping.

        Absent optional fields are omitted rather than emitted as null, so a decode/encode
        round trip reproduces the original document exactly.
        """
        wire: dict[str, Any] = {}
        wire["nodes"] = [item.to_wire() for item in self.nodes]
        wire["edges"] = [item.to_wire() for item in self.edges]
        wire["applied_depth_limit"] = self.applied_depth_limit
        wire["applied_node_limit"] = self.applied_node_limit
        wire["applied_edge_limit"] = self.applied_edge_limit
        wire["freshness"] = self.freshness.to_wire()
        wire["ordering_basis"] = self.ordering_basis
        wire["page"] = self.page.to_wire()
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "GraphTraversalResult") -> GraphTraversalResult:
        """Decode a wire payload into a GraphTraversalResult.

        Unknown fields are ignored so a newer peer's additive minor release still decodes
        here. Missing required fields and wrongly typed values raise ContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_nodes_items = _decode_sequence(
            _require_field(mapping, "nodes", path),
            f"{path}.nodes",
        )
        field_nodes = tuple(
            GraphNode.from_wire(item, f"{path}.nodes[{index}]")
            for index, item in enumerate(field_nodes_items)
        )
        field_edges_items = _decode_sequence(
            _require_field(mapping, "edges", path),
            f"{path}.edges",
        )
        field_edges = tuple(
            GraphEdge.from_wire(item, f"{path}.edges[{index}]")
            for index, item in enumerate(field_edges_items)
        )
        field_applied_depth_limit = _decode_int(
            _require_field(mapping, "applied_depth_limit", path),
            f"{path}.applied_depth_limit",
        )
        field_applied_node_limit = _decode_int(
            _require_field(mapping, "applied_node_limit", path),
            f"{path}.applied_node_limit",
        )
        field_applied_edge_limit = _decode_int(
            _require_field(mapping, "applied_edge_limit", path),
            f"{path}.applied_edge_limit",
        )
        field_freshness = ProjectionFreshness.from_wire(
            _require_field(mapping, "freshness", path),
            f"{path}.freshness",
        )
        field_ordering_basis = _decode_str(
            _require_field(mapping, "ordering_basis", path),
            f"{path}.ordering_basis",
        )
        field_page = PageMetadata.from_wire(_require_field(mapping, "page", path), f"{path}.page")
        return cls(
            nodes=field_nodes,
            edges=field_edges,
            applied_depth_limit=field_applied_depth_limit,
            applied_node_limit=field_applied_node_limit,
            applied_edge_limit=field_applied_edge_limit,
            freshness=field_freshness,
            ordering_basis=field_ordering_basis,
            page=field_page,
        )


# --- operation catalogue ---------------------------------------------------

OPERATION_CATALOGUE: Final[tuple[OperationMetadata, ...]] = (
    OperationMetadata(
        name="candidate.approve",
        scope=OperationScope(
            required_scopes=("memory:write",),
            side_effect="update",
            scope_kind="workspace",
        ),
        input_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/knowledge.schema.json"
            "#/$defs/CandidateApproveInput"
        ),
        result_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/knowledge.schema.json"
            "#/$defs/CandidateApproveResult"
        ),
        required_capability=CapabilityRequirement(
            id="knowledge.govern",
            minimum_version="1.0",
            required=True,
        ),
        job=OperationJobMetadata(completion_mode="synchronous"),
        pagination=OperationPaginationMetadata(paginated=False),
        idempotency=OperationIdempotencyMetadata(
            supports_idempotency_key=True,
            required=True,
            safe_to_retry=False,
        ),
        precondition=OperationPreconditionMetadata(
            supports_mutation_precondition=True,
            required=True,
        ),
        audit=OperationAuditMetadata(audited=True, audit_category="mutation"),
        allowed_errors=(
            "authentication_required",
            "authorization_denied",
            "cancelled",
            "capability_not_granted",
            "conflict",
            "deadline_exceeded",
            "dependency_unavailable",
            "idempotency_conflict",
            "incompatible_version",
            "internal_non_recoverable",
            "internal_recoverable",
            "invalid_purpose",
            "invalid_request",
            "mutation_precondition_failed",
            "not_found",
            "rate_limited",
            "upgrade_required",
            "workspace_busy",
            "workspace_lease_unavailable",
            "workspace_migration_required",
            "workspace_not_granted",
        ),
    ),
    OperationMetadata(
        name="candidate.reject",
        scope=OperationScope(
            required_scopes=("memory:write",),
            side_effect="update",
            scope_kind="workspace",
        ),
        input_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/knowledge.schema.json"
            "#/$defs/CandidateRejectInput"
        ),
        result_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/knowledge.schema.json"
            "#/$defs/CandidateRejectResult"
        ),
        required_capability=CapabilityRequirement(
            id="knowledge.govern",
            minimum_version="1.0",
            required=True,
        ),
        job=OperationJobMetadata(completion_mode="synchronous"),
        pagination=OperationPaginationMetadata(paginated=False),
        idempotency=OperationIdempotencyMetadata(
            supports_idempotency_key=True,
            required=True,
            safe_to_retry=False,
        ),
        precondition=OperationPreconditionMetadata(
            supports_mutation_precondition=True,
            required=True,
        ),
        audit=OperationAuditMetadata(audited=True, audit_category="mutation"),
        allowed_errors=(
            "authentication_required",
            "authorization_denied",
            "cancelled",
            "capability_not_granted",
            "conflict",
            "deadline_exceeded",
            "dependency_unavailable",
            "idempotency_conflict",
            "incompatible_version",
            "internal_non_recoverable",
            "internal_recoverable",
            "invalid_purpose",
            "invalid_request",
            "mutation_precondition_failed",
            "not_found",
            "rate_limited",
            "upgrade_required",
            "workspace_busy",
            "workspace_lease_unavailable",
            "workspace_migration_required",
            "workspace_not_granted",
        ),
    ),
    OperationMetadata(
        name="context_pack.build",
        scope=OperationScope(
            required_scopes=("memory:read",),
            side_effect="none",
            scope_kind="workspace",
        ),
        input_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/context-pack.schema.json"
            "#/$defs/ContextPackBuildInput"
        ),
        result_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/conte"
            "xt-pack.schema.json#/$defs/ContextPackBuildResult"
        ),
        required_capability=CapabilityRequirement(
            id="context_pack.build",
            minimum_version="1.0",
            required=True,
        ),
        job=OperationJobMetadata(completion_mode="synchronous"),
        pagination=OperationPaginationMetadata(paginated=False),
        idempotency=OperationIdempotencyMetadata(
            supports_idempotency_key=False,
            required=False,
            safe_to_retry=True,
        ),
        precondition=OperationPreconditionMetadata(
            supports_mutation_precondition=False,
            required=False,
        ),
        audit=OperationAuditMetadata(audited=True, audit_category="read"),
        allowed_errors=(
            "authentication_required",
            "authorization_denied",
            "cancelled",
            "capability_not_granted",
            "deadline_exceeded",
            "dependency_unavailable",
            "incompatible_version",
            "internal_non_recoverable",
            "internal_recoverable",
            "invalid_purpose",
            "invalid_request",
            "projection_unavailable",
            "rate_limited",
            "size_limit_exceeded",
            "stale_projection",
            "token_limit_exceeded",
            "upgrade_required",
            "workspace_migration_required",
            "workspace_not_granted",
        ),
    ),
    OperationMetadata(
        name="evidence.search",
        scope=OperationScope(
            required_scopes=("memory:read",),
            side_effect="none",
            scope_kind="workspace",
        ),
        input_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/evidence.schema.json"
            "#/$defs/EvidenceSearchInput"
        ),
        result_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/evidence.schema.json"
            "#/$defs/EvidenceSearchResult"
        ),
        required_capability=CapabilityRequirement(
            id="evidence.read",
            minimum_version="1.0",
            required=True,
        ),
        job=OperationJobMetadata(completion_mode="synchronous"),
        pagination=OperationPaginationMetadata(paginated=True, max_page_size=1000),
        idempotency=OperationIdempotencyMetadata(
            supports_idempotency_key=False,
            required=False,
            safe_to_retry=True,
        ),
        precondition=OperationPreconditionMetadata(
            supports_mutation_precondition=False,
            required=False,
        ),
        audit=OperationAuditMetadata(audited=True, audit_category="read"),
        allowed_errors=(
            "authentication_required",
            "authorization_denied",
            "cancelled",
            "capability_not_granted",
            "deadline_exceeded",
            "dependency_unavailable",
            "incompatible_version",
            "internal_non_recoverable",
            "internal_recoverable",
            "invalid_purpose",
            "invalid_request",
            "projection_unavailable",
            "rate_limited",
            "stale_projection",
            "upgrade_required",
            "workspace_migration_required",
            "workspace_not_granted",
        ),
    ),
    OperationMetadata(
        name="graph.traverse",
        scope=OperationScope(
            required_scopes=("graph:read",),
            side_effect="none",
            scope_kind="workspace",
        ),
        input_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/graph.schema.json"
            "#/$defs/GraphTraversalInput"
        ),
        result_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/graph.schema.json"
            "#/$defs/GraphTraversalResult"
        ),
        required_capability=CapabilityRequirement(
            id="graph.read",
            minimum_version="1.0",
            required=True,
        ),
        job=OperationJobMetadata(completion_mode="synchronous"),
        pagination=OperationPaginationMetadata(paginated=True, max_page_size=1000),
        idempotency=OperationIdempotencyMetadata(
            supports_idempotency_key=False,
            required=False,
            safe_to_retry=True,
        ),
        precondition=OperationPreconditionMetadata(
            supports_mutation_precondition=False,
            required=False,
        ),
        audit=OperationAuditMetadata(audited=True, audit_category="read"),
        allowed_errors=(
            "authentication_required",
            "authorization_denied",
            "cancelled",
            "capability_not_granted",
            "deadline_exceeded",
            "dependency_unavailable",
            "incompatible_version",
            "internal_non_recoverable",
            "internal_recoverable",
            "invalid_purpose",
            "invalid_request",
            "not_found",
            "projection_unavailable",
            "rate_limited",
            "size_limit_exceeded",
            "stale_projection",
            "upgrade_required",
            "workspace_migration_required",
            "workspace_not_granted",
        ),
    ),
    OperationMetadata(
        name="import.start",
        scope=OperationScope(
            required_scopes=("memory:write",),
            side_effect="create",
            scope_kind="workspace",
        ),
        input_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/jobs.schema.json"
            "#/$defs/ImportStartInput"
        ),
        result_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/jobs.schema.json"
            "#/$defs/ImportStartResult"
        ),
        required_capability=CapabilityRequirement(
            id="ingestion.import",
            minimum_version="1.0",
            required=True,
        ),
        job=OperationJobMetadata(
            completion_mode="always_returns_job",
            job_kind="ingestion.import",
            terminal_result_schema_ref=(
                "https://contracts.omnivia.dev/application/v1/j"
                "obs.schema.json#/$defs/ImportCompletionResult"
            ),
        ),
        pagination=OperationPaginationMetadata(paginated=False),
        idempotency=OperationIdempotencyMetadata(
            supports_idempotency_key=True,
            required=True,
            safe_to_retry=False,
        ),
        precondition=OperationPreconditionMetadata(
            supports_mutation_precondition=False,
            required=False,
        ),
        audit=OperationAuditMetadata(audited=True, audit_category="mutation"),
        allowed_errors=(
            "authentication_required",
            "authorization_denied",
            "cancelled",
            "capability_not_granted",
            "deadline_exceeded",
            "dependency_unavailable",
            "idempotency_conflict",
            "incompatible_version",
            "internal_non_recoverable",
            "internal_recoverable",
            "invalid_purpose",
            "invalid_request",
            "rate_limited",
            "size_limit_exceeded",
            "upgrade_required",
            "workspace_busy",
            "workspace_lease_unavailable",
            "workspace_migration_required",
            "workspace_not_granted",
        ),
    ),
    OperationMetadata(
        name="job.cancel",
        scope=OperationScope(
            required_scopes=("job:control",),
            side_effect="update",
            scope_kind="workspace",
        ),
        input_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/jobs.schema.json"
            "#/$defs/JobCancelInput"
        ),
        result_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/jobs.schema.json"
            "#/$defs/JobCancelResult"
        ),
        required_capability=CapabilityRequirement(
            id="job.control",
            minimum_version="1.0",
            required=True,
        ),
        job=OperationJobMetadata(completion_mode="synchronous"),
        pagination=OperationPaginationMetadata(paginated=False),
        idempotency=OperationIdempotencyMetadata(
            supports_idempotency_key=True,
            required=True,
            safe_to_retry=False,
        ),
        precondition=OperationPreconditionMetadata(
            supports_mutation_precondition=False,
            required=False,
        ),
        audit=OperationAuditMetadata(audited=True, audit_category="mutation"),
        allowed_errors=(
            "authentication_required",
            "authorization_denied",
            "cancelled",
            "capability_not_granted",
            "deadline_exceeded",
            "dependency_unavailable",
            "idempotency_conflict",
            "incompatible_version",
            "internal_non_recoverable",
            "internal_recoverable",
            "invalid_purpose",
            "invalid_request",
            "not_found",
            "rate_limited",
            "upgrade_required",
            "workspace_busy",
            "workspace_lease_unavailable",
            "workspace_migration_required",
            "workspace_not_granted",
        ),
    ),
    OperationMetadata(
        name="job.events",
        scope=OperationScope(
            required_scopes=("job:read",),
            side_effect="none",
            scope_kind="workspace",
        ),
        input_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/jobs.schema.json"
            "#/$defs/JobEventsInput"
        ),
        result_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/jobs.schema.json"
            "#/$defs/JobEventsResult"
        ),
        required_capability=CapabilityRequirement(
            id="job.read",
            minimum_version="1.0",
            required=True,
        ),
        job=OperationJobMetadata(completion_mode="synchronous"),
        pagination=OperationPaginationMetadata(paginated=True, max_page_size=1000),
        idempotency=OperationIdempotencyMetadata(
            supports_idempotency_key=False,
            required=False,
            safe_to_retry=True,
        ),
        precondition=OperationPreconditionMetadata(
            supports_mutation_precondition=False,
            required=False,
        ),
        audit=OperationAuditMetadata(audited=True, audit_category="read"),
        allowed_errors=(
            "authentication_required",
            "authorization_denied",
            "cancelled",
            "capability_not_granted",
            "deadline_exceeded",
            "dependency_unavailable",
            "incompatible_version",
            "internal_non_recoverable",
            "internal_recoverable",
            "invalid_purpose",
            "invalid_request",
            "not_found",
            "rate_limited",
            "size_limit_exceeded",
            "upgrade_required",
            "workspace_migration_required",
            "workspace_not_granted",
        ),
    ),
    OperationMetadata(
        name="job.get",
        scope=OperationScope(
            required_scopes=("job:read",),
            side_effect="none",
            scope_kind="workspace",
        ),
        input_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/jobs.schema.json"
            "#/$defs/JobGetInput"
        ),
        result_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/jobs.schema.json"
            "#/$defs/JobGetResult"
        ),
        required_capability=CapabilityRequirement(
            id="job.read",
            minimum_version="1.0",
            required=True,
        ),
        job=OperationJobMetadata(completion_mode="synchronous"),
        pagination=OperationPaginationMetadata(paginated=False),
        idempotency=OperationIdempotencyMetadata(
            supports_idempotency_key=False,
            required=False,
            safe_to_retry=True,
        ),
        precondition=OperationPreconditionMetadata(
            supports_mutation_precondition=False,
            required=False,
        ),
        audit=OperationAuditMetadata(audited=True, audit_category="read"),
        allowed_errors=(
            "authentication_required",
            "authorization_denied",
            "cancelled",
            "capability_not_granted",
            "deadline_exceeded",
            "dependency_unavailable",
            "incompatible_version",
            "internal_non_recoverable",
            "internal_recoverable",
            "invalid_purpose",
            "invalid_request",
            "not_found",
            "rate_limited",
            "upgrade_required",
            "workspace_migration_required",
            "workspace_not_granted",
        ),
    ),
    OperationMetadata(
        name="job.retry",
        scope=OperationScope(
            required_scopes=("job:control",),
            side_effect="update",
            scope_kind="workspace",
        ),
        input_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/jobs.schema.json"
            "#/$defs/JobRetryInput"
        ),
        result_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/jobs.schema.json"
            "#/$defs/JobRetryResult"
        ),
        required_capability=CapabilityRequirement(
            id="job.control",
            minimum_version="1.0",
            required=True,
        ),
        job=OperationJobMetadata(completion_mode="synchronous"),
        pagination=OperationPaginationMetadata(paginated=False),
        idempotency=OperationIdempotencyMetadata(
            supports_idempotency_key=True,
            required=True,
            safe_to_retry=False,
        ),
        precondition=OperationPreconditionMetadata(
            supports_mutation_precondition=False,
            required=False,
        ),
        audit=OperationAuditMetadata(audited=True, audit_category="mutation"),
        allowed_errors=(
            "authentication_required",
            "authorization_denied",
            "cancelled",
            "capability_not_granted",
            "deadline_exceeded",
            "dependency_unavailable",
            "idempotency_conflict",
            "incompatible_version",
            "internal_non_recoverable",
            "internal_recoverable",
            "invalid_purpose",
            "invalid_request",
            "not_found",
            "rate_limited",
            "upgrade_required",
            "workspace_busy",
            "workspace_lease_unavailable",
            "workspace_migration_required",
            "workspace_not_granted",
        ),
    ),
    OperationMetadata(
        name="knowledge.propose",
        scope=OperationScope(
            required_scopes=("memory:write",),
            side_effect="update",
            scope_kind="workspace",
        ),
        input_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/knowledge.schema.json"
            "#/$defs/KnowledgeProposeInput"
        ),
        result_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/knowledge.schema.json"
            "#/$defs/KnowledgeProposeResult"
        ),
        required_capability=CapabilityRequirement(
            id="knowledge.govern",
            minimum_version="1.0",
            required=True,
        ),
        job=OperationJobMetadata(completion_mode="synchronous"),
        pagination=OperationPaginationMetadata(paginated=False),
        idempotency=OperationIdempotencyMetadata(
            supports_idempotency_key=True,
            required=True,
            safe_to_retry=False,
        ),
        precondition=OperationPreconditionMetadata(
            supports_mutation_precondition=True,
            required=True,
        ),
        audit=OperationAuditMetadata(audited=True, audit_category="mutation"),
        allowed_errors=(
            "authentication_required",
            "authorization_denied",
            "cancelled",
            "capability_not_granted",
            "conflict",
            "deadline_exceeded",
            "dependency_unavailable",
            "idempotency_conflict",
            "incompatible_version",
            "internal_non_recoverable",
            "internal_recoverable",
            "invalid_purpose",
            "invalid_request",
            "mutation_precondition_failed",
            "not_found",
            "rate_limited",
            "upgrade_required",
            "workspace_busy",
            "workspace_lease_unavailable",
            "workspace_migration_required",
            "workspace_not_granted",
        ),
    ),
    OperationMetadata(
        name="knowledge.search",
        scope=OperationScope(
            required_scopes=("memory:read",),
            side_effect="none",
            scope_kind="workspace",
        ),
        input_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/knowledge.schema.json"
            "#/$defs/KnowledgeSearchInput"
        ),
        result_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/knowledge.schema.json"
            "#/$defs/KnowledgeSearchResult"
        ),
        required_capability=CapabilityRequirement(
            id="knowledge.read",
            minimum_version="1.0",
            required=True,
        ),
        job=OperationJobMetadata(completion_mode="synchronous"),
        pagination=OperationPaginationMetadata(paginated=True, max_page_size=1000),
        idempotency=OperationIdempotencyMetadata(
            supports_idempotency_key=False,
            required=False,
            safe_to_retry=True,
        ),
        precondition=OperationPreconditionMetadata(
            supports_mutation_precondition=False,
            required=False,
        ),
        audit=OperationAuditMetadata(audited=True, audit_category="read"),
        allowed_errors=(
            "authentication_required",
            "authorization_denied",
            "cancelled",
            "capability_not_granted",
            "deadline_exceeded",
            "dependency_unavailable",
            "incompatible_version",
            "internal_non_recoverable",
            "internal_recoverable",
            "invalid_purpose",
            "invalid_request",
            "projection_unavailable",
            "rate_limited",
            "stale_projection",
            "upgrade_required",
            "workspace_migration_required",
            "workspace_not_granted",
        ),
    ),
    OperationMetadata(
        name="memory.create",
        scope=OperationScope(
            required_scopes=("memory:write",),
            side_effect="create",
            scope_kind="workspace",
        ),
        input_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/memory.schema.json"
            "#/$defs/MemoryCreateInput"
        ),
        result_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/memory.schema.json"
            "#/$defs/MemoryCreateResult"
        ),
        required_capability=CapabilityRequirement(
            id="memory.write",
            minimum_version="1.0",
            required=True,
        ),
        job=OperationJobMetadata(completion_mode="synchronous"),
        pagination=OperationPaginationMetadata(paginated=False),
        idempotency=OperationIdempotencyMetadata(
            supports_idempotency_key=True,
            required=True,
            safe_to_retry=False,
        ),
        precondition=OperationPreconditionMetadata(
            supports_mutation_precondition=False,
            required=False,
        ),
        audit=OperationAuditMetadata(audited=True, audit_category="mutation"),
        allowed_errors=(
            "authentication_required",
            "authorization_denied",
            "cancelled",
            "capability_not_granted",
            "deadline_exceeded",
            "dependency_unavailable",
            "idempotency_conflict",
            "incompatible_version",
            "internal_non_recoverable",
            "internal_recoverable",
            "invalid_purpose",
            "invalid_request",
            "rate_limited",
            "upgrade_required",
            "workspace_busy",
            "workspace_lease_unavailable",
            "workspace_migration_required",
            "workspace_not_granted",
        ),
    ),
    OperationMetadata(
        name="memory.get",
        scope=OperationScope(
            required_scopes=("memory:read",),
            side_effect="none",
            scope_kind="workspace",
        ),
        input_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/memory.schema.json"
            "#/$defs/MemoryGetInput"
        ),
        result_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/memory.schema.json"
            "#/$defs/MemoryGetResult"
        ),
        required_capability=CapabilityRequirement(
            id="memory.read",
            minimum_version="1.0",
            required=True,
        ),
        job=OperationJobMetadata(completion_mode="synchronous"),
        pagination=OperationPaginationMetadata(paginated=False),
        idempotency=OperationIdempotencyMetadata(
            supports_idempotency_key=False,
            required=False,
            safe_to_retry=True,
        ),
        precondition=OperationPreconditionMetadata(
            supports_mutation_precondition=False,
            required=False,
        ),
        audit=OperationAuditMetadata(audited=True, audit_category="read"),
        allowed_errors=(
            "authentication_required",
            "authorization_denied",
            "cancelled",
            "capability_not_granted",
            "deadline_exceeded",
            "dependency_unavailable",
            "incompatible_version",
            "internal_non_recoverable",
            "internal_recoverable",
            "invalid_purpose",
            "invalid_request",
            "not_found",
            "rate_limited",
            "upgrade_required",
            "workspace_migration_required",
            "workspace_not_granted",
        ),
    ),
    OperationMetadata(
        name="memory.list",
        scope=OperationScope(
            required_scopes=("memory:read",),
            side_effect="none",
            scope_kind="workspace",
        ),
        input_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/memory.schema.json"
            "#/$defs/MemoryListInput"
        ),
        result_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/memory.schema.json"
            "#/$defs/MemoryListResult"
        ),
        required_capability=CapabilityRequirement(
            id="memory.read",
            minimum_version="1.0",
            required=True,
        ),
        job=OperationJobMetadata(completion_mode="synchronous"),
        pagination=OperationPaginationMetadata(paginated=True, max_page_size=1000),
        idempotency=OperationIdempotencyMetadata(
            supports_idempotency_key=False,
            required=False,
            safe_to_retry=True,
        ),
        precondition=OperationPreconditionMetadata(
            supports_mutation_precondition=False,
            required=False,
        ),
        audit=OperationAuditMetadata(audited=True, audit_category="read"),
        allowed_errors=(
            "authentication_required",
            "authorization_denied",
            "cancelled",
            "capability_not_granted",
            "deadline_exceeded",
            "dependency_unavailable",
            "incompatible_version",
            "internal_non_recoverable",
            "internal_recoverable",
            "invalid_purpose",
            "invalid_request",
            "rate_limited",
            "upgrade_required",
            "workspace_migration_required",
            "workspace_not_granted",
        ),
    ),
    OperationMetadata(
        name="memory.search",
        scope=OperationScope(
            required_scopes=("memory:read",),
            side_effect="none",
            scope_kind="workspace",
        ),
        input_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/memory.schema.json"
            "#/$defs/MemorySearchInput"
        ),
        result_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/memory.schema.json"
            "#/$defs/MemorySearchResult"
        ),
        required_capability=CapabilityRequirement(
            id="memory.read",
            minimum_version="1.0",
            required=True,
        ),
        job=OperationJobMetadata(completion_mode="synchronous"),
        pagination=OperationPaginationMetadata(paginated=True, max_page_size=1000),
        idempotency=OperationIdempotencyMetadata(
            supports_idempotency_key=False,
            required=False,
            safe_to_retry=True,
        ),
        precondition=OperationPreconditionMetadata(
            supports_mutation_precondition=False,
            required=False,
        ),
        audit=OperationAuditMetadata(audited=True, audit_category="read"),
        allowed_errors=(
            "authentication_required",
            "authorization_denied",
            "cancelled",
            "capability_not_granted",
            "deadline_exceeded",
            "dependency_unavailable",
            "incompatible_version",
            "internal_non_recoverable",
            "internal_recoverable",
            "invalid_purpose",
            "invalid_request",
            "projection_unavailable",
            "rate_limited",
            "stale_projection",
            "upgrade_required",
            "workspace_migration_required",
            "workspace_not_granted",
        ),
    ),
    OperationMetadata(
        name="record.supersede",
        scope=OperationScope(
            required_scopes=("memory:write",),
            side_effect="update",
            scope_kind="workspace",
        ),
        input_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/knowledge.schema.json"
            "#/$defs/RecordSupersedeInput"
        ),
        result_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/knowledge.schema.json"
            "#/$defs/RecordSupersedeResult"
        ),
        required_capability=CapabilityRequirement(
            id="knowledge.govern",
            minimum_version="1.0",
            required=True,
        ),
        job=OperationJobMetadata(completion_mode="synchronous"),
        pagination=OperationPaginationMetadata(paginated=False),
        idempotency=OperationIdempotencyMetadata(
            supports_idempotency_key=True,
            required=True,
            safe_to_retry=False,
        ),
        precondition=OperationPreconditionMetadata(
            supports_mutation_precondition=True,
            required=True,
        ),
        audit=OperationAuditMetadata(audited=True, audit_category="mutation"),
        allowed_errors=(
            "authentication_required",
            "authorization_denied",
            "cancelled",
            "capability_not_granted",
            "conflict",
            "deadline_exceeded",
            "dependency_unavailable",
            "idempotency_conflict",
            "incompatible_version",
            "internal_non_recoverable",
            "internal_recoverable",
            "invalid_purpose",
            "invalid_request",
            "mutation_precondition_failed",
            "not_found",
            "rate_limited",
            "upgrade_required",
            "workspace_busy",
            "workspace_lease_unavailable",
            "workspace_migration_required",
            "workspace_not_granted",
        ),
    ),
    OperationMetadata(
        name="workspace.create",
        scope=OperationScope(
            required_scopes=("workspace:write",),
            side_effect="create",
            scope_kind="installation",
        ),
        input_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/workspace.schema.json"
            "#/$defs/WorkspaceCreateInput"
        ),
        result_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/workspace.schema.json"
            "#/$defs/WorkspaceCreateResult"
        ),
        required_capability=CapabilityRequirement(
            id="workspace.write",
            minimum_version="1.0",
            required=True,
        ),
        job=OperationJobMetadata(completion_mode="synchronous"),
        pagination=OperationPaginationMetadata(paginated=False),
        idempotency=OperationIdempotencyMetadata(
            supports_idempotency_key=True,
            required=True,
            safe_to_retry=False,
        ),
        precondition=OperationPreconditionMetadata(
            supports_mutation_precondition=False,
            required=False,
        ),
        audit=OperationAuditMetadata(audited=True, audit_category="mutation"),
        allowed_errors=(
            "authentication_required",
            "authorization_denied",
            "bootstrap_in_progress",
            "cancelled",
            "capability_not_granted",
            "conflict",
            "deadline_exceeded",
            "dependency_unavailable",
            "idempotency_conflict",
            "incompatible_version",
            "internal_non_recoverable",
            "internal_recoverable",
            "invalid_purpose",
            "invalid_request",
            "rate_limited",
            "upgrade_required",
        ),
    ),
    OperationMetadata(
        name="workspace.inspect",
        scope=OperationScope(
            required_scopes=("workspace:read",),
            side_effect="none",
            scope_kind="workspace",
        ),
        input_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/workspace.schema.json"
            "#/$defs/WorkspaceInspectInput"
        ),
        result_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/workspace.schema.json"
            "#/$defs/WorkspaceInspectResult"
        ),
        required_capability=CapabilityRequirement(
            id="workspace.read",
            minimum_version="1.0",
            required=True,
        ),
        job=OperationJobMetadata(completion_mode="synchronous"),
        pagination=OperationPaginationMetadata(paginated=False),
        idempotency=OperationIdempotencyMetadata(
            supports_idempotency_key=False,
            required=False,
            safe_to_retry=True,
        ),
        precondition=OperationPreconditionMetadata(
            supports_mutation_precondition=False,
            required=False,
        ),
        audit=OperationAuditMetadata(audited=True, audit_category="read"),
        allowed_errors=(
            "authentication_required",
            "authorization_denied",
            "cancelled",
            "capability_not_granted",
            "deadline_exceeded",
            "dependency_unavailable",
            "incompatible_version",
            "internal_non_recoverable",
            "internal_recoverable",
            "invalid_purpose",
            "invalid_request",
            "not_found",
            "rate_limited",
            "upgrade_required",
            "workspace_migration_required",
            "workspace_not_granted",
        ),
    ),
    OperationMetadata(
        name="workspace.list",
        scope=OperationScope(
            required_scopes=("workspace:read",),
            side_effect="none",
            scope_kind="installation",
        ),
        input_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/workspace.schema.json"
            "#/$defs/WorkspaceListInput"
        ),
        result_schema_ref=(
            "https://contracts.omnivia.dev/application/v1/workspace.schema.json"
            "#/$defs/WorkspaceListResult"
        ),
        required_capability=CapabilityRequirement(
            id="workspace.read",
            minimum_version="1.0",
            required=True,
        ),
        job=OperationJobMetadata(completion_mode="synchronous"),
        pagination=OperationPaginationMetadata(paginated=True, max_page_size=1000),
        idempotency=OperationIdempotencyMetadata(
            supports_idempotency_key=False,
            required=False,
            safe_to_retry=True,
        ),
        precondition=OperationPreconditionMetadata(
            supports_mutation_precondition=False,
            required=False,
        ),
        audit=OperationAuditMetadata(audited=True, audit_category="read"),
        allowed_errors=(
            "authentication_required",
            "authorization_denied",
            "bootstrap_in_progress",
            "cancelled",
            "capability_not_granted",
            "deadline_exceeded",
            "dependency_unavailable",
            "incompatible_version",
            "internal_non_recoverable",
            "internal_recoverable",
            "invalid_purpose",
            "invalid_request",
            "rate_limited",
            "upgrade_required",
        ),
    ),
)
"""The canonical v1 application operation catalogue, in the canonical order. Generated from
`x-omnivia-operation-catalogue`, so this is contract metadata a caller can read, not a dispatch
table: nothing here routes, authorizes, or executes anything.
"""
