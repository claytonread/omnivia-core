"""Semantic validation for `CoreTargetV1` and `CoreSafeStatusV1` (ADR-038).

The generated dataclasses decode every field tolerantly: a closed vocabulary
(`kind`, `management`, the four normalized status states, `warning_codes`,
`permitted_actions`) arrives as a plain string or tuple of strings, and a
patterned or bounded scalar (`contract_version`, `target_ref`,
`endpoint_profile_ref`, `workspace_ref`, `display_name`, `server_version`,
`protocol_version`) arrives as whatever string the wire carried. Enforcing
either is JSON Schema's job, not the generated decoder's.

This module is where both halves are actually enforced on the production
decode/encode path: the declared closed domains *and* the declared scalar
bounds -- pattern, length, and array size -- each checked with the generated
guard for that value domain rather than a restated regex. It also carries the
cross-field invariants the schema cannot express on its own:

* a target's process-lifecycle actions (`start`/`stop`/`restart`) are only ever
  safe to offer when this client both owns the process (`locally_managed`) and
  the process is on this host (`local`) -- a remote or externally-managed target
  must never advertise them, because the caller has no authority to act on
  someone else's process;
* a status is published at the same `contract_version` as the target it
  describes;
* across a *set* of targets, no `target_ref` and no `workspace_ref` repeats --
  the handoff rule that two different target authorities may not reuse one
  writable workspace identity, which no single descriptor can enforce.

Standard library only. Nothing here may depend on runtime, storage, HTTP, MCP,
CLI, Platform, Dev, or a validation framework.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.generated import (
    CoreSafeStatusV1,
    CoreTargetV1,
    is_contract_version,
    is_identifier,
    is_release_version,
    is_workspace_id,
)

__all__ = [
    "decode_core_safe_status",
    "decode_core_target",
    "encode_core_safe_status",
    "encode_core_target",
    "validate_core_safe_status",
    "validate_core_target",
    "validate_core_target_authorities",
]

_TARGET_KINDS: Final = frozenset({"local", "private_remote", "cloud"})
_TARGET_MANAGEMENTS: Final = frozenset({"locally_managed", "externally_managed"})
_LIFECYCLE_STATES: Final = frozenset(
    {"starting", "running", "stopping", "stopped", "failed", "unknown"}
)
_READINESS_STATES: Final = frozenset({"ready", "not_ready", "unknown"})
_COMPATIBILITY_STATES: Final = frozenset(
    {"compatible", "compatible_with_deprecations", "upgrade_required", "incompatible", "unknown"}
)
#: `authentication_required` is the normalized *state* of a reachable target that
#: will not serve this caller yet; the same-named `CoreSafeWarningCode` stays the
#: actionable advisory. Both are published, and neither implies the other.
_CONNECTION_STATES: Final = frozenset(
    {"connected", "connecting", "disconnected", "unreachable", "authentication_required", "unknown"}
)
_WARNING_CODES: Final = frozenset(
    {
        "endpoint_unreachable",
        "authentication_required",
        "version_incompatible",
        "upgrade_required",
        "workspace_format_incompatible",
        "degraded",
    }
)
_SAFE_ACTIONS: Final = frozenset({"start", "stop", "restart", "reconnect", "open"})
#: Process-lifecycle actions: safe only for a locally-managed, local target.
_LOCAL_ONLY_ACTIONS: Final = frozenset({"start", "stop", "restart"})

#: The scalar bounds the schema declares that no value-domain guard carries:
#: `display_name` is a bare bounded string, and the two arrays are capped.
_DISPLAY_NAME_MIN_LENGTH: Final = 1
_DISPLAY_NAME_MAX_LENGTH: Final = 256
_MAX_WARNING_CODES: Final = 32
_MAX_PERMITTED_ACTIONS: Final = 16

_TARGET_CONTRACT_VERSION_REFUSAL: Final = "contract_version is not a well-formed ContractVersion"
_TARGET_REF_REFUSAL: Final = "target_ref is not a well-formed Identifier"
_DISPLAY_NAME_REFUSAL: Final = "display_name is not a string of 1..256 characters"
_WORKSPACE_REF_REFUSAL: Final = "workspace_ref is not a well-formed WorkspaceId"
_ENDPOINT_PROFILE_REF_REFUSAL: Final = "endpoint_profile_ref is not a well-formed Identifier"
_STATUS_CONTRACT_VERSION_REFUSAL: Final = "contract_version is not a well-formed ContractVersion"
_SERVER_VERSION_REFUSAL: Final = "server_version is not a well-formed ReleaseVersion"
_PROTOCOL_VERSION_REFUSAL: Final = "protocol_version is not a well-formed ContractVersion"
_VERSION_AGREEMENT_REFUSAL: Final = (
    "contract_version does not match the target's contract_version"
)
_WARNING_CODES_LIMIT_REFUSAL: Final = "warning_codes carries more than 32 entries"
_PERMITTED_ACTIONS_LIMIT_REFUSAL: Final = "permitted_actions carries more than 16 entries"
_TARGET_REF_DUPLICATE_REFUSAL: Final = "target_ref repeats across the target set"
_WORKSPACE_REF_DUPLICATE_REFUSAL: Final = (
    "workspace_ref repeats across the target set: two target authorities "
    "may not share one writable workspace identity"
)

_TARGET_KIND_REFUSAL: Final = "kind is not a known CoreTargetKind"
_TARGET_MANAGEMENT_REFUSAL: Final = "management is not a known CoreTargetManagement"
_LIFECYCLE_STATE_REFUSAL: Final = "lifecycle_state is not a known CoreLifecycleState"
_READINESS_STATE_REFUSAL: Final = "readiness_state is not a known CoreReadinessState"
_COMPATIBILITY_STATE_REFUSAL: Final = "compatibility_state is not a known CoreCompatibilityState"
_CONNECTION_STATE_REFUSAL: Final = "connection_state is not a known CoreConnectionState"
_WARNING_CODE_REFUSAL: Final = "warning_codes contains a value that is not a known CoreSafeWarningCode"
_WARNING_CODES_DUPLICATE_REFUSAL: Final = "warning_codes contains a duplicate code"
_PERMITTED_ACTION_REFUSAL: Final = "permitted_actions contains a value that is not a known CoreSafeAction"
_PERMITTED_ACTIONS_DUPLICATE_REFUSAL: Final = "permitted_actions contains a duplicate action"
_LOCAL_ACTION_REFUSAL: Final = (
    "start/stop/restart may only be offered for a locally_managed local target"
)


def validate_core_target(target: object) -> None:
    """Reject a target that falls outside its declared closed vocabularies or scalar bounds.

    Both halves the schema declares: `kind` and `management` against their closed
    vocabularies, and every scalar against the value domain it `$ref`s -- with the
    generated guard for that domain, not a restated pattern.
    """
    if not isinstance(target, CoreTargetV1):
        raise ContractSemanticError("target is not a CoreTargetV1")
    if not is_contract_version(target.contract_version):
        raise ContractSemanticError(_TARGET_CONTRACT_VERSION_REFUSAL)
    if not is_identifier(target.target_ref):
        raise ContractSemanticError(_TARGET_REF_REFUSAL)
    if not isinstance(target.display_name, str) or not (
        _DISPLAY_NAME_MIN_LENGTH <= len(target.display_name) <= _DISPLAY_NAME_MAX_LENGTH
    ):
        raise ContractSemanticError(_DISPLAY_NAME_REFUSAL)
    if target.kind not in _TARGET_KINDS:
        raise ContractSemanticError(_TARGET_KIND_REFUSAL)
    if not is_workspace_id(target.workspace_ref):
        raise ContractSemanticError(_WORKSPACE_REF_REFUSAL)
    if target.management not in _TARGET_MANAGEMENTS:
        raise ContractSemanticError(_TARGET_MANAGEMENT_REFUSAL)
    if not is_identifier(target.endpoint_profile_ref):
        raise ContractSemanticError(_ENDPOINT_PROFILE_REF_REFUSAL)


def validate_core_target_authorities(targets: Iterable[object]) -> None:
    """Reject a set of targets in which two authorities collide.

    Each target must be individually valid, and across the set neither `target_ref`
    nor `workspace_ref` may repeat. The workspace clause is the handoff rule: a
    writable workspace identity belongs to exactly one target authority, so two
    descriptors naming the same `workspace_ref` are two authorities claiming one
    writable store -- an invariant a single descriptor cannot see and the schema
    cannot express.
    """
    seen_target_refs: set[str] = set()
    seen_workspace_refs: set[str] = set()
    for target in targets:
        validate_core_target(target)
        assert isinstance(target, CoreTargetV1)
        if target.target_ref in seen_target_refs:
            raise ContractSemanticError(_TARGET_REF_DUPLICATE_REFUSAL)
        if target.workspace_ref in seen_workspace_refs:
            raise ContractSemanticError(_WORKSPACE_REF_DUPLICATE_REFUSAL)
        seen_target_refs.add(target.target_ref)
        seen_workspace_refs.add(target.workspace_ref)


def decode_core_target(payload: object, path: str = "CoreTargetV1") -> CoreTargetV1:
    """Structurally decode and then enforce target closed-vocabulary semantics."""
    target = CoreTargetV1.from_wire(payload, path)
    validate_core_target(target)
    return target


def encode_core_target(target: object) -> Mapping[str, Any]:
    """Validate a target before rendering it for publication."""
    validate_core_target(target)
    assert isinstance(target, CoreTargetV1)
    return target.to_wire()


def validate_core_safe_status(status: object) -> None:
    """Reject a safe status that falls outside its declared closed domains or scalar bounds.

    Covers the closed vocabularies, the scalar value domains (`contract_version`
    and the two optional versions, each via its generated guard), the declared
    array caps, and the cross-field invariants the schema cannot express: a
    `start`/`stop`/`restart` entry in `permitted_actions` is refused unless the
    nested `target` is both `locally_managed` and `local`, and the status must be
    published at the same `contract_version` as the target it describes.
    """
    if not isinstance(status, CoreSafeStatusV1):
        raise ContractSemanticError("status is not a CoreSafeStatusV1")
    validate_core_target(status.target)
    if not is_contract_version(status.contract_version):
        raise ContractSemanticError(_STATUS_CONTRACT_VERSION_REFUSAL)
    if status.contract_version != status.target.contract_version:
        raise ContractSemanticError(_VERSION_AGREEMENT_REFUSAL)
    if status.server_version is not None and not is_release_version(status.server_version):
        raise ContractSemanticError(_SERVER_VERSION_REFUSAL)
    if status.protocol_version is not None and not is_contract_version(status.protocol_version):
        raise ContractSemanticError(_PROTOCOL_VERSION_REFUSAL)
    if status.lifecycle_state not in _LIFECYCLE_STATES:
        raise ContractSemanticError(_LIFECYCLE_STATE_REFUSAL)
    if status.readiness_state not in _READINESS_STATES:
        raise ContractSemanticError(_READINESS_STATE_REFUSAL)
    if status.compatibility_state not in _COMPATIBILITY_STATES:
        raise ContractSemanticError(_COMPATIBILITY_STATE_REFUSAL)
    if status.connection_state not in _CONNECTION_STATES:
        raise ContractSemanticError(_CONNECTION_STATE_REFUSAL)

    if len(status.warning_codes) > _MAX_WARNING_CODES:
        raise ContractSemanticError(_WARNING_CODES_LIMIT_REFUSAL)
    if len(set(status.warning_codes)) != len(status.warning_codes):
        raise ContractSemanticError(_WARNING_CODES_DUPLICATE_REFUSAL)
    if any(code not in _WARNING_CODES for code in status.warning_codes):
        raise ContractSemanticError(_WARNING_CODE_REFUSAL)

    if len(status.permitted_actions) > _MAX_PERMITTED_ACTIONS:
        raise ContractSemanticError(_PERMITTED_ACTIONS_LIMIT_REFUSAL)
    if len(set(status.permitted_actions)) != len(status.permitted_actions):
        raise ContractSemanticError(_PERMITTED_ACTIONS_DUPLICATE_REFUSAL)
    if any(action not in _SAFE_ACTIONS for action in status.permitted_actions):
        raise ContractSemanticError(_PERMITTED_ACTION_REFUSAL)

    is_locally_managed_local = (
        status.target.management == "locally_managed" and status.target.kind == "local"
    )
    if not is_locally_managed_local and set(status.permitted_actions) & _LOCAL_ONLY_ACTIONS:
        raise ContractSemanticError(_LOCAL_ACTION_REFUSAL)


def decode_core_safe_status(payload: object, path: str = "CoreSafeStatusV1") -> CoreSafeStatusV1:
    """Structurally decode a safe status, then enforce its semantics."""
    status = CoreSafeStatusV1.from_wire(payload, path)
    validate_core_safe_status(status)
    return status


def encode_core_safe_status(status: object) -> Mapping[str, Any]:
    """Validate a safe status before rendering it for publication."""
    validate_core_safe_status(status)
    assert isinstance(status, CoreSafeStatusV1)
    return status.to_wire()
