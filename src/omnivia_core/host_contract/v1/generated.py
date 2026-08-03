# GENERATED FILE - DO NOT EDIT.
#
# Source of truth:
#   contracts/host/v1/schemas/host-contract-v1.schema.json
# Governed by:
#   DOC-004 section AA; approval GOV-HOST-CONTRACT-APPROVAL-001;
#   proposal commit 2568f0049256b86fa58dc2387e8302aa4da94ae1;
#   proposal content-set SHA-256 31da9251944079ea1bc8db61ed0a393a05e0ce18602f31f6771def4ab243f0ac.
# Generator:
#   scripts/generate-host-contract.py
#
# Regenerate: python scripts/generate-host-contract.py
# Verify:     python scripts/generate-host-contract.py --check
#
# Frozen dataclasses, type aliases, and closed vocabulary for the OmniVia Host
# Contract v1. Standard library only: this module must never depend on runtime,
# storage, HTTP, MCP, CLI, Platform, Dev, or a validation framework.

"""Generated Host Contract v1 types (DOC-004 section AA).

Every record enforces the canonical schema, not a structural subset of it.
``validate()`` re-implements the governed patterns, string lengths, integer
bounds, RFC 3339 timestamps, array cardinality and uniqueness, closed
vocabularies, closed field sets and record-level branch invariants;
``from_wire`` decodes and then validates, and ``to_wire`` validates before it
emits. There is therefore no public way into or out of a record that skips the
governed constraints, and a directly constructed record with hostile values is
refused at the moment it would become a wire document -- a type annotation is
documentation, not a runtime guarantee.

No diagnostic raised here reproduces a rejected value. A refused document may
be carrying exactly the credential, token or private key the contract forbids,
so a message names the field path and the rule it broke and nothing else.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, ClassVar, Final, TypeAlias, cast

__all__ = [
    "AVAILABILITY_STATES",
    "AVAILABILITY_STATE_AVAILABLE",
    "AVAILABILITY_STATE_AVAILABLE_AFTER_CONFIGURATION",
    "AVAILABILITY_STATE_AVAILABLE_THROUGH_COMPANION",
    "AVAILABILITY_STATE_ENTITLEMENT_UNAVAILABLE",
    "AVAILABILITY_STATE_PERMISSION_DENIED",
    "AVAILABILITY_STATE_TEMPORARILY_UNHEALTHY",
    "AVAILABILITY_STATE_UNAVAILABLE_ON_TARGET",
    "COMPATIBILITY_STATUSES",
    "COMPATIBILITY_STATUS_BLOCKED_BY_POLICY",
    "COMPATIBILITY_STATUS_COMPATIBLE",
    "COMPATIBILITY_STATUS_COMPATIBLE_AFTER_CONFIGURATION",
    "COMPATIBILITY_STATUS_COMPATIBLE_WITH_DEGRADATION",
    "COMPATIBILITY_STATUS_INCOMPATIBLE",
    "COMPATIBILITY_STATUS_REQUIRES_COMPANION",
    "CONTRACT_VERSION_PATTERN",
    "CONTRIBUTION_KINDS",
    "CONTRIBUTION_KIND_COMMAND",
    "CONTRIBUTION_KIND_CONTEXT_ITEM_TYPE",
    "CONTRIBUTION_KIND_DIAGNOSTIC",
    "CONTRIBUTION_KIND_ENTITLEMENT",
    "CONTRIBUTION_KIND_NAVIGATION",
    "CONTRIBUTION_KIND_ROUTE",
    "CONTRIBUTION_KIND_RUNTIME_ADAPTER",
    "CONTRIBUTION_KIND_SETTINGS_SECTION",
    "CONTRIBUTION_KIND_WORKFLOW_COMPONENT",
    "DENIAL_CATEGORIES",
    "DENIAL_CATEGORY_APPROVAL_REQUIRED",
    "DENIAL_CATEGORY_AUTHENTICATION_REQUIRED",
    "DENIAL_CATEGORY_BLOCKED_BY_POLICY",
    "DENIAL_CATEGORY_COMPANION_REQUIRED",
    "DENIAL_CATEGORY_CONFIGURATION_REQUIRED",
    "DENIAL_CATEGORY_ENTITLEMENT_REQUIRED",
    "DENIAL_CATEGORY_MEMBERSHIP_REQUIRED",
    "DENIAL_CATEGORY_PERMISSION_DENIED",
    "DENIAL_CATEGORY_TEMPORARILY_UNHEALTHY",
    "DENIAL_CATEGORY_UNAVAILABLE_ON_TARGET",
    "DEVELOPMENT_TARGETS",
    "DEVELOPMENT_TARGET_CONFORMANCE",
    "DEVELOPMENT_TARGET_DESKTOP",
    "HOST_CONTRACT_APPROVAL_ID",
    "HOST_CONTRACT_CONTENT_SET_SHA256",
    "HOST_CONTRACT_MAJOR",
    "HOST_CONTRACT_PROPOSAL_COMMIT",
    "HOST_CONTRACT_RECORDS",
    "HOST_CONTRACT_SCHEMA_ID",
    "HOST_CONTRACT_VERSION",
    "HOST_NAMESPACES",
    "HOST_NAMESPACE_AGENTS",
    "HOST_NAMESPACE_APPROVALS",
    "HOST_NAMESPACE_AUDIT",
    "HOST_NAMESPACE_CREDENTIALS",
    "HOST_NAMESPACE_DATA",
    "HOST_NAMESPACE_DIAGNOSTICS",
    "HOST_NAMESPACE_ENTITLEMENTS",
    "HOST_NAMESPACE_EVIDENCE",
    "HOST_NAMESPACE_FILES",
    "HOST_NAMESPACE_IDENTITY",
    "HOST_NAMESPACE_INTEGRATIONS",
    "HOST_NAMESPACE_KNOWLEDGE",
    "HOST_NAMESPACE_NAVIGATION",
    "HOST_NAMESPACE_NOTIFICATIONS",
    "HOST_NAMESPACE_ORGANISATION",
    "HOST_NAMESPACE_WORKFLOWS",
    "HOST_NAMESPACE_WORKSPACE",
    "IDENTIFIER_PATTERN",
    "OUTCOMES",
    "OUTCOME_DENIED",
    "OUTCOME_FAILED",
    "OUTCOME_SUCCESS",
    "OUTCOME_UNAVAILABLE",
    "PROMOTION_RESULTS",
    "PROMOTION_RESULT_PROMOTED",
    "PROMOTION_RESULT_REJECTED",
    "REMEDIATION_KINDS",
    "REMEDIATION_KIND_CONFIGURE",
    "REMEDIATION_KIND_CONTACT_ADMINISTRATOR",
    "REMEDIATION_KIND_INSTALL_COMPANION",
    "REMEDIATION_KIND_MANAGE_ENTITLEMENT",
    "REMEDIATION_KIND_NONE",
    "REMEDIATION_KIND_REQUEST_APPROVAL",
    "REMEDIATION_KIND_REQUEST_MEMBERSHIP",
    "REMEDIATION_KIND_REQUEST_PERMISSION",
    "REMEDIATION_KIND_RETRY_LATER",
    "REMEDIATION_KIND_SIGN_IN",
    "SEM_VER_PATTERN",
    "SHA256_PATTERN",
    "TARGETS",
    "TARGET_CLOUD",
    "TARGET_CONFORMANCE",
    "TARGET_DESKTOP",
    "TEST_DRIVER_OPERATIONS",
    "TEST_DRIVER_OPERATION_CAPTURE_SCREENSHOT",
    "TEST_DRIVER_OPERATION_CAPTURE_TRACE",
    "TEST_DRIVER_OPERATION_INSPECT_STATE",
    "TEST_DRIVER_OPERATION_INVOKE_COMMAND",
    "TEST_DRIVER_OPERATION_NAVIGATE",
    "TEST_DRIVER_OPERATION_RUN_ACCESSIBILITY_CHECK",
    "TEST_DRIVER_OUTCOMES",
    "TEST_DRIVER_OUTCOME_FAILED",
    "TEST_DRIVER_OUTCOME_PASSED",
    "TEST_DRIVER_OUTCOME_UNAVAILABLE",
    "CapabilityAvailability",
    "CapabilityDenial",
    "CompatibilityResult",
    "ContractVersion",
    "Contribution",
    "DevelopmentProfile",
    "EnvironmentBinding",
    "HostContractDecodeError",
    "HostFailure",
    "HostNamespace",
    "HostRequest",
    "HostResult",
    "Identifier",
    "JsonObject",
    "LogicalReferences",
    "ModuleContributionDeclaration",
    "PromotionRecord",
    "ReleasePackageIdentity",
    "Remediation",
    "RollbackRecord",
    "ScenarioAssertion",
    "SemVer",
    "Sha256",
    "ShellContext",
    "ShellScenario",
    "SourceMount",
    "Target",
    "TargetEntry",
    "TestDriverRequest",
    "TestDriverResult",
]


class HostContractDecodeError(Exception):
    """Raised when a payload is not a valid Host Contract v1 value.

    Carries the payload's ``correlationId`` when the document had a usable one,
    so a caller can display a generic safe denial keyed by the correlation ID
    without reading anything else out of the rejected document.
    """

    def __init__(self, message: str, correlation_id: str | None = None) -> None:
        super().__init__(message)
        self.correlation_id = correlation_id


def _require_mapping(payload: object, path: str) -> Mapping[str, Any]:
    if type(payload) not in (dict, MappingProxyType):
        raise HostContractDecodeError(f"{path}: expected an object")
    mapping = cast(Mapping[str, Any], payload)
    for key in mapping:
        if type(key) is not str:
            raise HostContractDecodeError(f"{path}: object keys must be strings")
    return mapping


def _require_field(mapping: Mapping[str, Any], name: str, path: str) -> Any:
    if name not in mapping:
        raise HostContractDecodeError(f"{path}: missing required field {name!r}")
    return mapping[name]


#: A field name safe to name back to the caller. A rejected document controls
#: its own key spellings, so a key that is not a plain identifier is reported
#: as a placeholder rather than echoed.
_SAFE_FIELD_NAME_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


def _safe_field_name(name: str) -> str:
    return name if _SAFE_FIELD_NAME_RE.fullmatch(name) else "<unprintable field name>"


def _reject_unknown_fields(mapping: Mapping[str, Any], known: frozenset[str], path: str) -> None:
    """Reject any field the closed record does not declare.

    This is the structural half of the governed redaction and secret-exclusion
    invariants: a closed record cannot carry an inline secret, an environment
    value or an unapproved policy detail, because it cannot carry any field the
    approved schema does not name.
    """
    unknown = sorted(_safe_field_name(name) for name in set(mapping) - known)
    if unknown:
        raise HostContractDecodeError(f"{path}: unknown field(s) {unknown} in a closed record")


def _decode_str(value: object, path: str) -> str:
    if type(value) is not str:
        raise HostContractDecodeError(f"{path}: expected a string")
    return value


def _decode_enum(value: object, allowed: tuple[str, ...], path: str) -> str:
    """Decode a closed vocabulary member, failing closed on anything else.

    An unknown value is never downgraded to a usable one and is never treated
    as available: it raises, and the refused value is not echoed back.
    """
    text = _decode_str(value, path)
    if text not in allowed:
        raise HostContractDecodeError(f"{path}: unsupported_contract_value")
    return text


def _decode_int(value: object, path: str) -> int:
    if type(value) is not int:
        raise HostContractDecodeError(f"{path}: expected an integer")
    return value


def _decode_bool(value: object, path: str) -> bool:
    if type(value) is not bool:
        raise HostContractDecodeError(f"{path}: expected a boolean")
    return value


def _decode_sequence(value: object, path: str) -> Sequence[Any]:
    if type(value) not in (list, tuple):
        raise HostContractDecodeError(f"{path}: expected an array")
    return cast(Sequence[Any], value)


def _unique_key(value: Any) -> Any:
    """Return a hashable key that compares two JSON values structurally.

    ``uniqueItems`` compares whole JSON values, so two equal objects repeat
    even when they are distinct Python objects. Booleans are keyed apart from
    the integers they equal, because JSON `true` is not JSON `1`.
    """
    if type(value) in (dict, MappingProxyType):
        return ("object", tuple(sorted((key, _unique_key(item)) for key, item in value.items())))
    if type(value) in (str, bytes, bytearray):
        return ("scalar", value)
    if type(value) is bool:
        return ("bool", value)
    if type(value) in (list, tuple):
        return ("array", tuple(_unique_key(item) for item in value))
    return ("scalar", value)


def _require_unique(values: Sequence[Any], path: str) -> None:
    """Raise on a repeated item, comparing JSON values structurally.

    The diagnostic names the index and nothing else: a repeated value can be
    exactly the credential-shaped string the contract refuses to reproduce.
    """
    seen: set[Any] = set()
    for index, value in enumerate(values):
        try:
            key = _unique_key(value)
            repeated = key in seen
        except TypeError as error:
            raise HostContractDecodeError(f"{path}[{index}]: value is not JSON") from error
        if repeated:
            raise HostContractDecodeError(f"{path}[{index}]: duplicate value")
        seen.add(key)


def _freeze_json(value: object, path: str) -> Any:
    """Return a deeply immutable view of an opaque JSON value."""
    if type(value) in (dict, MappingProxyType):
        mapping = cast(Mapping[str, Any], value)
        for key in mapping:
            if type(key) is not str:
                raise HostContractDecodeError(f"{path}: object keys must be strings")
        return MappingProxyType(
            {key: _freeze_json(item, f"{path}.{key}") for key, item in mapping.items()}
        )
    if type(value) in (str, bool, int) or value is None:
        return value
    if type(value) is float:
        return value
    if type(value) in (list, tuple):
        return tuple(
            _freeze_json(item, f"{path}[]") for item in cast(Sequence[Any], value)
        )
    raise HostContractDecodeError(f"{path}: value is not JSON")


def _decode_json_object(value: object, path: str) -> JsonObject:
    mapping = _require_mapping(value, path)
    frozen: JsonObject = _freeze_json(mapping, path)
    return frozen


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _encode_json_object(value: JsonObject) -> dict[str, Any]:
    thawed: Any = _thaw_json(value)
    return dict(thawed)


#: An opaque JSON object whose shape a namespace schema owns, not this contract.
JsonObject: TypeAlias = Mapping[str, Any]


#: RFC 3339 `date-time`. A time zone is mandatory: an authority record whose
#: validity window depends on the reader's clock is not a fixed instant. The
#: leap second RFC 3339 permits is accepted and validated against `:59`.
_DATE_TIME_RE: Final[re.Pattern[str]] = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):([0-5]\d|60)(?:\.\d+)?"
    r"(?:[Zz]|[+-](?:[01]\d|2[0-3]):[0-5]\d)"
)


def _check_date_time(value: str, path: str) -> None:
    match = _DATE_TIME_RE.fullmatch(value)
    if match is None:
        raise HostContractDecodeError(f"{path}: expected an RFC 3339 date-time with a time zone")
    year, month, day, hour, minute, second = (int(part) for part in match.groups())
    try:
        datetime(year, month, day, hour, minute, min(second, 59), tzinfo=UTC)
    except ValueError as error:
        raise HostContractDecodeError(f"{path}: is not a real calendar instant") from error


def _check_str(
    value: object,
    path: str,
    *,
    pattern: str | None = None,
    min_length: int | None = None,
    max_length: int | None = None,
    date_time: bool = False,
) -> None:
    """Raise unless the value is a string meeting every governed constraint."""
    if type(value) is not str:
        raise HostContractDecodeError(f"{path}: expected a string")
    if min_length is not None and len(value) < min_length:
        raise HostContractDecodeError(f"{path}: expected at least {min_length} character(s)")
    if max_length is not None and len(value) > max_length:
        raise HostContractDecodeError(f"{path}: expected at most {max_length} character(s)")
    if pattern is not None and re.fullmatch(pattern, value) is None:
        raise HostContractDecodeError(f"{path}: does not match the governed pattern")
    if date_time:
        _check_date_time(value, path)


def _check_enum(value: object, allowed: tuple[str, ...], path: str) -> None:
    if type(value) is not str or value not in allowed:
        raise HostContractDecodeError(f"{path}: unsupported_contract_value")


def _check_int(
    value: object, path: str, *, minimum: int | None = None, maximum: int | None = None
) -> None:
    if type(value) is not int:
        raise HostContractDecodeError(f"{path}: expected an integer")
    if minimum is not None and value < minimum:
        raise HostContractDecodeError(f"{path}: expected an integer of at least {minimum}")
    if maximum is not None and value > maximum:
        raise HostContractDecodeError(f"{path}: expected an integer of at most {maximum}")


def _check_bool(value: object, path: str) -> None:
    if type(value) is not bool:
        raise HostContractDecodeError(f"{path}: expected a boolean")


def _check_json_value(value: object, path: str) -> None:
    """Raise unless the value is representable as JSON, all the way down."""
    if type(value) in (dict, MappingProxyType):
        for key, item in cast(Mapping[str, Any], value).items():
            if type(key) is not str:
                raise HostContractDecodeError(f"{path}: object keys must be strings")
            _check_json_value(item, f"{path}.{_safe_field_name(key)}")
        return
    if value is None or type(value) in (bool, str):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise HostContractDecodeError(f"{path}: expected a finite JSON number")
        return
    if type(value) is int:
        return
    if type(value) in (bytes, bytearray):
        raise HostContractDecodeError(f"{path}: value is not JSON")
    if type(value) in (list, tuple):
        for index, item in enumerate(cast(Sequence[Any], value)):
            _check_json_value(item, f"{path}[{index}]")
        return
    raise HostContractDecodeError(f"{path}: value is not JSON")


def _check_json_object(value: object, path: str) -> None:
    if type(value) not in (dict, MappingProxyType):
        raise HostContractDecodeError(f"{path}: expected an object")
    _check_json_value(value, path)


def _check_record(value: object, expected: type[Any], path: str) -> None:
    """Raise unless the value is that record *and* is itself valid."""
    if type(value) is not expected:
        raise HostContractDecodeError(f"{path}: expected a {expected.__name__}")
    cast(Any, value).validate(path)


def _check_sequence(
    value: object, path: str, *, min_items: int | None = None, unique: bool = False
) -> Sequence[Any]:
    if type(value) not in (list, tuple):
        raise HostContractDecodeError(f"{path}: expected an array")
    sequence = cast(Sequence[Any], value)
    if min_items is not None and len(sequence) < min_items:
        raise HostContractDecodeError(f"{path}: expected at least {min_items} item(s)")
    if unique:
        _require_unique(sequence, path)
    return sequence


def _check_branches(
    discriminator: object,
    branches: Mapping[str, Any],
    rules: Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]],
    path: str,
) -> None:
    """Raise unless the present optional branches match the discriminator's rule.

    Transcribed from the canonical schema's `allOf`, so the envelope is total
    by construction: there is no discriminator value without a rule, and no
    rule that tolerates a branch belonging to a different one.
    """
    if not isinstance(discriminator, str) or discriminator not in rules:
        raise HostContractDecodeError(f"{path}: unsupported_contract_value")
    required, forbidden = rules[discriminator]
    present = tuple(name for name, value in branches.items() if value is not None)
    if [name for name in required if name not in present] or [
        name for name in forbidden if name in present
    ]:
        raise HostContractDecodeError(
            f"{path}: this outcome requires exactly one branch ({list(required)}); "
            f"got {list(present)}"
        )

#: The governed Host Contract version and the single major this build speaks.
HOST_CONTRACT_VERSION: Final[str] = "1.0.0"
HOST_CONTRACT_MAJOR: Final[int] = 1
HOST_CONTRACT_SCHEMA_ID: Final[str] = "urn:omnivia:host-contract:1.0.0"

#: The exact-byte approval DOC-004 section AA incorporates.
HOST_CONTRACT_APPROVAL_ID: Final[str] = "GOV-HOST-CONTRACT-APPROVAL-001"
HOST_CONTRACT_PROPOSAL_COMMIT: Final[str] = "2568f0049256b86fa58dc2387e8302aa4da94ae1"
HOST_CONTRACT_CONTENT_SET_SHA256: Final[str] = (
    "31da9251944079ea1bc8db61ed0a393a05e0ce18602f31f6771def4ab243f0ac"
)
#: Closed v1 vocabulary for `availability.state`. An unknown value fails closed.
AVAILABILITY_STATE_AVAILABLE: Final[str] = "available"
AVAILABILITY_STATE_UNAVAILABLE_ON_TARGET: Final[str] = "unavailable_on_target"
AVAILABILITY_STATE_AVAILABLE_AFTER_CONFIGURATION: Final[str] = "available_after_configuration"
AVAILABILITY_STATE_AVAILABLE_THROUGH_COMPANION: Final[str] = "available_through_companion"
AVAILABILITY_STATE_PERMISSION_DENIED: Final[str] = "permission_denied"
AVAILABILITY_STATE_ENTITLEMENT_UNAVAILABLE: Final[str] = "entitlement_unavailable"
AVAILABILITY_STATE_TEMPORARILY_UNHEALTHY: Final[str] = "temporarily_unhealthy"
AVAILABILITY_STATES: Final[tuple[str, ...]] = (
    AVAILABILITY_STATE_AVAILABLE,
    AVAILABILITY_STATE_UNAVAILABLE_ON_TARGET,
    AVAILABILITY_STATE_AVAILABLE_AFTER_CONFIGURATION,
    AVAILABILITY_STATE_AVAILABLE_THROUGH_COMPANION,
    AVAILABILITY_STATE_PERMISSION_DENIED,
    AVAILABILITY_STATE_ENTITLEMENT_UNAVAILABLE,
    AVAILABILITY_STATE_TEMPORARILY_UNHEALTHY,
)

#: Closed v1 vocabulary for `compatibilityResult.status`. An unknown value fails closed.
COMPATIBILITY_STATUS_COMPATIBLE: Final[str] = "compatible"
COMPATIBILITY_STATUS_COMPATIBLE_WITH_DEGRADATION: Final[str] = "compatible_with_degradation"
COMPATIBILITY_STATUS_COMPATIBLE_AFTER_CONFIGURATION: Final[str] = "compatible_after_configuration"
COMPATIBILITY_STATUS_REQUIRES_COMPANION: Final[str] = "requires_companion"
COMPATIBILITY_STATUS_INCOMPATIBLE: Final[str] = "incompatible"
COMPATIBILITY_STATUS_BLOCKED_BY_POLICY: Final[str] = "blocked_by_policy"
COMPATIBILITY_STATUSES: Final[tuple[str, ...]] = (
    COMPATIBILITY_STATUS_COMPATIBLE,
    COMPATIBILITY_STATUS_COMPATIBLE_WITH_DEGRADATION,
    COMPATIBILITY_STATUS_COMPATIBLE_AFTER_CONFIGURATION,
    COMPATIBILITY_STATUS_REQUIRES_COMPANION,
    COMPATIBILITY_STATUS_INCOMPATIBLE,
    COMPATIBILITY_STATUS_BLOCKED_BY_POLICY,
)

#: Closed v1 vocabulary for `moduleContributionDeclaration.contributions[].kind`. An unknown value fails closed.
CONTRIBUTION_KIND_ROUTE: Final[str] = "route"
CONTRIBUTION_KIND_NAVIGATION: Final[str] = "navigation"
CONTRIBUTION_KIND_COMMAND: Final[str] = "command"
CONTRIBUTION_KIND_SETTINGS_SECTION: Final[str] = "settings_section"
CONTRIBUTION_KIND_CONTEXT_ITEM_TYPE: Final[str] = "context_item_type"
CONTRIBUTION_KIND_WORKFLOW_COMPONENT: Final[str] = "workflow_component"
CONTRIBUTION_KIND_RUNTIME_ADAPTER: Final[str] = "runtime_adapter"
CONTRIBUTION_KIND_DIAGNOSTIC: Final[str] = "diagnostic"
CONTRIBUTION_KIND_ENTITLEMENT: Final[str] = "entitlement"
CONTRIBUTION_KINDS: Final[tuple[str, ...]] = (
    CONTRIBUTION_KIND_ROUTE,
    CONTRIBUTION_KIND_NAVIGATION,
    CONTRIBUTION_KIND_COMMAND,
    CONTRIBUTION_KIND_SETTINGS_SECTION,
    CONTRIBUTION_KIND_CONTEXT_ITEM_TYPE,
    CONTRIBUTION_KIND_WORKFLOW_COMPONENT,
    CONTRIBUTION_KIND_RUNTIME_ADAPTER,
    CONTRIBUTION_KIND_DIAGNOSTIC,
    CONTRIBUTION_KIND_ENTITLEMENT,
)

#: Closed v1 vocabulary for `denial.category`. An unknown value fails closed.
DENIAL_CATEGORY_AUTHENTICATION_REQUIRED: Final[str] = "authentication_required"
DENIAL_CATEGORY_MEMBERSHIP_REQUIRED: Final[str] = "membership_required"
DENIAL_CATEGORY_PERMISSION_DENIED: Final[str] = "permission_denied"
DENIAL_CATEGORY_APPROVAL_REQUIRED: Final[str] = "approval_required"
DENIAL_CATEGORY_ENTITLEMENT_REQUIRED: Final[str] = "entitlement_required"
DENIAL_CATEGORY_UNAVAILABLE_ON_TARGET: Final[str] = "unavailable_on_target"
DENIAL_CATEGORY_CONFIGURATION_REQUIRED: Final[str] = "configuration_required"
DENIAL_CATEGORY_COMPANION_REQUIRED: Final[str] = "companion_required"
DENIAL_CATEGORY_BLOCKED_BY_POLICY: Final[str] = "blocked_by_policy"
DENIAL_CATEGORY_TEMPORARILY_UNHEALTHY: Final[str] = "temporarily_unhealthy"
DENIAL_CATEGORIES: Final[tuple[str, ...]] = (
    DENIAL_CATEGORY_AUTHENTICATION_REQUIRED,
    DENIAL_CATEGORY_MEMBERSHIP_REQUIRED,
    DENIAL_CATEGORY_PERMISSION_DENIED,
    DENIAL_CATEGORY_APPROVAL_REQUIRED,
    DENIAL_CATEGORY_ENTITLEMENT_REQUIRED,
    DENIAL_CATEGORY_UNAVAILABLE_ON_TARGET,
    DENIAL_CATEGORY_CONFIGURATION_REQUIRED,
    DENIAL_CATEGORY_COMPANION_REQUIRED,
    DENIAL_CATEGORY_BLOCKED_BY_POLICY,
    DENIAL_CATEGORY_TEMPORARILY_UNHEALTHY,
)

#: Closed v1 vocabulary for `developmentProfile.target`. An unknown value fails closed.
DEVELOPMENT_TARGET_DESKTOP: Final[str] = "desktop"
DEVELOPMENT_TARGET_CONFORMANCE: Final[str] = "conformance"
DEVELOPMENT_TARGETS: Final[tuple[str, ...]] = (
    DEVELOPMENT_TARGET_DESKTOP,
    DEVELOPMENT_TARGET_CONFORMANCE,
)

#: The fixed v1 `namespace` vocabulary. An unknown value fails closed.
HOST_NAMESPACE_IDENTITY: Final[str] = "identity"
HOST_NAMESPACE_ORGANISATION: Final[str] = "organisation"
HOST_NAMESPACE_WORKSPACE: Final[str] = "workspace"
HOST_NAMESPACE_DATA: Final[str] = "data"
HOST_NAMESPACE_KNOWLEDGE: Final[str] = "knowledge"
HOST_NAMESPACE_FILES: Final[str] = "files"
HOST_NAMESPACE_WORKFLOWS: Final[str] = "workflows"
HOST_NAMESPACE_AGENTS: Final[str] = "agents"
HOST_NAMESPACE_INTEGRATIONS: Final[str] = "integrations"
HOST_NAMESPACE_CREDENTIALS: Final[str] = "credentials"
HOST_NAMESPACE_NOTIFICATIONS: Final[str] = "notifications"
HOST_NAMESPACE_APPROVALS: Final[str] = "approvals"
HOST_NAMESPACE_AUDIT: Final[str] = "audit"
HOST_NAMESPACE_EVIDENCE: Final[str] = "evidence"
HOST_NAMESPACE_ENTITLEMENTS: Final[str] = "entitlements"
HOST_NAMESPACE_NAVIGATION: Final[str] = "navigation"
HOST_NAMESPACE_DIAGNOSTICS: Final[str] = "diagnostics"
HOST_NAMESPACES: Final[tuple[str, ...]] = (
    HOST_NAMESPACE_IDENTITY,
    HOST_NAMESPACE_ORGANISATION,
    HOST_NAMESPACE_WORKSPACE,
    HOST_NAMESPACE_DATA,
    HOST_NAMESPACE_KNOWLEDGE,
    HOST_NAMESPACE_FILES,
    HOST_NAMESPACE_WORKFLOWS,
    HOST_NAMESPACE_AGENTS,
    HOST_NAMESPACE_INTEGRATIONS,
    HOST_NAMESPACE_CREDENTIALS,
    HOST_NAMESPACE_NOTIFICATIONS,
    HOST_NAMESPACE_APPROVALS,
    HOST_NAMESPACE_AUDIT,
    HOST_NAMESPACE_EVIDENCE,
    HOST_NAMESPACE_ENTITLEMENTS,
    HOST_NAMESPACE_NAVIGATION,
    HOST_NAMESPACE_DIAGNOSTICS,
)

#: Closed v1 vocabulary for `result.outcome`. An unknown value fails closed.
OUTCOME_SUCCESS: Final[str] = "success"
OUTCOME_DENIED: Final[str] = "denied"
OUTCOME_UNAVAILABLE: Final[str] = "unavailable"
OUTCOME_FAILED: Final[str] = "failed"
OUTCOMES: Final[tuple[str, ...]] = (
    OUTCOME_SUCCESS,
    OUTCOME_DENIED,
    OUTCOME_UNAVAILABLE,
    OUTCOME_FAILED,
)

#: Closed v1 vocabulary for `promotionRecord.result`. An unknown value fails closed.
PROMOTION_RESULT_PROMOTED: Final[str] = "promoted"
PROMOTION_RESULT_REJECTED: Final[str] = "rejected"
PROMOTION_RESULTS: Final[tuple[str, ...]] = (
    PROMOTION_RESULT_PROMOTED,
    PROMOTION_RESULT_REJECTED,
)

#: Closed v1 vocabulary for `remediation.kind`. An unknown value fails closed.
REMEDIATION_KIND_SIGN_IN: Final[str] = "sign_in"
REMEDIATION_KIND_REQUEST_MEMBERSHIP: Final[str] = "request_membership"
REMEDIATION_KIND_REQUEST_PERMISSION: Final[str] = "request_permission"
REMEDIATION_KIND_REQUEST_APPROVAL: Final[str] = "request_approval"
REMEDIATION_KIND_MANAGE_ENTITLEMENT: Final[str] = "manage_entitlement"
REMEDIATION_KIND_CONFIGURE: Final[str] = "configure"
REMEDIATION_KIND_INSTALL_COMPANION: Final[str] = "install_companion"
REMEDIATION_KIND_RETRY_LATER: Final[str] = "retry_later"
REMEDIATION_KIND_CONTACT_ADMINISTRATOR: Final[str] = "contact_administrator"
REMEDIATION_KIND_NONE: Final[str] = "none"
REMEDIATION_KINDS: Final[tuple[str, ...]] = (
    REMEDIATION_KIND_SIGN_IN,
    REMEDIATION_KIND_REQUEST_MEMBERSHIP,
    REMEDIATION_KIND_REQUEST_PERMISSION,
    REMEDIATION_KIND_REQUEST_APPROVAL,
    REMEDIATION_KIND_MANAGE_ENTITLEMENT,
    REMEDIATION_KIND_CONFIGURE,
    REMEDIATION_KIND_INSTALL_COMPANION,
    REMEDIATION_KIND_RETRY_LATER,
    REMEDIATION_KIND_CONTACT_ADMINISTRATOR,
    REMEDIATION_KIND_NONE,
)

#: The fixed v1 `target` vocabulary. An unknown value fails closed.
TARGET_DESKTOP: Final[str] = "desktop"
TARGET_CLOUD: Final[str] = "cloud"
TARGET_CONFORMANCE: Final[str] = "conformance"
TARGETS: Final[tuple[str, ...]] = (
    TARGET_DESKTOP,
    TARGET_CLOUD,
    TARGET_CONFORMANCE,
)

#: Closed v1 vocabulary for `testDriverRequest.operation`. An unknown value fails closed.
TEST_DRIVER_OPERATION_NAVIGATE: Final[str] = "navigate"
TEST_DRIVER_OPERATION_INVOKE_COMMAND: Final[str] = "invoke_command"
TEST_DRIVER_OPERATION_CAPTURE_SCREENSHOT: Final[str] = "capture_screenshot"
TEST_DRIVER_OPERATION_RUN_ACCESSIBILITY_CHECK: Final[str] = "run_accessibility_check"
TEST_DRIVER_OPERATION_CAPTURE_TRACE: Final[str] = "capture_trace"
TEST_DRIVER_OPERATION_INSPECT_STATE: Final[str] = "inspect_state"
TEST_DRIVER_OPERATIONS: Final[tuple[str, ...]] = (
    TEST_DRIVER_OPERATION_NAVIGATE,
    TEST_DRIVER_OPERATION_INVOKE_COMMAND,
    TEST_DRIVER_OPERATION_CAPTURE_SCREENSHOT,
    TEST_DRIVER_OPERATION_RUN_ACCESSIBILITY_CHECK,
    TEST_DRIVER_OPERATION_CAPTURE_TRACE,
    TEST_DRIVER_OPERATION_INSPECT_STATE,
)

#: Closed v1 vocabulary for `testDriverResult.outcome`. An unknown value fails closed.
TEST_DRIVER_OUTCOME_PASSED: Final[str] = "passed"
TEST_DRIVER_OUTCOME_FAILED: Final[str] = "failed"
TEST_DRIVER_OUTCOME_UNAVAILABLE: Final[str] = "unavailable"
TEST_DRIVER_OUTCOMES: Final[tuple[str, ...]] = (
    TEST_DRIVER_OUTCOME_PASSED,
    TEST_DRIVER_OUTCOME_FAILED,
    TEST_DRIVER_OUTCOME_UNAVAILABLE,
)
#: A `identifier` value.
Identifier: TypeAlias = str
IDENTIFIER_PATTERN: Final[str] = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"

#: A `semver` value.
SemVer: TypeAlias = str
SEM_VER_PATTERN: Final[str] = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"

#: A `contractVersion` value.
ContractVersion: TypeAlias = str
CONTRACT_VERSION_PATTERN: Final[str] = r"^1\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"

#: A `sha256` value.
Sha256: TypeAlias = str
SHA256_PATTERN: Final[str] = r"^[a-f0-9]{64}$"

#: A `target` value. Closed: see TARGETS.
Target: TypeAlias = str

#: A `namespace` value. Closed: see HOST_NAMESPACES.
HostNamespace: TypeAlias = str

#: A `referenceArray` value. Present-and-empty is not the same as absent.
LogicalReferences: TypeAlias = tuple[Identifier, ...]
#: Every governed top-level record, in the canonical schema's `oneOf` order.
HOST_CONTRACT_RECORDS: Final[tuple[str, ...]] = (
    "HostRequest",
    "HostResult",
    "ShellContext",
    "CompatibilityResult",
    "CapabilityAvailability",
    "CapabilityDenial",
    "ModuleContributionDeclaration",
    "ReleasePackageIdentity",
    "PromotionRecord",
    "RollbackRecord",
    "EnvironmentBinding",
    "DevelopmentProfile",
    "ShellScenario",
    "TestDriverRequest",
    "TestDriverResult",
)
@dataclass(frozen=True, slots=True)
class CapabilityAvailability:
    """Whether a capability can be used on a target, and why not when it cannot. An unknown state
    fails closed and is never treated as available.
    """

    state: str
    capability_id: Identifier
    target: Target
    safe_reason: str | None = None
    companion_id: Identifier | None = None
    retry_after_seconds: int | None = None
    remediation: Remediation | None = None

    #: Every canonical wire field spelling this record declares.
    WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset({"state", "capabilityId", "target", "safeReason", "companionId", "retryAfterSeconds", "remediation"})
    #: Whether the canonical schema refuses fields it does not declare.
    CLOSED: ClassVar[bool] = True

    def validate(self, path: str = "CapabilityAvailability") -> None:
        """Raise unless every field satisfies the canonical schema. Runtime values are checked, not
        annotations: a record built by calling this constructor with hostile values is refused
        here exactly as a hostile wire document is. No diagnostic reproduces a rejected value.
        """
        _check_enum(self.state, AVAILABILITY_STATES, f"{path}.state")
        _check_str(self.capability_id, f"{path}.capabilityId", pattern=IDENTIFIER_PATTERN)
        _check_enum(self.target, TARGETS, f"{path}.target")
        if self.safe_reason is not None:
            _check_str(self.safe_reason, f"{path}.safeReason", max_length=500)
        if self.companion_id is not None:
            _check_str(self.companion_id, f"{path}.companionId", pattern=IDENTIFIER_PATTERN)
        if self.retry_after_seconds is not None:
            _check_int(self.retry_after_seconds, f"{path}.retryAfterSeconds", minimum=0)
        if self.remediation is not None:
            _check_record(self.remediation, Remediation, f"{path}.remediation")

    def to_wire(self) -> dict[str, Any]:
        """Validate this value and render it as a JSON-compatible mapping using the canonical field
        spelling. An absent optional field is omitted rather than emitted as null, so a
        decode/encode round trip reproduces the original document exactly. Emission is validated
        so a record that was built rather than decoded cannot become an invalid wire document.
        """
        self.validate()
        wire: dict[str, Any] = {}
        wire["state"] = self.state
        wire["capabilityId"] = self.capability_id
        wire["target"] = self.target
        if self.safe_reason is not None:
            wire["safeReason"] = self.safe_reason
        if self.companion_id is not None:
            wire["companionId"] = self.companion_id
        if self.retry_after_seconds is not None:
            wire["retryAfterSeconds"] = self.retry_after_seconds
        if self.remediation is not None:
            wire["remediation"] = self.remediation.to_wire()
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "CapabilityAvailability") -> CapabilityAvailability:
        """Decode a wire payload into a CapabilityAvailability. Unknown fields are refused: this
        record is closed by the canonical schema. The decoded value is then validated against
        every governed constraint, so this is the strict admission path rather than a structural
        one: a missing required field, a wrongly typed value, an unknown closed-vocabulary member,
        a pattern, length, range, timestamp, cardinality or uniqueness violation all raise
        HostContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        _reject_unknown_fields(mapping, cls.WIRE_FIELDS, path)
        field_state = _decode_enum(_require_field(mapping, "state", path), AVAILABILITY_STATES, f"{path}.state")
        field_capability_id = _decode_str(_require_field(mapping, "capabilityId", path), f"{path}.capabilityId")
        field_target = _decode_enum(_require_field(mapping, "target", path), TARGETS, f"{path}.target")
        field_safe_reason = None
        if "safeReason" in mapping:
            field_safe_reason = _decode_str(mapping["safeReason"], f"{path}.safeReason")
        field_companion_id = None
        if "companionId" in mapping:
            field_companion_id = _decode_str(mapping["companionId"], f"{path}.companionId")
        field_retry_after_seconds = None
        if "retryAfterSeconds" in mapping:
            field_retry_after_seconds = _decode_int(mapping["retryAfterSeconds"], f"{path}.retryAfterSeconds")
        field_remediation = None
        if "remediation" in mapping:
            field_remediation = Remediation.from_wire(mapping["remediation"], f"{path}.remediation")
        decoded = cls(
            state=field_state,
            capability_id=field_capability_id,
            target=field_target,
            safe_reason=field_safe_reason,
            companion_id=field_companion_id,
            retry_after_seconds=field_retry_after_seconds,
            remediation=field_remediation,
        )
        decoded.validate(path)
        return decoded


@dataclass(frozen=True, slots=True)
class Remediation:
    """The safe next step a denial or availability answer offers the user."""

    kind: str
    route_id: Identifier | None = None

    #: Every canonical wire field spelling this record declares.
    WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset({"kind", "routeId"})
    #: Whether the canonical schema refuses fields it does not declare.
    CLOSED: ClassVar[bool] = True

    def validate(self, path: str = "Remediation") -> None:
        """Raise unless every field satisfies the canonical schema. Runtime values are checked, not
        annotations: a record built by calling this constructor with hostile values is refused
        here exactly as a hostile wire document is. No diagnostic reproduces a rejected value.
        """
        _check_enum(self.kind, REMEDIATION_KINDS, f"{path}.kind")
        if self.route_id is not None:
            _check_str(self.route_id, f"{path}.routeId", pattern=IDENTIFIER_PATTERN)

    def to_wire(self) -> dict[str, Any]:
        """Validate this value and render it as a JSON-compatible mapping using the canonical field
        spelling. An absent optional field is omitted rather than emitted as null, so a
        decode/encode round trip reproduces the original document exactly. Emission is validated
        so a record that was built rather than decoded cannot become an invalid wire document.
        """
        self.validate()
        wire: dict[str, Any] = {}
        wire["kind"] = self.kind
        if self.route_id is not None:
            wire["routeId"] = self.route_id
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "Remediation") -> Remediation:
        """Decode a wire payload into a Remediation. Unknown fields are refused: this record is
        closed by the canonical schema. The decoded value is then validated against every governed
        constraint, so this is the strict admission path rather than a structural one: a missing
        required field, a wrongly typed value, an unknown closed-vocabulary member, a pattern,
        length, range, timestamp, cardinality or uniqueness violation all raise
        HostContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        _reject_unknown_fields(mapping, cls.WIRE_FIELDS, path)
        field_kind = _decode_enum(_require_field(mapping, "kind", path), REMEDIATION_KINDS, f"{path}.kind")
        field_route_id = None
        if "routeId" in mapping:
            field_route_id = _decode_str(mapping["routeId"], f"{path}.routeId")
        decoded = cls(
            kind=field_kind,
            route_id=field_route_id,
        )
        decoded.validate(path)
        return decoded


@dataclass(frozen=True, slots=True)
class CapabilityDenial:
    """A refusal record whose closed field set constrains shape but does not prove display content
    safety. A trusted host validator or redactor must admit `safeReason`; otherwise the public
    display boundary returns a fixed generic correlation-bound refusal.
    """

    category: str
    capability_id: Identifier
    safe_reason: str
    approval_available: bool
    correlation_id: Identifier
    audit_ref: Identifier | None = None
    remediation: Remediation | None = None

    #: Every canonical wire field spelling this record declares.
    WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset({"category", "capabilityId", "safeReason", "approvalAvailable", "correlationId", "auditRef", "remediation"})
    #: Whether the canonical schema refuses fields it does not declare.
    CLOSED: ClassVar[bool] = True

    def validate(self, path: str = "CapabilityDenial") -> None:
        """Raise unless every field satisfies the canonical schema. Runtime values are checked, not
        annotations: a record built by calling this constructor with hostile values is refused
        here exactly as a hostile wire document is. No diagnostic reproduces a rejected value.
        """
        _check_enum(self.category, DENIAL_CATEGORIES, f"{path}.category")
        _check_str(self.capability_id, f"{path}.capabilityId", pattern=IDENTIFIER_PATTERN)
        _check_str(self.safe_reason, f"{path}.safeReason", min_length=1, max_length=500)
        _check_bool(self.approval_available, f"{path}.approvalAvailable")
        _check_str(self.correlation_id, f"{path}.correlationId", pattern=IDENTIFIER_PATTERN)
        if self.audit_ref is not None:
            _check_str(self.audit_ref, f"{path}.auditRef", pattern=IDENTIFIER_PATTERN)
        if self.remediation is not None:
            _check_record(self.remediation, Remediation, f"{path}.remediation")

    def to_wire(self) -> dict[str, Any]:
        """Validate this value and render it as a JSON-compatible mapping using the canonical field
        spelling. An absent optional field is omitted rather than emitted as null, so a
        decode/encode round trip reproduces the original document exactly. Emission is validated
        so a record that was built rather than decoded cannot become an invalid wire document.
        """
        self.validate()
        wire: dict[str, Any] = {}
        wire["category"] = self.category
        wire["capabilityId"] = self.capability_id
        wire["safeReason"] = self.safe_reason
        wire["approvalAvailable"] = self.approval_available
        wire["correlationId"] = self.correlation_id
        if self.audit_ref is not None:
            wire["auditRef"] = self.audit_ref
        if self.remediation is not None:
            wire["remediation"] = self.remediation.to_wire()
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "CapabilityDenial") -> CapabilityDenial:
        """Decode a wire payload into a CapabilityDenial. Unknown fields are refused: this record is
        closed by the canonical schema. The decoded value is then validated against every governed
        constraint, so this is the strict admission path rather than a structural one: a missing
        required field, a wrongly typed value, an unknown closed-vocabulary member, a pattern,
        length, range, timestamp, cardinality or uniqueness violation all raise
        HostContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        _reject_unknown_fields(mapping, cls.WIRE_FIELDS, path)
        field_category = _decode_enum(_require_field(mapping, "category", path), DENIAL_CATEGORIES, f"{path}.category")
        field_capability_id = _decode_str(_require_field(mapping, "capabilityId", path), f"{path}.capabilityId")
        field_safe_reason = _decode_str(_require_field(mapping, "safeReason", path), f"{path}.safeReason")
        field_approval_available = _decode_bool(_require_field(mapping, "approvalAvailable", path), f"{path}.approvalAvailable")
        field_correlation_id = _decode_str(_require_field(mapping, "correlationId", path), f"{path}.correlationId")
        field_audit_ref = None
        if "auditRef" in mapping:
            field_audit_ref = _decode_str(mapping["auditRef"], f"{path}.auditRef")
        field_remediation = None
        if "remediation" in mapping:
            field_remediation = Remediation.from_wire(mapping["remediation"], f"{path}.remediation")
        decoded = cls(
            category=field_category,
            capability_id=field_capability_id,
            safe_reason=field_safe_reason,
            approval_available=field_approval_available,
            correlation_id=field_correlation_id,
            audit_ref=field_audit_ref,
            remediation=field_remediation,
        )
        decoded.validate(path)
        return decoded


@dataclass(frozen=True, slots=True)
class HostFailure:
    """A safe description of an operation that failed for neither denial nor availability reasons.
    """

    code: Identifier
    safe_reason: str

    #: Every canonical wire field spelling this record declares.
    WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset({"code", "safeReason"})
    #: Whether the canonical schema refuses fields it does not declare.
    CLOSED: ClassVar[bool] = True

    def validate(self, path: str = "HostFailure") -> None:
        """Raise unless every field satisfies the canonical schema. Runtime values are checked, not
        annotations: a record built by calling this constructor with hostile values is refused
        here exactly as a hostile wire document is. No diagnostic reproduces a rejected value.
        """
        _check_str(self.code, f"{path}.code", pattern=IDENTIFIER_PATTERN)
        _check_str(self.safe_reason, f"{path}.safeReason", min_length=1, max_length=500)

    def to_wire(self) -> dict[str, Any]:
        """Validate this value and render it as a JSON-compatible mapping using the canonical field
        spelling. An absent optional field is omitted rather than emitted as null, so a
        decode/encode round trip reproduces the original document exactly. Emission is validated
        so a record that was built rather than decoded cannot become an invalid wire document.
        """
        self.validate()
        wire: dict[str, Any] = {}
        wire["code"] = self.code
        wire["safeReason"] = self.safe_reason
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "HostFailure") -> HostFailure:
        """Decode a wire payload into a HostFailure. Unknown fields are refused: this record is
        closed by the canonical schema. The decoded value is then validated against every governed
        constraint, so this is the strict admission path rather than a structural one: a missing
        required field, a wrongly typed value, an unknown closed-vocabulary member, a pattern,
        length, range, timestamp, cardinality or uniqueness violation all raise
        HostContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        _reject_unknown_fields(mapping, cls.WIRE_FIELDS, path)
        field_code = _decode_str(_require_field(mapping, "code", path), f"{path}.code")
        field_safe_reason = _decode_str(_require_field(mapping, "safeReason", path), f"{path}.safeReason")
        decoded = cls(
            code=field_code,
            safe_reason=field_safe_reason,
        )
        decoded.validate(path)
        return decoded


@dataclass(frozen=True, slots=True)
class HostResult:
    """The total result envelope. Exactly one of `data`, `denial`, `availability` or `failure`
    matches `outcome`; a degraded success additionally carries `availability` alongside `data`.
    """

    contract_version: ContractVersion
    request_id: Identifier
    outcome: str
    correlation_id: Identifier
    data: JsonObject | None = None
    availability: CapabilityAvailability | None = None
    denial: CapabilityDenial | None = None
    failure: HostFailure | None = None
    audit_ref: Identifier | None = None

    #: Every canonical wire field spelling this record declares.
    WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset({"contractVersion", "requestId", "outcome", "correlationId", "data", "availability", "denial", "failure", "auditRef"})
    #: Whether the canonical schema refuses fields it does not declare.
    CLOSED: ClassVar[bool] = True

    def validate(self, path: str = "HostResult") -> None:
        """Raise unless every field satisfies the canonical schema. Runtime values are checked, not
        annotations: a record built by calling this constructor with hostile values is refused
        here exactly as a hostile wire document is. No diagnostic reproduces a rejected value.
        """
        _check_str(self.contract_version, f"{path}.contractVersion", pattern=CONTRACT_VERSION_PATTERN)
        _check_str(self.request_id, f"{path}.requestId", pattern=IDENTIFIER_PATTERN)
        _check_enum(self.outcome, OUTCOMES, f"{path}.outcome")
        _check_str(self.correlation_id, f"{path}.correlationId", pattern=IDENTIFIER_PATTERN)
        if self.data is not None:
            _check_json_object(self.data, f"{path}.data")
        if self.availability is not None:
            _check_record(self.availability, CapabilityAvailability, f"{path}.availability")
        if self.denial is not None:
            _check_record(self.denial, CapabilityDenial, f"{path}.denial")
        if self.failure is not None:
            _check_record(self.failure, HostFailure, f"{path}.failure")
        if self.audit_ref is not None:
            _check_str(self.audit_ref, f"{path}.auditRef", pattern=IDENTIFIER_PATTERN)
        self.validate_branches(path)

    def validate_branches(self, path: str = "HostResult") -> None:
        """Raise unless exactly the branch `outcome` names is present. Transcribed from the canonical
        schema's `allOf`, so the envelope stays total: an outcome without its branch, or carrying
        one that belongs to a different outcome, is a protocol violation rather than something to
        resolve by preferring one side.
        """
        _check_branches(
            self.outcome,
            {
                "data": self.data,
                "denial": self.denial,
                "failure": self.failure,
                "availability": self.availability,
            },
            {
                "success": (("data",), ("denial", "failure")),
                "denied": (("denial",), ("data", "availability", "failure")),
                "unavailable": (("availability",), ("data", "denial", "failure")),
                "failed": (("failure",), ("data", "availability", "denial")),
            },
            path,
        )

    def to_wire(self) -> dict[str, Any]:
        """Validate this value and render it as a JSON-compatible mapping using the canonical field
        spelling. An absent optional field is omitted rather than emitted as null, so a
        decode/encode round trip reproduces the original document exactly. Emission is validated
        so a record that was built rather than decoded cannot become an invalid wire document.
        """
        self.validate()
        wire: dict[str, Any] = {}
        wire["contractVersion"] = self.contract_version
        wire["requestId"] = self.request_id
        wire["outcome"] = self.outcome
        wire["correlationId"] = self.correlation_id
        if self.data is not None:
            wire["data"] = _encode_json_object(self.data)
        if self.availability is not None:
            wire["availability"] = self.availability.to_wire()
        if self.denial is not None:
            wire["denial"] = self.denial.to_wire()
        if self.failure is not None:
            wire["failure"] = self.failure.to_wire()
        if self.audit_ref is not None:
            wire["auditRef"] = self.audit_ref
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "HostResult") -> HostResult:
        """Decode a wire payload into a HostResult. Unknown fields are refused: this record is closed
        by the canonical schema. The decoded value is then validated against every governed
        constraint, so this is the strict admission path rather than a structural one: a missing
        required field, a wrongly typed value, an unknown closed-vocabulary member, a pattern,
        length, range, timestamp, cardinality or uniqueness violation all raise
        HostContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        _reject_unknown_fields(mapping, cls.WIRE_FIELDS, path)
        field_contract_version = _decode_str(_require_field(mapping, "contractVersion", path), f"{path}.contractVersion")
        field_request_id = _decode_str(_require_field(mapping, "requestId", path), f"{path}.requestId")
        field_outcome = _decode_enum(_require_field(mapping, "outcome", path), OUTCOMES, f"{path}.outcome")
        field_correlation_id = _decode_str(_require_field(mapping, "correlationId", path), f"{path}.correlationId")
        field_data = None
        if "data" in mapping:
            field_data = _decode_json_object(mapping["data"], f"{path}.data")
        field_availability = None
        if "availability" in mapping:
            field_availability = CapabilityAvailability.from_wire(mapping["availability"], f"{path}.availability")
        field_denial = None
        if "denial" in mapping:
            field_denial = CapabilityDenial.from_wire(mapping["denial"], f"{path}.denial")
        field_failure = None
        if "failure" in mapping:
            field_failure = HostFailure.from_wire(mapping["failure"], f"{path}.failure")
        field_audit_ref = None
        if "auditRef" in mapping:
            field_audit_ref = _decode_str(mapping["auditRef"], f"{path}.auditRef")
        decoded = cls(
            contract_version=field_contract_version,
            request_id=field_request_id,
            outcome=field_outcome,
            correlation_id=field_correlation_id,
            data=field_data,
            availability=field_availability,
            denial=field_denial,
            failure=field_failure,
            audit_ref=field_audit_ref,
        )
        decoded.validate(path)
        return decoded


@dataclass(frozen=True, slots=True)
class HostRequest:
    """One cross-host operation attempt. `contextId` is a lookup into the trusted host's ShellContext
    store: `payload` carries operation input only and never establishes principal, Workspace,
    permission, entitlement or environment authority.
    """

    contract_version: ContractVersion
    request_id: Identifier
    namespace: HostNamespace
    operation: Identifier
    context_id: Identifier
    payload: JsonObject
    idempotency_key: Identifier | None = None
    expected_state_version: int | None = None

    #: Every canonical wire field spelling this record declares.
    WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset({"contractVersion", "requestId", "namespace", "operation", "contextId", "payload", "idempotencyKey", "expectedStateVersion"})
    #: Whether the canonical schema refuses fields it does not declare.
    CLOSED: ClassVar[bool] = False

    def validate(self, path: str = "HostRequest") -> None:
        """Raise unless every field satisfies the canonical schema. Runtime values are checked, not
        annotations: a record built by calling this constructor with hostile values is refused
        here exactly as a hostile wire document is. No diagnostic reproduces a rejected value.
        """
        _check_str(self.contract_version, f"{path}.contractVersion", pattern=CONTRACT_VERSION_PATTERN)
        _check_str(self.request_id, f"{path}.requestId", pattern=IDENTIFIER_PATTERN)
        _check_enum(self.namespace, HOST_NAMESPACES, f"{path}.namespace")
        _check_str(self.operation, f"{path}.operation", pattern=IDENTIFIER_PATTERN)
        _check_str(self.context_id, f"{path}.contextId", pattern=IDENTIFIER_PATTERN)
        _check_json_object(self.payload, f"{path}.payload")
        if self.idempotency_key is not None:
            _check_str(self.idempotency_key, f"{path}.idempotencyKey", pattern=IDENTIFIER_PATTERN)
        if self.expected_state_version is not None:
            _check_int(self.expected_state_version, f"{path}.expectedStateVersion", minimum=0)

    def to_wire(self) -> dict[str, Any]:
        """Validate this value and render it as a JSON-compatible mapping using the canonical field
        spelling. An absent optional field is omitted rather than emitted as null, so a
        decode/encode round trip reproduces every field this record declares exactly. It
        reproduces nothing else: this record is open, so a decode kept only the declared fields
        and there is no unknown remainder left to re-emit. Emission is validated so a record that
        was built rather than decoded cannot become an invalid wire document.
        """
        self.validate()
        wire: dict[str, Any] = {}
        wire["contractVersion"] = self.contract_version
        wire["requestId"] = self.request_id
        wire["namespace"] = self.namespace
        wire["operation"] = self.operation
        wire["contextId"] = self.context_id
        wire["payload"] = _encode_json_object(self.payload)
        if self.idempotency_key is not None:
            wire["idempotencyKey"] = self.idempotency_key
        if self.expected_state_version is not None:
            wire["expectedStateVersion"] = self.expected_state_version
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "HostRequest") -> HostRequest:
        """Decode a wire payload into a HostRequest. Unknown fields are ignored, so a newer additive
        minor release still decodes here. The decoded value is then validated against every
        governed constraint, so this is the strict admission path rather than a structural one: a
        missing required field, a wrongly typed value, an unknown closed-vocabulary member, a
        pattern, length, range, timestamp, cardinality or uniqueness violation all raise
        HostContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        field_contract_version = _decode_str(_require_field(mapping, "contractVersion", path), f"{path}.contractVersion")
        field_request_id = _decode_str(_require_field(mapping, "requestId", path), f"{path}.requestId")
        field_namespace = _decode_enum(_require_field(mapping, "namespace", path), HOST_NAMESPACES, f"{path}.namespace")
        field_operation = _decode_str(_require_field(mapping, "operation", path), f"{path}.operation")
        field_context_id = _decode_str(_require_field(mapping, "contextId", path), f"{path}.contextId")
        field_payload = _decode_json_object(_require_field(mapping, "payload", path), f"{path}.payload")
        field_idempotency_key = None
        if "idempotencyKey" in mapping:
            field_idempotency_key = _decode_str(mapping["idempotencyKey"], f"{path}.idempotencyKey")
        field_expected_state_version = None
        if "expectedStateVersion" in mapping:
            field_expected_state_version = _decode_int(mapping["expectedStateVersion"], f"{path}.expectedStateVersion")
        decoded = cls(
            contract_version=field_contract_version,
            request_id=field_request_id,
            namespace=field_namespace,
            operation=field_operation,
            context_id=field_context_id,
            payload=field_payload,
            idempotency_key=field_idempotency_key,
            expected_state_version=field_expected_state_version,
        )
        decoded.validate(path)
        return decoded


@dataclass(frozen=True, slots=True)
class ShellContext:
    """The immutable trusted-host authority record. A Workspace, principal, entitlement, permission,
    runtime or environment change creates a new context ID rather than mutating this one.
    """

    context_id: Identifier
    context_version: int
    principal_ref: Identifier
    organisation_ref: Identifier
    workspace_ref: Identifier
    entitlement_summary_ref: Identifier
    permission_summary_ref: Identifier
    runtime_id: Identifier
    environment_id: Identifier
    target: Target
    issued_at: str
    expires_at: str | None = None

    #: Every canonical wire field spelling this record declares.
    WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset({"contextId", "contextVersion", "principalRef", "organisationRef", "workspaceRef", "entitlementSummaryRef", "permissionSummaryRef", "runtimeId", "environmentId", "target", "issuedAt", "expiresAt"})
    #: Whether the canonical schema refuses fields it does not declare.
    CLOSED: ClassVar[bool] = True

    def validate(self, path: str = "ShellContext") -> None:
        """Raise unless every field satisfies the canonical schema. Runtime values are checked, not
        annotations: a record built by calling this constructor with hostile values is refused
        here exactly as a hostile wire document is. No diagnostic reproduces a rejected value.
        """
        _check_str(self.context_id, f"{path}.contextId", pattern=IDENTIFIER_PATTERN)
        _check_int(self.context_version, f"{path}.contextVersion", minimum=1)
        _check_str(self.principal_ref, f"{path}.principalRef", pattern=IDENTIFIER_PATTERN)
        _check_str(self.organisation_ref, f"{path}.organisationRef", pattern=IDENTIFIER_PATTERN)
        _check_str(self.workspace_ref, f"{path}.workspaceRef", pattern=IDENTIFIER_PATTERN)
        _check_str(self.entitlement_summary_ref, f"{path}.entitlementSummaryRef", pattern=IDENTIFIER_PATTERN)
        _check_str(self.permission_summary_ref, f"{path}.permissionSummaryRef", pattern=IDENTIFIER_PATTERN)
        _check_str(self.runtime_id, f"{path}.runtimeId", pattern=IDENTIFIER_PATTERN)
        _check_str(self.environment_id, f"{path}.environmentId", pattern=IDENTIFIER_PATTERN)
        _check_enum(self.target, TARGETS, f"{path}.target")
        _check_str(self.issued_at, f"{path}.issuedAt", date_time=True)
        if self.expires_at is not None:
            _check_str(self.expires_at, f"{path}.expiresAt", date_time=True)

    def to_wire(self) -> dict[str, Any]:
        """Validate this value and render it as a JSON-compatible mapping using the canonical field
        spelling. An absent optional field is omitted rather than emitted as null, so a
        decode/encode round trip reproduces the original document exactly. Emission is validated
        so a record that was built rather than decoded cannot become an invalid wire document.
        """
        self.validate()
        wire: dict[str, Any] = {}
        wire["contextId"] = self.context_id
        wire["contextVersion"] = self.context_version
        wire["principalRef"] = self.principal_ref
        wire["organisationRef"] = self.organisation_ref
        wire["workspaceRef"] = self.workspace_ref
        wire["entitlementSummaryRef"] = self.entitlement_summary_ref
        wire["permissionSummaryRef"] = self.permission_summary_ref
        wire["runtimeId"] = self.runtime_id
        wire["environmentId"] = self.environment_id
        wire["target"] = self.target
        wire["issuedAt"] = self.issued_at
        if self.expires_at is not None:
            wire["expiresAt"] = self.expires_at
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "ShellContext") -> ShellContext:
        """Decode a wire payload into a ShellContext. Unknown fields are refused: this record is
        closed by the canonical schema. The decoded value is then validated against every governed
        constraint, so this is the strict admission path rather than a structural one: a missing
        required field, a wrongly typed value, an unknown closed-vocabulary member, a pattern,
        length, range, timestamp, cardinality or uniqueness violation all raise
        HostContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        _reject_unknown_fields(mapping, cls.WIRE_FIELDS, path)
        field_context_id = _decode_str(_require_field(mapping, "contextId", path), f"{path}.contextId")
        field_context_version = _decode_int(_require_field(mapping, "contextVersion", path), f"{path}.contextVersion")
        field_principal_ref = _decode_str(_require_field(mapping, "principalRef", path), f"{path}.principalRef")
        field_organisation_ref = _decode_str(_require_field(mapping, "organisationRef", path), f"{path}.organisationRef")
        field_workspace_ref = _decode_str(_require_field(mapping, "workspaceRef", path), f"{path}.workspaceRef")
        field_entitlement_summary_ref = _decode_str(_require_field(mapping, "entitlementSummaryRef", path), f"{path}.entitlementSummaryRef")
        field_permission_summary_ref = _decode_str(_require_field(mapping, "permissionSummaryRef", path), f"{path}.permissionSummaryRef")
        field_runtime_id = _decode_str(_require_field(mapping, "runtimeId", path), f"{path}.runtimeId")
        field_environment_id = _decode_str(_require_field(mapping, "environmentId", path), f"{path}.environmentId")
        field_target = _decode_enum(_require_field(mapping, "target", path), TARGETS, f"{path}.target")
        field_issued_at = _decode_str(_require_field(mapping, "issuedAt", path), f"{path}.issuedAt")
        field_expires_at = None
        if "expiresAt" in mapping:
            field_expires_at = _decode_str(mapping["expiresAt"], f"{path}.expiresAt")
        decoded = cls(
            context_id=field_context_id,
            context_version=field_context_version,
            principal_ref=field_principal_ref,
            organisation_ref=field_organisation_ref,
            workspace_ref=field_workspace_ref,
            entitlement_summary_ref=field_entitlement_summary_ref,
            permission_summary_ref=field_permission_summary_ref,
            runtime_id=field_runtime_id,
            environment_id=field_environment_id,
            target=field_target,
            issued_at=field_issued_at,
            expires_at=field_expires_at,
        )
        decoded.validate(path)
        return decoded


@dataclass(frozen=True, slots=True)
class CompatibilityResult:
    """The structured outcome of evaluating a declaration against a target. A missing required
    capability can never yield `compatible`.
    """

    status: str
    target: Target
    required_missing: tuple[Identifier, ...]
    optional_missing: tuple[Identifier, ...]
    policy_blocks: tuple[Identifier, ...]
    required_configuration: tuple[Identifier, ...]
    companions: tuple[Identifier, ...]
    safe_reasons: tuple[str, ...]

    #: Every canonical wire field spelling this record declares.
    WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset({"status", "target", "requiredMissing", "optionalMissing", "policyBlocks", "requiredConfiguration", "companions", "safeReasons"})
    #: Whether the canonical schema refuses fields it does not declare.
    CLOSED: ClassVar[bool] = True

    def validate(self, path: str = "CompatibilityResult") -> None:
        """Raise unless every field satisfies the canonical schema. Runtime values are checked, not
        annotations: a record built by calling this constructor with hostile values is refused
        here exactly as a hostile wire document is. No diagnostic reproduces a rejected value.
        """
        _check_enum(self.status, COMPATIBILITY_STATUSES, f"{path}.status")
        _check_enum(self.target, TARGETS, f"{path}.target")
        _check_sequence(self.required_missing, f"{path}.requiredMissing", unique=True)
        for index, entry_required_missing in enumerate(self.required_missing):
            _check_str(entry_required_missing, f"{path}.requiredMissing[{index}]", pattern=IDENTIFIER_PATTERN)
        _check_sequence(self.optional_missing, f"{path}.optionalMissing", unique=True)
        for index, entry_optional_missing in enumerate(self.optional_missing):
            _check_str(entry_optional_missing, f"{path}.optionalMissing[{index}]", pattern=IDENTIFIER_PATTERN)
        _check_sequence(self.policy_blocks, f"{path}.policyBlocks", unique=True)
        for index, entry_policy_blocks in enumerate(self.policy_blocks):
            _check_str(entry_policy_blocks, f"{path}.policyBlocks[{index}]", pattern=IDENTIFIER_PATTERN)
        _check_sequence(self.required_configuration, f"{path}.requiredConfiguration", unique=True)
        for index, entry_required_configuration in enumerate(self.required_configuration):
            _check_str(entry_required_configuration, f"{path}.requiredConfiguration[{index}]", pattern=IDENTIFIER_PATTERN)
        _check_sequence(self.companions, f"{path}.companions", unique=True)
        for index, entry_companions in enumerate(self.companions):
            _check_str(entry_companions, f"{path}.companions[{index}]", pattern=IDENTIFIER_PATTERN)
        _check_sequence(self.safe_reasons, f"{path}.safeReasons")
        for index, entry_safe_reasons in enumerate(self.safe_reasons):
            _check_str(entry_safe_reasons, f"{path}.safeReasons[{index}]")

    def to_wire(self) -> dict[str, Any]:
        """Validate this value and render it as a JSON-compatible mapping using the canonical field
        spelling. An absent optional field is omitted rather than emitted as null, so a
        decode/encode round trip reproduces the original document exactly. Emission is validated
        so a record that was built rather than decoded cannot become an invalid wire document.
        """
        self.validate()
        wire: dict[str, Any] = {}
        wire["status"] = self.status
        wire["target"] = self.target
        wire["requiredMissing"] = list(self.required_missing)
        wire["optionalMissing"] = list(self.optional_missing)
        wire["policyBlocks"] = list(self.policy_blocks)
        wire["requiredConfiguration"] = list(self.required_configuration)
        wire["companions"] = list(self.companions)
        wire["safeReasons"] = list(self.safe_reasons)
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "CompatibilityResult") -> CompatibilityResult:
        """Decode a wire payload into a CompatibilityResult. Unknown fields are refused: this record
        is closed by the canonical schema. The decoded value is then validated against every
        governed constraint, so this is the strict admission path rather than a structural one: a
        missing required field, a wrongly typed value, an unknown closed-vocabulary member, a
        pattern, length, range, timestamp, cardinality or uniqueness violation all raise
        HostContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        _reject_unknown_fields(mapping, cls.WIRE_FIELDS, path)
        field_status = _decode_enum(_require_field(mapping, "status", path), COMPATIBILITY_STATUSES, f"{path}.status")
        field_target = _decode_enum(_require_field(mapping, "target", path), TARGETS, f"{path}.target")
        raw_required_missing = _decode_sequence(_require_field(mapping, "requiredMissing", path), f"{path}.requiredMissing")
        field_required_missing = tuple(_decode_str(entry, f"{path}.requiredMissing[]") for entry in raw_required_missing)
        raw_optional_missing = _decode_sequence(_require_field(mapping, "optionalMissing", path), f"{path}.optionalMissing")
        field_optional_missing = tuple(_decode_str(entry, f"{path}.optionalMissing[]") for entry in raw_optional_missing)
        raw_policy_blocks = _decode_sequence(_require_field(mapping, "policyBlocks", path), f"{path}.policyBlocks")
        field_policy_blocks = tuple(_decode_str(entry, f"{path}.policyBlocks[]") for entry in raw_policy_blocks)
        raw_required_configuration = _decode_sequence(_require_field(mapping, "requiredConfiguration", path), f"{path}.requiredConfiguration")
        field_required_configuration = tuple(_decode_str(entry, f"{path}.requiredConfiguration[]") for entry in raw_required_configuration)
        raw_companions = _decode_sequence(_require_field(mapping, "companions", path), f"{path}.companions")
        field_companions = tuple(_decode_str(entry, f"{path}.companions[]") for entry in raw_companions)
        raw_safe_reasons = _decode_sequence(_require_field(mapping, "safeReasons", path), f"{path}.safeReasons")
        field_safe_reasons = tuple(_decode_str(entry, f"{path}.safeReasons[]") for entry in raw_safe_reasons)
        decoded = cls(
            status=field_status,
            target=field_target,
            required_missing=field_required_missing,
            optional_missing=field_optional_missing,
            policy_blocks=field_policy_blocks,
            required_configuration=field_required_configuration,
            companions=field_companions,
            safe_reasons=field_safe_reasons,
        )
        decoded.validate(path)
        return decoded


@dataclass(frozen=True, slots=True)
class Contribution:
    """One registry entry a Module release contributes to the shell."""

    contribution_id: Identifier
    kind: str
    target_id: Identifier
    required_capabilities: tuple[Identifier, ...]
    optional_capabilities: tuple[Identifier, ...]
    payload_schema_id: Identifier
    priority: int | None = None

    #: Every canonical wire field spelling this record declares.
    WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset({"contributionId", "kind", "targetId", "requiredCapabilities", "optionalCapabilities", "payloadSchemaId", "priority"})
    #: Whether the canonical schema refuses fields it does not declare.
    CLOSED: ClassVar[bool] = True

    def validate(self, path: str = "Contribution") -> None:
        """Raise unless every field satisfies the canonical schema. Runtime values are checked, not
        annotations: a record built by calling this constructor with hostile values is refused
        here exactly as a hostile wire document is. No diagnostic reproduces a rejected value.
        """
        _check_str(self.contribution_id, f"{path}.contributionId", pattern=IDENTIFIER_PATTERN)
        _check_enum(self.kind, CONTRIBUTION_KINDS, f"{path}.kind")
        _check_str(self.target_id, f"{path}.targetId", pattern=IDENTIFIER_PATTERN)
        _check_sequence(self.required_capabilities, f"{path}.requiredCapabilities", unique=True)
        for index, entry_required_capabilities in enumerate(self.required_capabilities):
            _check_str(entry_required_capabilities, f"{path}.requiredCapabilities[{index}]", pattern=IDENTIFIER_PATTERN)
        _check_sequence(self.optional_capabilities, f"{path}.optionalCapabilities", unique=True)
        for index, entry_optional_capabilities in enumerate(self.optional_capabilities):
            _check_str(entry_optional_capabilities, f"{path}.optionalCapabilities[{index}]", pattern=IDENTIFIER_PATTERN)
        _check_str(self.payload_schema_id, f"{path}.payloadSchemaId", pattern=IDENTIFIER_PATTERN)
        if self.priority is not None:
            _check_int(self.priority, f"{path}.priority")

    def to_wire(self) -> dict[str, Any]:
        """Validate this value and render it as a JSON-compatible mapping using the canonical field
        spelling. An absent optional field is omitted rather than emitted as null, so a
        decode/encode round trip reproduces the original document exactly. Emission is validated
        so a record that was built rather than decoded cannot become an invalid wire document.
        """
        self.validate()
        wire: dict[str, Any] = {}
        wire["contributionId"] = self.contribution_id
        wire["kind"] = self.kind
        wire["targetId"] = self.target_id
        wire["requiredCapabilities"] = list(self.required_capabilities)
        wire["optionalCapabilities"] = list(self.optional_capabilities)
        wire["payloadSchemaId"] = self.payload_schema_id
        if self.priority is not None:
            wire["priority"] = self.priority
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "Contribution") -> Contribution:
        """Decode a wire payload into a Contribution. Unknown fields are refused: this record is
        closed by the canonical schema. The decoded value is then validated against every governed
        constraint, so this is the strict admission path rather than a structural one: a missing
        required field, a wrongly typed value, an unknown closed-vocabulary member, a pattern,
        length, range, timestamp, cardinality or uniqueness violation all raise
        HostContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        _reject_unknown_fields(mapping, cls.WIRE_FIELDS, path)
        field_contribution_id = _decode_str(_require_field(mapping, "contributionId", path), f"{path}.contributionId")
        field_kind = _decode_enum(_require_field(mapping, "kind", path), CONTRIBUTION_KINDS, f"{path}.kind")
        field_target_id = _decode_str(_require_field(mapping, "targetId", path), f"{path}.targetId")
        raw_required_capabilities = _decode_sequence(_require_field(mapping, "requiredCapabilities", path), f"{path}.requiredCapabilities")
        field_required_capabilities = tuple(_decode_str(entry, f"{path}.requiredCapabilities[]") for entry in raw_required_capabilities)
        raw_optional_capabilities = _decode_sequence(_require_field(mapping, "optionalCapabilities", path), f"{path}.optionalCapabilities")
        field_optional_capabilities = tuple(_decode_str(entry, f"{path}.optionalCapabilities[]") for entry in raw_optional_capabilities)
        field_payload_schema_id = _decode_str(_require_field(mapping, "payloadSchemaId", path), f"{path}.payloadSchemaId")
        field_priority = None
        if "priority" in mapping:
            field_priority = _decode_int(mapping["priority"], f"{path}.priority")
        decoded = cls(
            contribution_id=field_contribution_id,
            kind=field_kind,
            target_id=field_target_id,
            required_capabilities=field_required_capabilities,
            optional_capabilities=field_optional_capabilities,
            payload_schema_id=field_payload_schema_id,
            priority=field_priority,
        )
        decoded.validate(path)
        return decoded


@dataclass(frozen=True, slots=True)
class ModuleContributionDeclaration:
    """Everything one Module release contributes. Registration is all-or-nothing: partial activation
    of a declaration is prohibited.
    """

    declaration_version: ContractVersion
    module_id: Identifier
    module_release_id: Identifier
    contributions: tuple[Contribution, ...]
    required_entitlements: tuple[Identifier, ...]

    #: Every canonical wire field spelling this record declares.
    WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset({"declarationVersion", "moduleId", "moduleReleaseId", "contributions", "requiredEntitlements"})
    #: Whether the canonical schema refuses fields it does not declare.
    CLOSED: ClassVar[bool] = True

    def validate(self, path: str = "ModuleContributionDeclaration") -> None:
        """Raise unless every field satisfies the canonical schema. Runtime values are checked, not
        annotations: a record built by calling this constructor with hostile values is refused
        here exactly as a hostile wire document is. No diagnostic reproduces a rejected value.
        """
        _check_str(self.declaration_version, f"{path}.declarationVersion", pattern=CONTRACT_VERSION_PATTERN)
        _check_str(self.module_id, f"{path}.moduleId", pattern=IDENTIFIER_PATTERN)
        _check_str(self.module_release_id, f"{path}.moduleReleaseId", pattern=IDENTIFIER_PATTERN)
        _check_sequence(self.contributions, f"{path}.contributions", min_items=1)
        for index, entry_contributions in enumerate(self.contributions):
            _check_record(entry_contributions, Contribution, f"{path}.contributions[{index}]")
        _check_sequence(self.required_entitlements, f"{path}.requiredEntitlements", unique=True)
        for index, entry_required_entitlements in enumerate(self.required_entitlements):
            _check_str(entry_required_entitlements, f"{path}.requiredEntitlements[{index}]", pattern=IDENTIFIER_PATTERN)

    def to_wire(self) -> dict[str, Any]:
        """Validate this value and render it as a JSON-compatible mapping using the canonical field
        spelling. An absent optional field is omitted rather than emitted as null, so a
        decode/encode round trip reproduces the original document exactly. Emission is validated
        so a record that was built rather than decoded cannot become an invalid wire document.
        """
        self.validate()
        wire: dict[str, Any] = {}
        wire["declarationVersion"] = self.declaration_version
        wire["moduleId"] = self.module_id
        wire["moduleReleaseId"] = self.module_release_id
        wire["contributions"] = [item.to_wire() for item in self.contributions]
        wire["requiredEntitlements"] = list(self.required_entitlements)
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "ModuleContributionDeclaration") -> ModuleContributionDeclaration:
        """Decode a wire payload into a ModuleContributionDeclaration. Unknown fields are refused:
        this record is closed by the canonical schema. The decoded value is then validated against
        every governed constraint, so this is the strict admission path rather than a structural
        one: a missing required field, a wrongly typed value, an unknown closed-vocabulary member,
        a pattern, length, range, timestamp, cardinality or uniqueness violation all raise
        HostContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        _reject_unknown_fields(mapping, cls.WIRE_FIELDS, path)
        field_declaration_version = _decode_str(_require_field(mapping, "declarationVersion", path), f"{path}.declarationVersion")
        field_module_id = _decode_str(_require_field(mapping, "moduleId", path), f"{path}.moduleId")
        field_module_release_id = _decode_str(_require_field(mapping, "moduleReleaseId", path), f"{path}.moduleReleaseId")
        raw_contributions = _decode_sequence(_require_field(mapping, "contributions", path), f"{path}.contributions")
        field_contributions = tuple(Contribution.from_wire(entry, f"{path}.contributions[]") for entry in raw_contributions)
        raw_required_entitlements = _decode_sequence(_require_field(mapping, "requiredEntitlements", path), f"{path}.requiredEntitlements")
        field_required_entitlements = tuple(_decode_str(entry, f"{path}.requiredEntitlements[]") for entry in raw_required_entitlements)
        decoded = cls(
            declaration_version=field_declaration_version,
            module_id=field_module_id,
            module_release_id=field_module_release_id,
            contributions=field_contributions,
            required_entitlements=field_required_entitlements,
        )
        decoded.validate(path)
        return decoded


@dataclass(frozen=True, slots=True)
class TargetEntry:
    """The entry point a Release Package exposes for one target."""

    target: Target
    entry: str

    #: Every canonical wire field spelling this record declares.
    WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset({"target", "entry"})
    #: Whether the canonical schema refuses fields it does not declare.
    CLOSED: ClassVar[bool] = True

    def validate(self, path: str = "TargetEntry") -> None:
        """Raise unless every field satisfies the canonical schema. Runtime values are checked, not
        annotations: a record built by calling this constructor with hostile values is refused
        here exactly as a hostile wire document is. No diagnostic reproduces a rejected value.
        """
        _check_enum(self.target, TARGETS, f"{path}.target")
        _check_str(self.entry, f"{path}.entry", min_length=1, max_length=500)

    def to_wire(self) -> dict[str, Any]:
        """Validate this value and render it as a JSON-compatible mapping using the canonical field
        spelling. An absent optional field is omitted rather than emitted as null, so a
        decode/encode round trip reproduces the original document exactly. Emission is validated
        so a record that was built rather than decoded cannot become an invalid wire document.
        """
        self.validate()
        wire: dict[str, Any] = {}
        wire["target"] = self.target
        wire["entry"] = self.entry
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "TargetEntry") -> TargetEntry:
        """Decode a wire payload into a TargetEntry. Unknown fields are refused: this record is
        closed by the canonical schema. The decoded value is then validated against every governed
        constraint, so this is the strict admission path rather than a structural one: a missing
        required field, a wrongly typed value, an unknown closed-vocabulary member, a pattern,
        length, range, timestamp, cardinality or uniqueness violation all raise
        HostContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        _reject_unknown_fields(mapping, cls.WIRE_FIELDS, path)
        field_target = _decode_enum(_require_field(mapping, "target", path), TARGETS, f"{path}.target")
        field_entry = _decode_str(_require_field(mapping, "entry", path), f"{path}.entry")
        decoded = cls(
            target=field_target,
            entry=field_entry,
        )
        decoded.validate(path)
        return decoded


@dataclass(frozen=True, slots=True)
class ReleasePackageIdentity:
    """The immutable identity of a Release Package. It carries digests and publisher identity only:
    there is no field for an environment value or a secret.
    """

    app_id: Identifier
    release_id: Identifier
    version: SemVer
    manifest_sha256: Sha256
    payload_sha256: Sha256
    publisher_id: Identifier
    signing_key_id: Identifier
    target_entries: tuple[TargetEntry, ...]

    #: Every canonical wire field spelling this record declares.
    WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset({"appId", "releaseId", "version", "manifestSha256", "payloadSha256", "publisherId", "signingKeyId", "targetEntries"})
    #: Whether the canonical schema refuses fields it does not declare.
    CLOSED: ClassVar[bool] = True

    def validate(self, path: str = "ReleasePackageIdentity") -> None:
        """Raise unless every field satisfies the canonical schema. Runtime values are checked, not
        annotations: a record built by calling this constructor with hostile values is refused
        here exactly as a hostile wire document is. No diagnostic reproduces a rejected value.
        """
        _check_str(self.app_id, f"{path}.appId", pattern=IDENTIFIER_PATTERN)
        _check_str(self.release_id, f"{path}.releaseId", pattern=IDENTIFIER_PATTERN)
        _check_str(self.version, f"{path}.version", pattern=SEM_VER_PATTERN)
        _check_str(self.manifest_sha256, f"{path}.manifestSha256", pattern=SHA256_PATTERN)
        _check_str(self.payload_sha256, f"{path}.payloadSha256", pattern=SHA256_PATTERN)
        _check_str(self.publisher_id, f"{path}.publisherId", pattern=IDENTIFIER_PATTERN)
        _check_str(self.signing_key_id, f"{path}.signingKeyId", pattern=IDENTIFIER_PATTERN)
        _check_sequence(self.target_entries, f"{path}.targetEntries", min_items=1)
        for index, entry_target_entries in enumerate(self.target_entries):
            _check_record(entry_target_entries, TargetEntry, f"{path}.targetEntries[{index}]")

    def to_wire(self) -> dict[str, Any]:
        """Validate this value and render it as a JSON-compatible mapping using the canonical field
        spelling. An absent optional field is omitted rather than emitted as null, so a
        decode/encode round trip reproduces the original document exactly. Emission is validated
        so a record that was built rather than decoded cannot become an invalid wire document.
        """
        self.validate()
        wire: dict[str, Any] = {}
        wire["appId"] = self.app_id
        wire["releaseId"] = self.release_id
        wire["version"] = self.version
        wire["manifestSha256"] = self.manifest_sha256
        wire["payloadSha256"] = self.payload_sha256
        wire["publisherId"] = self.publisher_id
        wire["signingKeyId"] = self.signing_key_id
        wire["targetEntries"] = [item.to_wire() for item in self.target_entries]
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "ReleasePackageIdentity") -> ReleasePackageIdentity:
        """Decode a wire payload into a ReleasePackageIdentity. Unknown fields are refused: this
        record is closed by the canonical schema. The decoded value is then validated against
        every governed constraint, so this is the strict admission path rather than a structural
        one: a missing required field, a wrongly typed value, an unknown closed-vocabulary member,
        a pattern, length, range, timestamp, cardinality or uniqueness violation all raise
        HostContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        _reject_unknown_fields(mapping, cls.WIRE_FIELDS, path)
        field_app_id = _decode_str(_require_field(mapping, "appId", path), f"{path}.appId")
        field_release_id = _decode_str(_require_field(mapping, "releaseId", path), f"{path}.releaseId")
        field_version = _decode_str(_require_field(mapping, "version", path), f"{path}.version")
        field_manifest_sha256 = _decode_str(_require_field(mapping, "manifestSha256", path), f"{path}.manifestSha256")
        field_payload_sha256 = _decode_str(_require_field(mapping, "payloadSha256", path), f"{path}.payloadSha256")
        field_publisher_id = _decode_str(_require_field(mapping, "publisherId", path), f"{path}.publisherId")
        field_signing_key_id = _decode_str(_require_field(mapping, "signingKeyId", path), f"{path}.signingKeyId")
        raw_target_entries = _decode_sequence(_require_field(mapping, "targetEntries", path), f"{path}.targetEntries")
        field_target_entries = tuple(TargetEntry.from_wire(entry, f"{path}.targetEntries[]") for entry in raw_target_entries)
        decoded = cls(
            app_id=field_app_id,
            release_id=field_release_id,
            version=field_version,
            manifest_sha256=field_manifest_sha256,
            payload_sha256=field_payload_sha256,
            publisher_id=field_publisher_id,
            signing_key_id=field_signing_key_id,
            target_entries=field_target_entries,
        )
        decoded.validate(path)
        return decoded


@dataclass(frozen=True, slots=True)
class PromotionRecord:
    """Evidence that promotion copied the same verified immutable Release Package. Promotion never
    rebuilds a package or injects environment data.
    """

    promotion_id: Identifier
    release_id: Identifier
    source_registry: Identifier
    target_registry: Identifier
    verified_manifest_sha256: Sha256
    environment_binding_id: Identifier
    promoted_at: str
    actor_ref: Identifier
    result: str
    audit_ref: Identifier

    #: Every canonical wire field spelling this record declares.
    WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset({"promotionId", "releaseId", "sourceRegistry", "targetRegistry", "verifiedManifestSha256", "environmentBindingId", "promotedAt", "actorRef", "result", "auditRef"})
    #: Whether the canonical schema refuses fields it does not declare.
    CLOSED: ClassVar[bool] = True

    def validate(self, path: str = "PromotionRecord") -> None:
        """Raise unless every field satisfies the canonical schema. Runtime values are checked, not
        annotations: a record built by calling this constructor with hostile values is refused
        here exactly as a hostile wire document is. No diagnostic reproduces a rejected value.
        """
        _check_str(self.promotion_id, f"{path}.promotionId", pattern=IDENTIFIER_PATTERN)
        _check_str(self.release_id, f"{path}.releaseId", pattern=IDENTIFIER_PATTERN)
        _check_str(self.source_registry, f"{path}.sourceRegistry", pattern=IDENTIFIER_PATTERN)
        _check_str(self.target_registry, f"{path}.targetRegistry", pattern=IDENTIFIER_PATTERN)
        _check_str(self.verified_manifest_sha256, f"{path}.verifiedManifestSha256", pattern=SHA256_PATTERN)
        _check_str(self.environment_binding_id, f"{path}.environmentBindingId", pattern=IDENTIFIER_PATTERN)
        _check_str(self.promoted_at, f"{path}.promotedAt", date_time=True)
        _check_str(self.actor_ref, f"{path}.actorRef", pattern=IDENTIFIER_PATTERN)
        _check_enum(self.result, PROMOTION_RESULTS, f"{path}.result")
        _check_str(self.audit_ref, f"{path}.auditRef", pattern=IDENTIFIER_PATTERN)

    def to_wire(self) -> dict[str, Any]:
        """Validate this value and render it as a JSON-compatible mapping using the canonical field
        spelling. An absent optional field is omitted rather than emitted as null, so a
        decode/encode round trip reproduces the original document exactly. Emission is validated
        so a record that was built rather than decoded cannot become an invalid wire document.
        """
        self.validate()
        wire: dict[str, Any] = {}
        wire["promotionId"] = self.promotion_id
        wire["releaseId"] = self.release_id
        wire["sourceRegistry"] = self.source_registry
        wire["targetRegistry"] = self.target_registry
        wire["verifiedManifestSha256"] = self.verified_manifest_sha256
        wire["environmentBindingId"] = self.environment_binding_id
        wire["promotedAt"] = self.promoted_at
        wire["actorRef"] = self.actor_ref
        wire["result"] = self.result
        wire["auditRef"] = self.audit_ref
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "PromotionRecord") -> PromotionRecord:
        """Decode a wire payload into a PromotionRecord. Unknown fields are refused: this record is
        closed by the canonical schema. The decoded value is then validated against every governed
        constraint, so this is the strict admission path rather than a structural one: a missing
        required field, a wrongly typed value, an unknown closed-vocabulary member, a pattern,
        length, range, timestamp, cardinality or uniqueness violation all raise
        HostContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        _reject_unknown_fields(mapping, cls.WIRE_FIELDS, path)
        field_promotion_id = _decode_str(_require_field(mapping, "promotionId", path), f"{path}.promotionId")
        field_release_id = _decode_str(_require_field(mapping, "releaseId", path), f"{path}.releaseId")
        field_source_registry = _decode_str(_require_field(mapping, "sourceRegistry", path), f"{path}.sourceRegistry")
        field_target_registry = _decode_str(_require_field(mapping, "targetRegistry", path), f"{path}.targetRegistry")
        field_verified_manifest_sha256 = _decode_str(_require_field(mapping, "verifiedManifestSha256", path), f"{path}.verifiedManifestSha256")
        field_environment_binding_id = _decode_str(_require_field(mapping, "environmentBindingId", path), f"{path}.environmentBindingId")
        field_promoted_at = _decode_str(_require_field(mapping, "promotedAt", path), f"{path}.promotedAt")
        field_actor_ref = _decode_str(_require_field(mapping, "actorRef", path), f"{path}.actorRef")
        field_result = _decode_enum(_require_field(mapping, "result", path), PROMOTION_RESULTS, f"{path}.result")
        field_audit_ref = _decode_str(_require_field(mapping, "auditRef", path), f"{path}.auditRef")
        decoded = cls(
            promotion_id=field_promotion_id,
            release_id=field_release_id,
            source_registry=field_source_registry,
            target_registry=field_target_registry,
            verified_manifest_sha256=field_verified_manifest_sha256,
            environment_binding_id=field_environment_binding_id,
            promoted_at=field_promoted_at,
            actor_ref=field_actor_ref,
            result=field_result,
            audit_ref=field_audit_ref,
        )
        decoded.validate(path)
        return decoded


@dataclass(frozen=True, slots=True)
class RollbackRecord:
    """Evidence that rollback selected a previously verified Release, mutating neither."""

    rollback_id: Identifier
    failed_promotion_id: Identifier
    restored_release_id: Identifier
    reason_code: Identifier
    performed_at: str
    actor_ref: Identifier
    audit_ref: Identifier

    #: Every canonical wire field spelling this record declares.
    WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset({"rollbackId", "failedPromotionId", "restoredReleaseId", "reasonCode", "performedAt", "actorRef", "auditRef"})
    #: Whether the canonical schema refuses fields it does not declare.
    CLOSED: ClassVar[bool] = True

    def validate(self, path: str = "RollbackRecord") -> None:
        """Raise unless every field satisfies the canonical schema. Runtime values are checked, not
        annotations: a record built by calling this constructor with hostile values is refused
        here exactly as a hostile wire document is. No diagnostic reproduces a rejected value.
        """
        _check_str(self.rollback_id, f"{path}.rollbackId", pattern=IDENTIFIER_PATTERN)
        _check_str(self.failed_promotion_id, f"{path}.failedPromotionId", pattern=IDENTIFIER_PATTERN)
        _check_str(self.restored_release_id, f"{path}.restoredReleaseId", pattern=IDENTIFIER_PATTERN)
        _check_str(self.reason_code, f"{path}.reasonCode", pattern=IDENTIFIER_PATTERN)
        _check_str(self.performed_at, f"{path}.performedAt", date_time=True)
        _check_str(self.actor_ref, f"{path}.actorRef", pattern=IDENTIFIER_PATTERN)
        _check_str(self.audit_ref, f"{path}.auditRef", pattern=IDENTIFIER_PATTERN)

    def to_wire(self) -> dict[str, Any]:
        """Validate this value and render it as a JSON-compatible mapping using the canonical field
        spelling. An absent optional field is omitted rather than emitted as null, so a
        decode/encode round trip reproduces the original document exactly. Emission is validated
        so a record that was built rather than decoded cannot become an invalid wire document.
        """
        self.validate()
        wire: dict[str, Any] = {}
        wire["rollbackId"] = self.rollback_id
        wire["failedPromotionId"] = self.failed_promotion_id
        wire["restoredReleaseId"] = self.restored_release_id
        wire["reasonCode"] = self.reason_code
        wire["performedAt"] = self.performed_at
        wire["actorRef"] = self.actor_ref
        wire["auditRef"] = self.audit_ref
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "RollbackRecord") -> RollbackRecord:
        """Decode a wire payload into a RollbackRecord. Unknown fields are refused: this record is
        closed by the canonical schema. The decoded value is then validated against every governed
        constraint, so this is the strict admission path rather than a structural one: a missing
        required field, a wrongly typed value, an unknown closed-vocabulary member, a pattern,
        length, range, timestamp, cardinality or uniqueness violation all raise
        HostContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        _reject_unknown_fields(mapping, cls.WIRE_FIELDS, path)
        field_rollback_id = _decode_str(_require_field(mapping, "rollbackId", path), f"{path}.rollbackId")
        field_failed_promotion_id = _decode_str(_require_field(mapping, "failedPromotionId", path), f"{path}.failedPromotionId")
        field_restored_release_id = _decode_str(_require_field(mapping, "restoredReleaseId", path), f"{path}.restoredReleaseId")
        field_reason_code = _decode_str(_require_field(mapping, "reasonCode", path), f"{path}.reasonCode")
        field_performed_at = _decode_str(_require_field(mapping, "performedAt", path), f"{path}.performedAt")
        field_actor_ref = _decode_str(_require_field(mapping, "actorRef", path), f"{path}.actorRef")
        field_audit_ref = _decode_str(_require_field(mapping, "auditRef", path), f"{path}.auditRef")
        decoded = cls(
            rollback_id=field_rollback_id,
            failed_promotion_id=field_failed_promotion_id,
            restored_release_id=field_restored_release_id,
            reason_code=field_reason_code,
            performed_at=field_performed_at,
            actor_ref=field_actor_ref,
            audit_ref=field_audit_ref,
        )
        decoded.validate(path)
        return decoded


@dataclass(frozen=True, slots=True)
class EnvironmentBinding:
    """Workspace-scoped, separately versioned references that bind a Release to an environment.
    References only: never a raw credential, token, private key or provider secret.
    """

    binding_id: Identifier
    binding_version: int
    release_id: Identifier
    environment_id: Identifier
    workspace_id: Identifier
    target: Target
    credential_refs: LogicalReferences
    integration_refs: LogicalReferences
    storage_refs: LogicalReferences
    domain_refs: LogicalReferences
    created_at: str
    actor_ref: Identifier
    data_residency_policy_ref: Identifier | None = None
    runtime_limits_ref: Identifier | None = None
    execution_placement_ref: Identifier | None = None

    #: Every canonical wire field spelling this record declares.
    WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset({"bindingId", "bindingVersion", "releaseId", "environmentId", "workspaceId", "target", "credentialRefs", "integrationRefs", "storageRefs", "domainRefs", "createdAt", "actorRef", "dataResidencyPolicyRef", "runtimeLimitsRef", "executionPlacementRef"})
    #: Whether the canonical schema refuses fields it does not declare.
    CLOSED: ClassVar[bool] = True

    def validate(self, path: str = "EnvironmentBinding") -> None:
        """Raise unless every field satisfies the canonical schema. Runtime values are checked, not
        annotations: a record built by calling this constructor with hostile values is refused
        here exactly as a hostile wire document is. No diagnostic reproduces a rejected value.
        """
        _check_str(self.binding_id, f"{path}.bindingId", pattern=IDENTIFIER_PATTERN)
        _check_int(self.binding_version, f"{path}.bindingVersion", minimum=1)
        _check_str(self.release_id, f"{path}.releaseId", pattern=IDENTIFIER_PATTERN)
        _check_str(self.environment_id, f"{path}.environmentId", pattern=IDENTIFIER_PATTERN)
        _check_str(self.workspace_id, f"{path}.workspaceId", pattern=IDENTIFIER_PATTERN)
        _check_enum(self.target, TARGETS, f"{path}.target")
        _check_sequence(self.credential_refs, f"{path}.credentialRefs", unique=True)
        for index, entry_credential_refs in enumerate(self.credential_refs):
            _check_str(entry_credential_refs, f"{path}.credentialRefs[{index}]", pattern=IDENTIFIER_PATTERN)
        _check_sequence(self.integration_refs, f"{path}.integrationRefs", unique=True)
        for index, entry_integration_refs in enumerate(self.integration_refs):
            _check_str(entry_integration_refs, f"{path}.integrationRefs[{index}]", pattern=IDENTIFIER_PATTERN)
        _check_sequence(self.storage_refs, f"{path}.storageRefs", unique=True)
        for index, entry_storage_refs in enumerate(self.storage_refs):
            _check_str(entry_storage_refs, f"{path}.storageRefs[{index}]", pattern=IDENTIFIER_PATTERN)
        _check_sequence(self.domain_refs, f"{path}.domainRefs", unique=True)
        for index, entry_domain_refs in enumerate(self.domain_refs):
            _check_str(entry_domain_refs, f"{path}.domainRefs[{index}]", pattern=IDENTIFIER_PATTERN)
        _check_str(self.created_at, f"{path}.createdAt", date_time=True)
        _check_str(self.actor_ref, f"{path}.actorRef", pattern=IDENTIFIER_PATTERN)
        if self.data_residency_policy_ref is not None:
            _check_str(self.data_residency_policy_ref, f"{path}.dataResidencyPolicyRef", pattern=IDENTIFIER_PATTERN)
        if self.runtime_limits_ref is not None:
            _check_str(self.runtime_limits_ref, f"{path}.runtimeLimitsRef", pattern=IDENTIFIER_PATTERN)
        if self.execution_placement_ref is not None:
            _check_str(self.execution_placement_ref, f"{path}.executionPlacementRef", pattern=IDENTIFIER_PATTERN)

    def to_wire(self) -> dict[str, Any]:
        """Validate this value and render it as a JSON-compatible mapping using the canonical field
        spelling. An absent optional field is omitted rather than emitted as null, so a
        decode/encode round trip reproduces the original document exactly. Emission is validated
        so a record that was built rather than decoded cannot become an invalid wire document.
        """
        self.validate()
        wire: dict[str, Any] = {}
        wire["bindingId"] = self.binding_id
        wire["bindingVersion"] = self.binding_version
        wire["releaseId"] = self.release_id
        wire["environmentId"] = self.environment_id
        wire["workspaceId"] = self.workspace_id
        wire["target"] = self.target
        wire["credentialRefs"] = list(self.credential_refs)
        wire["integrationRefs"] = list(self.integration_refs)
        wire["storageRefs"] = list(self.storage_refs)
        wire["domainRefs"] = list(self.domain_refs)
        wire["createdAt"] = self.created_at
        wire["actorRef"] = self.actor_ref
        if self.data_residency_policy_ref is not None:
            wire["dataResidencyPolicyRef"] = self.data_residency_policy_ref
        if self.runtime_limits_ref is not None:
            wire["runtimeLimitsRef"] = self.runtime_limits_ref
        if self.execution_placement_ref is not None:
            wire["executionPlacementRef"] = self.execution_placement_ref
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "EnvironmentBinding") -> EnvironmentBinding:
        """Decode a wire payload into a EnvironmentBinding. Unknown fields are refused: this record
        is closed by the canonical schema. The decoded value is then validated against every
        governed constraint, so this is the strict admission path rather than a structural one: a
        missing required field, a wrongly typed value, an unknown closed-vocabulary member, a
        pattern, length, range, timestamp, cardinality or uniqueness violation all raise
        HostContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        _reject_unknown_fields(mapping, cls.WIRE_FIELDS, path)
        field_binding_id = _decode_str(_require_field(mapping, "bindingId", path), f"{path}.bindingId")
        field_binding_version = _decode_int(_require_field(mapping, "bindingVersion", path), f"{path}.bindingVersion")
        field_release_id = _decode_str(_require_field(mapping, "releaseId", path), f"{path}.releaseId")
        field_environment_id = _decode_str(_require_field(mapping, "environmentId", path), f"{path}.environmentId")
        field_workspace_id = _decode_str(_require_field(mapping, "workspaceId", path), f"{path}.workspaceId")
        field_target = _decode_enum(_require_field(mapping, "target", path), TARGETS, f"{path}.target")
        raw_credential_refs = _decode_sequence(_require_field(mapping, "credentialRefs", path), f"{path}.credentialRefs")
        field_credential_refs = tuple(_decode_str(entry, f"{path}.credentialRefs[]") for entry in raw_credential_refs)
        raw_integration_refs = _decode_sequence(_require_field(mapping, "integrationRefs", path), f"{path}.integrationRefs")
        field_integration_refs = tuple(_decode_str(entry, f"{path}.integrationRefs[]") for entry in raw_integration_refs)
        raw_storage_refs = _decode_sequence(_require_field(mapping, "storageRefs", path), f"{path}.storageRefs")
        field_storage_refs = tuple(_decode_str(entry, f"{path}.storageRefs[]") for entry in raw_storage_refs)
        raw_domain_refs = _decode_sequence(_require_field(mapping, "domainRefs", path), f"{path}.domainRefs")
        field_domain_refs = tuple(_decode_str(entry, f"{path}.domainRefs[]") for entry in raw_domain_refs)
        field_created_at = _decode_str(_require_field(mapping, "createdAt", path), f"{path}.createdAt")
        field_actor_ref = _decode_str(_require_field(mapping, "actorRef", path), f"{path}.actorRef")
        field_data_residency_policy_ref = None
        if "dataResidencyPolicyRef" in mapping:
            field_data_residency_policy_ref = _decode_str(mapping["dataResidencyPolicyRef"], f"{path}.dataResidencyPolicyRef")
        field_runtime_limits_ref = None
        if "runtimeLimitsRef" in mapping:
            field_runtime_limits_ref = _decode_str(mapping["runtimeLimitsRef"], f"{path}.runtimeLimitsRef")
        field_execution_placement_ref = None
        if "executionPlacementRef" in mapping:
            field_execution_placement_ref = _decode_str(mapping["executionPlacementRef"], f"{path}.executionPlacementRef")
        decoded = cls(
            binding_id=field_binding_id,
            binding_version=field_binding_version,
            release_id=field_release_id,
            environment_id=field_environment_id,
            workspace_id=field_workspace_id,
            target=field_target,
            credential_refs=field_credential_refs,
            integration_refs=field_integration_refs,
            storage_refs=field_storage_refs,
            domain_refs=field_domain_refs,
            created_at=field_created_at,
            actor_ref=field_actor_ref,
            data_residency_policy_ref=field_data_residency_policy_ref,
            runtime_limits_ref=field_runtime_limits_ref,
            execution_placement_ref=field_execution_placement_ref,
        )
        decoded.validate(path)
        return decoded


@dataclass(frozen=True, slots=True)
class SourceMount:
    """One development source mount. Compiled out of production customer builds."""

    module_id: Identifier
    path_ref: Identifier

    #: Every canonical wire field spelling this record declares.
    WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset({"moduleId", "pathRef"})
    #: Whether the canonical schema refuses fields it does not declare.
    CLOSED: ClassVar[bool] = True

    def validate(self, path: str = "SourceMount") -> None:
        """Raise unless every field satisfies the canonical schema. Runtime values are checked, not
        annotations: a record built by calling this constructor with hostile values is refused
        here exactly as a hostile wire document is. No diagnostic reproduces a rejected value.
        """
        _check_str(self.module_id, f"{path}.moduleId", pattern=IDENTIFIER_PATTERN)
        _check_str(self.path_ref, f"{path}.pathRef", pattern=IDENTIFIER_PATTERN)

    def to_wire(self) -> dict[str, Any]:
        """Validate this value and render it as a JSON-compatible mapping using the canonical field
        spelling. An absent optional field is omitted rather than emitted as null, so a
        decode/encode round trip reproduces the original document exactly. Emission is validated
        so a record that was built rather than decoded cannot become an invalid wire document.
        """
        self.validate()
        wire: dict[str, Any] = {}
        wire["moduleId"] = self.module_id
        wire["pathRef"] = self.path_ref
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "SourceMount") -> SourceMount:
        """Decode a wire payload into a SourceMount. Unknown fields are refused: this record is
        closed by the canonical schema. The decoded value is then validated against every governed
        constraint, so this is the strict admission path rather than a structural one: a missing
        required field, a wrongly typed value, an unknown closed-vocabulary member, a pattern,
        length, range, timestamp, cardinality or uniqueness violation all raise
        HostContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        _reject_unknown_fields(mapping, cls.WIRE_FIELDS, path)
        field_module_id = _decode_str(_require_field(mapping, "moduleId", path), f"{path}.moduleId")
        field_path_ref = _decode_str(_require_field(mapping, "pathRef", path), f"{path}.pathRef")
        decoded = cls(
            module_id=field_module_id,
            path_ref=field_path_ref,
        )
        decoded.validate(path)
        return decoded


@dataclass(frozen=True, slots=True)
class DevelopmentProfile:
    """A local development configuration. Its target is restricted to `desktop` or `conformance`: a
    production Cloud target is not expressible.
    """

    profile_version: ContractVersion
    profile_id: Identifier
    source_mounts: tuple[SourceMount, ...]
    target: str
    initial_route: Identifier
    scenario_id: Identifier
    workspace_fixture_id: Identifier
    declared_capabilities: LogicalReferences
    isolation_id: Identifier
    ports: tuple[int, ...]
    user_data_directory_ref: Identifier

    #: Every canonical wire field spelling this record declares.
    WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset({"profileVersion", "profileId", "sourceMounts", "target", "initialRoute", "scenarioId", "workspaceFixtureId", "declaredCapabilities", "isolationId", "ports", "userDataDirectoryRef"})
    #: Whether the canonical schema refuses fields it does not declare.
    CLOSED: ClassVar[bool] = True

    def validate(self, path: str = "DevelopmentProfile") -> None:
        """Raise unless every field satisfies the canonical schema. Runtime values are checked, not
        annotations: a record built by calling this constructor with hostile values is refused
        here exactly as a hostile wire document is. No diagnostic reproduces a rejected value.
        """
        _check_str(self.profile_version, f"{path}.profileVersion", pattern=CONTRACT_VERSION_PATTERN)
        _check_str(self.profile_id, f"{path}.profileId", pattern=IDENTIFIER_PATTERN)
        _check_sequence(self.source_mounts, f"{path}.sourceMounts")
        for index, entry_source_mounts in enumerate(self.source_mounts):
            _check_record(entry_source_mounts, SourceMount, f"{path}.sourceMounts[{index}]")
        _check_enum(self.target, DEVELOPMENT_TARGETS, f"{path}.target")
        _check_str(self.initial_route, f"{path}.initialRoute", pattern=IDENTIFIER_PATTERN)
        _check_str(self.scenario_id, f"{path}.scenarioId", pattern=IDENTIFIER_PATTERN)
        _check_str(self.workspace_fixture_id, f"{path}.workspaceFixtureId", pattern=IDENTIFIER_PATTERN)
        _check_sequence(self.declared_capabilities, f"{path}.declaredCapabilities", unique=True)
        for index, entry_declared_capabilities in enumerate(self.declared_capabilities):
            _check_str(entry_declared_capabilities, f"{path}.declaredCapabilities[{index}]", pattern=IDENTIFIER_PATTERN)
        _check_str(self.isolation_id, f"{path}.isolationId", pattern=IDENTIFIER_PATTERN)
        _check_sequence(self.ports, f"{path}.ports", unique=True)
        for index, entry_ports in enumerate(self.ports):
            _check_int(entry_ports, f"{path}.ports[{index}]", minimum=1, maximum=65535)
        _check_str(self.user_data_directory_ref, f"{path}.userDataDirectoryRef", pattern=IDENTIFIER_PATTERN)

    def to_wire(self) -> dict[str, Any]:
        """Validate this value and render it as a JSON-compatible mapping using the canonical field
        spelling. An absent optional field is omitted rather than emitted as null, so a
        decode/encode round trip reproduces the original document exactly. Emission is validated
        so a record that was built rather than decoded cannot become an invalid wire document.
        """
        self.validate()
        wire: dict[str, Any] = {}
        wire["profileVersion"] = self.profile_version
        wire["profileId"] = self.profile_id
        wire["sourceMounts"] = [item.to_wire() for item in self.source_mounts]
        wire["target"] = self.target
        wire["initialRoute"] = self.initial_route
        wire["scenarioId"] = self.scenario_id
        wire["workspaceFixtureId"] = self.workspace_fixture_id
        wire["declaredCapabilities"] = list(self.declared_capabilities)
        wire["isolationId"] = self.isolation_id
        wire["ports"] = list(self.ports)
        wire["userDataDirectoryRef"] = self.user_data_directory_ref
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "DevelopmentProfile") -> DevelopmentProfile:
        """Decode a wire payload into a DevelopmentProfile. Unknown fields are refused: this record
        is closed by the canonical schema. The decoded value is then validated against every
        governed constraint, so this is the strict admission path rather than a structural one: a
        missing required field, a wrongly typed value, an unknown closed-vocabulary member, a
        pattern, length, range, timestamp, cardinality or uniqueness violation all raise
        HostContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        _reject_unknown_fields(mapping, cls.WIRE_FIELDS, path)
        field_profile_version = _decode_str(_require_field(mapping, "profileVersion", path), f"{path}.profileVersion")
        field_profile_id = _decode_str(_require_field(mapping, "profileId", path), f"{path}.profileId")
        raw_source_mounts = _decode_sequence(_require_field(mapping, "sourceMounts", path), f"{path}.sourceMounts")
        field_source_mounts = tuple(SourceMount.from_wire(entry, f"{path}.sourceMounts[]") for entry in raw_source_mounts)
        field_target = _decode_enum(_require_field(mapping, "target", path), DEVELOPMENT_TARGETS, f"{path}.target")
        field_initial_route = _decode_str(_require_field(mapping, "initialRoute", path), f"{path}.initialRoute")
        field_scenario_id = _decode_str(_require_field(mapping, "scenarioId", path), f"{path}.scenarioId")
        field_workspace_fixture_id = _decode_str(_require_field(mapping, "workspaceFixtureId", path), f"{path}.workspaceFixtureId")
        raw_declared_capabilities = _decode_sequence(_require_field(mapping, "declaredCapabilities", path), f"{path}.declaredCapabilities")
        field_declared_capabilities = tuple(_decode_str(entry, f"{path}.declaredCapabilities[]") for entry in raw_declared_capabilities)
        field_isolation_id = _decode_str(_require_field(mapping, "isolationId", path), f"{path}.isolationId")
        raw_ports = _decode_sequence(_require_field(mapping, "ports", path), f"{path}.ports")
        field_ports = tuple(_decode_int(entry, f"{path}.ports[]") for entry in raw_ports)
        field_user_data_directory_ref = _decode_str(_require_field(mapping, "userDataDirectoryRef", path), f"{path}.userDataDirectoryRef")
        decoded = cls(
            profile_version=field_profile_version,
            profile_id=field_profile_id,
            source_mounts=field_source_mounts,
            target=field_target,
            initial_route=field_initial_route,
            scenario_id=field_scenario_id,
            workspace_fixture_id=field_workspace_fixture_id,
            declared_capabilities=field_declared_capabilities,
            isolation_id=field_isolation_id,
            ports=field_ports,
            user_data_directory_ref=field_user_data_directory_ref,
        )
        decoded.validate(path)
        return decoded


@dataclass(frozen=True, slots=True)
class ScenarioAssertion:
    """One deterministic assertion a shell scenario makes."""

    kind: Identifier
    target_id: Identifier

    #: Every canonical wire field spelling this record declares.
    WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset({"kind", "targetId"})
    #: Whether the canonical schema refuses fields it does not declare.
    CLOSED: ClassVar[bool] = True

    def validate(self, path: str = "ScenarioAssertion") -> None:
        """Raise unless every field satisfies the canonical schema. Runtime values are checked, not
        annotations: a record built by calling this constructor with hostile values is refused
        here exactly as a hostile wire document is. No diagnostic reproduces a rejected value.
        """
        _check_str(self.kind, f"{path}.kind", pattern=IDENTIFIER_PATTERN)
        _check_str(self.target_id, f"{path}.targetId", pattern=IDENTIFIER_PATTERN)

    def to_wire(self) -> dict[str, Any]:
        """Validate this value and render it as a JSON-compatible mapping using the canonical field
        spelling. An absent optional field is omitted rather than emitted as null, so a
        decode/encode round trip reproduces the original document exactly. Emission is validated
        so a record that was built rather than decoded cannot become an invalid wire document.
        """
        self.validate()
        wire: dict[str, Any] = {}
        wire["kind"] = self.kind
        wire["targetId"] = self.target_id
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "ScenarioAssertion") -> ScenarioAssertion:
        """Decode a wire payload into a ScenarioAssertion. Unknown fields are refused: this record is
        closed by the canonical schema. The decoded value is then validated against every governed
        constraint, so this is the strict admission path rather than a structural one: a missing
        required field, a wrongly typed value, an unknown closed-vocabulary member, a pattern,
        length, range, timestamp, cardinality or uniqueness violation all raise
        HostContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        _reject_unknown_fields(mapping, cls.WIRE_FIELDS, path)
        field_kind = _decode_str(_require_field(mapping, "kind", path), f"{path}.kind")
        field_target_id = _decode_str(_require_field(mapping, "targetId", path), f"{path}.targetId")
        decoded = cls(
            kind=field_kind,
            target_id=field_target_id,
        )
        decoded.validate(path)
        return decoded


@dataclass(frozen=True, slots=True)
class ShellScenario:
    """A deterministic fixture-driven scenario for conformance runs."""

    scenario_version: ContractVersion
    scenario_id: Identifier
    principal_fixture_id: Identifier
    workspace_fixture_id: Identifier
    module_fixture_ids: LogicalReferences
    app_fixture_ids: LogicalReferences
    permission_fixture_id: Identifier
    health_fixture_id: Identifier
    data_fixture_ids: LogicalReferences
    assertions: tuple[ScenarioAssertion, ...]

    #: Every canonical wire field spelling this record declares.
    WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset({"scenarioVersion", "scenarioId", "principalFixtureId", "workspaceFixtureId", "moduleFixtureIds", "appFixtureIds", "permissionFixtureId", "healthFixtureId", "dataFixtureIds", "assertions"})
    #: Whether the canonical schema refuses fields it does not declare.
    CLOSED: ClassVar[bool] = True

    def validate(self, path: str = "ShellScenario") -> None:
        """Raise unless every field satisfies the canonical schema. Runtime values are checked, not
        annotations: a record built by calling this constructor with hostile values is refused
        here exactly as a hostile wire document is. No diagnostic reproduces a rejected value.
        """
        _check_str(self.scenario_version, f"{path}.scenarioVersion", pattern=CONTRACT_VERSION_PATTERN)
        _check_str(self.scenario_id, f"{path}.scenarioId", pattern=IDENTIFIER_PATTERN)
        _check_str(self.principal_fixture_id, f"{path}.principalFixtureId", pattern=IDENTIFIER_PATTERN)
        _check_str(self.workspace_fixture_id, f"{path}.workspaceFixtureId", pattern=IDENTIFIER_PATTERN)
        _check_sequence(self.module_fixture_ids, f"{path}.moduleFixtureIds", unique=True)
        for index, entry_module_fixture_ids in enumerate(self.module_fixture_ids):
            _check_str(entry_module_fixture_ids, f"{path}.moduleFixtureIds[{index}]", pattern=IDENTIFIER_PATTERN)
        _check_sequence(self.app_fixture_ids, f"{path}.appFixtureIds", unique=True)
        for index, entry_app_fixture_ids in enumerate(self.app_fixture_ids):
            _check_str(entry_app_fixture_ids, f"{path}.appFixtureIds[{index}]", pattern=IDENTIFIER_PATTERN)
        _check_str(self.permission_fixture_id, f"{path}.permissionFixtureId", pattern=IDENTIFIER_PATTERN)
        _check_str(self.health_fixture_id, f"{path}.healthFixtureId", pattern=IDENTIFIER_PATTERN)
        _check_sequence(self.data_fixture_ids, f"{path}.dataFixtureIds", unique=True)
        for index, entry_data_fixture_ids in enumerate(self.data_fixture_ids):
            _check_str(entry_data_fixture_ids, f"{path}.dataFixtureIds[{index}]", pattern=IDENTIFIER_PATTERN)
        _check_sequence(self.assertions, f"{path}.assertions")
        for index, entry_assertions in enumerate(self.assertions):
            _check_record(entry_assertions, ScenarioAssertion, f"{path}.assertions[{index}]")

    def to_wire(self) -> dict[str, Any]:
        """Validate this value and render it as a JSON-compatible mapping using the canonical field
        spelling. An absent optional field is omitted rather than emitted as null, so a
        decode/encode round trip reproduces the original document exactly. Emission is validated
        so a record that was built rather than decoded cannot become an invalid wire document.
        """
        self.validate()
        wire: dict[str, Any] = {}
        wire["scenarioVersion"] = self.scenario_version
        wire["scenarioId"] = self.scenario_id
        wire["principalFixtureId"] = self.principal_fixture_id
        wire["workspaceFixtureId"] = self.workspace_fixture_id
        wire["moduleFixtureIds"] = list(self.module_fixture_ids)
        wire["appFixtureIds"] = list(self.app_fixture_ids)
        wire["permissionFixtureId"] = self.permission_fixture_id
        wire["healthFixtureId"] = self.health_fixture_id
        wire["dataFixtureIds"] = list(self.data_fixture_ids)
        wire["assertions"] = [item.to_wire() for item in self.assertions]
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "ShellScenario") -> ShellScenario:
        """Decode a wire payload into a ShellScenario. Unknown fields are refused: this record is
        closed by the canonical schema. The decoded value is then validated against every governed
        constraint, so this is the strict admission path rather than a structural one: a missing
        required field, a wrongly typed value, an unknown closed-vocabulary member, a pattern,
        length, range, timestamp, cardinality or uniqueness violation all raise
        HostContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        _reject_unknown_fields(mapping, cls.WIRE_FIELDS, path)
        field_scenario_version = _decode_str(_require_field(mapping, "scenarioVersion", path), f"{path}.scenarioVersion")
        field_scenario_id = _decode_str(_require_field(mapping, "scenarioId", path), f"{path}.scenarioId")
        field_principal_fixture_id = _decode_str(_require_field(mapping, "principalFixtureId", path), f"{path}.principalFixtureId")
        field_workspace_fixture_id = _decode_str(_require_field(mapping, "workspaceFixtureId", path), f"{path}.workspaceFixtureId")
        raw_module_fixture_ids = _decode_sequence(_require_field(mapping, "moduleFixtureIds", path), f"{path}.moduleFixtureIds")
        field_module_fixture_ids = tuple(_decode_str(entry, f"{path}.moduleFixtureIds[]") for entry in raw_module_fixture_ids)
        raw_app_fixture_ids = _decode_sequence(_require_field(mapping, "appFixtureIds", path), f"{path}.appFixtureIds")
        field_app_fixture_ids = tuple(_decode_str(entry, f"{path}.appFixtureIds[]") for entry in raw_app_fixture_ids)
        field_permission_fixture_id = _decode_str(_require_field(mapping, "permissionFixtureId", path), f"{path}.permissionFixtureId")
        field_health_fixture_id = _decode_str(_require_field(mapping, "healthFixtureId", path), f"{path}.healthFixtureId")
        raw_data_fixture_ids = _decode_sequence(_require_field(mapping, "dataFixtureIds", path), f"{path}.dataFixtureIds")
        field_data_fixture_ids = tuple(_decode_str(entry, f"{path}.dataFixtureIds[]") for entry in raw_data_fixture_ids)
        raw_assertions = _decode_sequence(_require_field(mapping, "assertions", path), f"{path}.assertions")
        field_assertions = tuple(ScenarioAssertion.from_wire(entry, f"{path}.assertions[]") for entry in raw_assertions)
        decoded = cls(
            scenario_version=field_scenario_version,
            scenario_id=field_scenario_id,
            principal_fixture_id=field_principal_fixture_id,
            workspace_fixture_id=field_workspace_fixture_id,
            module_fixture_ids=field_module_fixture_ids,
            app_fixture_ids=field_app_fixture_ids,
            permission_fixture_id=field_permission_fixture_id,
            health_fixture_id=field_health_fixture_id,
            data_fixture_ids=field_data_fixture_ids,
            assertions=field_assertions,
        )
        decoded.validate(path)
        return decoded


@dataclass(frozen=True, slots=True)
class TestDriverRequest:
    """One conformance test-driver instruction. Absent from production customer builds."""

    driver_version: ContractVersion
    request_id: Identifier
    operation: str
    scenario_id: Identifier
    payload: JsonObject

    #: Every canonical wire field spelling this record declares.
    WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset({"driverVersion", "requestId", "operation", "scenarioId", "payload"})
    #: Whether the canonical schema refuses fields it does not declare.
    CLOSED: ClassVar[bool] = True

    def validate(self, path: str = "TestDriverRequest") -> None:
        """Raise unless every field satisfies the canonical schema. Runtime values are checked, not
        annotations: a record built by calling this constructor with hostile values is refused
        here exactly as a hostile wire document is. No diagnostic reproduces a rejected value.
        """
        _check_str(self.driver_version, f"{path}.driverVersion", pattern=CONTRACT_VERSION_PATTERN)
        _check_str(self.request_id, f"{path}.requestId", pattern=IDENTIFIER_PATTERN)
        _check_enum(self.operation, TEST_DRIVER_OPERATIONS, f"{path}.operation")
        _check_str(self.scenario_id, f"{path}.scenarioId", pattern=IDENTIFIER_PATTERN)
        _check_json_object(self.payload, f"{path}.payload")

    def to_wire(self) -> dict[str, Any]:
        """Validate this value and render it as a JSON-compatible mapping using the canonical field
        spelling. An absent optional field is omitted rather than emitted as null, so a
        decode/encode round trip reproduces the original document exactly. Emission is validated
        so a record that was built rather than decoded cannot become an invalid wire document.
        """
        self.validate()
        wire: dict[str, Any] = {}
        wire["driverVersion"] = self.driver_version
        wire["requestId"] = self.request_id
        wire["operation"] = self.operation
        wire["scenarioId"] = self.scenario_id
        wire["payload"] = _encode_json_object(self.payload)
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "TestDriverRequest") -> TestDriverRequest:
        """Decode a wire payload into a TestDriverRequest. Unknown fields are refused: this record is
        closed by the canonical schema. The decoded value is then validated against every governed
        constraint, so this is the strict admission path rather than a structural one: a missing
        required field, a wrongly typed value, an unknown closed-vocabulary member, a pattern,
        length, range, timestamp, cardinality or uniqueness violation all raise
        HostContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        _reject_unknown_fields(mapping, cls.WIRE_FIELDS, path)
        field_driver_version = _decode_str(_require_field(mapping, "driverVersion", path), f"{path}.driverVersion")
        field_request_id = _decode_str(_require_field(mapping, "requestId", path), f"{path}.requestId")
        field_operation = _decode_enum(_require_field(mapping, "operation", path), TEST_DRIVER_OPERATIONS, f"{path}.operation")
        field_scenario_id = _decode_str(_require_field(mapping, "scenarioId", path), f"{path}.scenarioId")
        field_payload = _decode_json_object(_require_field(mapping, "payload", path), f"{path}.payload")
        decoded = cls(
            driver_version=field_driver_version,
            request_id=field_request_id,
            operation=field_operation,
            scenario_id=field_scenario_id,
            payload=field_payload,
        )
        decoded.validate(path)
        return decoded


@dataclass(frozen=True, slots=True)
class TestDriverResult:
    """The outcome of one conformance test-driver instruction."""

    driver_version: ContractVersion
    request_id: Identifier
    outcome: str
    evidence_refs: LogicalReferences
    correlation_id: Identifier
    safe_reason: str | None = None

    #: Every canonical wire field spelling this record declares.
    WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset({"driverVersion", "requestId", "outcome", "evidenceRefs", "correlationId", "safeReason"})
    #: Whether the canonical schema refuses fields it does not declare.
    CLOSED: ClassVar[bool] = True

    def validate(self, path: str = "TestDriverResult") -> None:
        """Raise unless every field satisfies the canonical schema. Runtime values are checked, not
        annotations: a record built by calling this constructor with hostile values is refused
        here exactly as a hostile wire document is. No diagnostic reproduces a rejected value.
        """
        _check_str(self.driver_version, f"{path}.driverVersion", pattern=CONTRACT_VERSION_PATTERN)
        _check_str(self.request_id, f"{path}.requestId", pattern=IDENTIFIER_PATTERN)
        _check_enum(self.outcome, TEST_DRIVER_OUTCOMES, f"{path}.outcome")
        _check_sequence(self.evidence_refs, f"{path}.evidenceRefs", unique=True)
        for index, entry_evidence_refs in enumerate(self.evidence_refs):
            _check_str(entry_evidence_refs, f"{path}.evidenceRefs[{index}]", pattern=IDENTIFIER_PATTERN)
        _check_str(self.correlation_id, f"{path}.correlationId", pattern=IDENTIFIER_PATTERN)
        if self.safe_reason is not None:
            _check_str(self.safe_reason, f"{path}.safeReason", min_length=1, max_length=500)

    def to_wire(self) -> dict[str, Any]:
        """Validate this value and render it as a JSON-compatible mapping using the canonical field
        spelling. An absent optional field is omitted rather than emitted as null, so a
        decode/encode round trip reproduces the original document exactly. Emission is validated
        so a record that was built rather than decoded cannot become an invalid wire document.
        """
        self.validate()
        wire: dict[str, Any] = {}
        wire["driverVersion"] = self.driver_version
        wire["requestId"] = self.request_id
        wire["outcome"] = self.outcome
        wire["evidenceRefs"] = list(self.evidence_refs)
        wire["correlationId"] = self.correlation_id
        if self.safe_reason is not None:
            wire["safeReason"] = self.safe_reason
        return wire

    @classmethod
    def from_wire(cls, payload: object, path: str = "TestDriverResult") -> TestDriverResult:
        """Decode a wire payload into a TestDriverResult. Unknown fields are refused: this record is
        closed by the canonical schema. The decoded value is then validated against every governed
        constraint, so this is the strict admission path rather than a structural one: a missing
        required field, a wrongly typed value, an unknown closed-vocabulary member, a pattern,
        length, range, timestamp, cardinality or uniqueness violation all raise
        HostContractDecodeError.
        """
        mapping = _require_mapping(payload, path)
        _reject_unknown_fields(mapping, cls.WIRE_FIELDS, path)
        field_driver_version = _decode_str(_require_field(mapping, "driverVersion", path), f"{path}.driverVersion")
        field_request_id = _decode_str(_require_field(mapping, "requestId", path), f"{path}.requestId")
        field_outcome = _decode_enum(_require_field(mapping, "outcome", path), TEST_DRIVER_OUTCOMES, f"{path}.outcome")
        raw_evidence_refs = _decode_sequence(_require_field(mapping, "evidenceRefs", path), f"{path}.evidenceRefs")
        field_evidence_refs = tuple(_decode_str(entry, f"{path}.evidenceRefs[]") for entry in raw_evidence_refs)
        field_correlation_id = _decode_str(_require_field(mapping, "correlationId", path), f"{path}.correlationId")
        field_safe_reason = None
        if "safeReason" in mapping:
            field_safe_reason = _decode_str(mapping["safeReason"], f"{path}.safeReason")
        decoded = cls(
            driver_version=field_driver_version,
            request_id=field_request_id,
            outcome=field_outcome,
            evidence_refs=field_evidence_refs,
            correlation_id=field_correlation_id,
            safe_reason=field_safe_reason,
        )
        decoded.validate(path)
        return decoded
