# GENERATED FILE - DO NOT EDIT.
#
# Source of truth:
#   contracts/application/v1/schemas/common.schema.json
#   contracts/application/v1/schemas/compatibility.schema.json
#   contracts/application/v1/schemas/errors.schema.json
#   contracts/application/v1/schemas/envelopes.schema.json
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
    "FROZEN_ERROR_CODES",
    "FROZEN_RETRY_CLASSES",
    "IDEMPOTENCY_KEY_PATTERN",
    "IDENTIFIER_PATTERN",
    "OPAQUE_TOKEN_PATTERN",
    "OPEN_CODE_PATTERN",
    "OPERATION_NAME_PATTERN",
    "PROJECTION_VERSION_PATTERN",
    "PURPOSE_PATTERN",
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
    "CapabilityId",
    "CapabilityRef",
    "CapabilityRequirement",
    "CapabilitySet",
    "ClientIdentity",
    "CompatibilityMetadata",
    "ContractDecodeError",
    "ContractVersion",
    "CorrelationId",
    "Deprecation",
    "DurationMs",
    "ErrorCode",
    "ErrorResponseEnvelope",
    "GrantedAuthority",
    "IdempotencyKey",
    "Identifier",
    "JobReference",
    "JsonObject",
    "MutationPrecondition",
    "Omission",
    "OpaqueToken",
    "OpenCode",
    "OperationName",
    "PageMetadata",
    "PartialResult",
    "PrincipalClaim",
    "ProjectionFreshness",
    "ProjectionVersion",
    "Purpose",
    "ReleaseVersion",
    "RequestEnvelope",
    "RequestId",
    "RequestMetadata",
    "ResponseEnvelope",
    "ResponseMetadata",
    "RetryClass",
    "Scope",
    "SuccessResponseEnvelope",
    "Timestamp",
    "TraceId",
    "UpgradeState",
    "VersionCapabilityEnvelope",
    "VersionWindow",
    "Warning",
    "WorkspaceId",
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

CONTRACT_VERSION: Final = "1.0"
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
OPERATION_NAME_PATTERN: Final = '^[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)+$'
ERROR_CODE_PATTERN: Final = '^[a-z][a-z0-9_]*$'
RETRY_CLASS_PATTERN: Final = '^[a-z][a-z0-9_]*$'


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
