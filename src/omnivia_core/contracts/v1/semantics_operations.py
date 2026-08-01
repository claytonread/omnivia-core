"""Operation-catalogue lookup and request-metadata semantics (A2.5, ADR-038/ADR-039).

The canonical catalogue lives in `x-omnivia-operation-catalogue` and reaches this
package as the generated :data:`~omnivia_core.contracts.v1.generated.OPERATION_CATALOGUE`.
This module is the narrow read surface over it: resolve an operation name to its
frozen metadata, check a `RequestMetadata` against the parts of that metadata a
request is actually able to state, and check that an `ApiError` is one the
operation is allowed to fail with.

None of that is dispatch, and none of it is authorization. There is no registry,
no handler table, and no state here: `validate_operation_request_metadata` decides
only whether a request is *well formed for the operation it names*. Whether the
claimed principal is authenticated, whether the server actually granted the
capability, whether the workspace exists, and what the operation then does are all
questions this layer deliberately cannot answer -- it never sees a server, and a
caller can assert anything it likes in its own metadata.

Standard library only. Nothing here may depend on runtime, storage, HTTP, MCP, CLI,
Platform, Dev, or a validation framework.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from omnivia_core.contracts.v1.codec import validate_error_retry_class
from omnivia_core.contracts.v1.compatibility import (
    ContractSemanticError,
    compare_contract_versions,
)
from omnivia_core.contracts.v1.generated import (
    IDEMPOTENCY_KEY_PATTERN,
    OPAQUE_TOKEN_PATTERN,
    OPERATION_CATALOGUE,
    ApiError,
    CapabilityRequirement,
    MutationPrecondition,
    OperationMetadata,
    RequestMetadata,
)
from omnivia_core.contracts.v1.semantics import validate_operation_scope_workspace_id

__all__ = [
    "get_operation_metadata",
    "validate_operation_error",
    "validate_operation_request_metadata",
]

#: Name -> metadata, built once from the generated catalogue. The catalogue is the
#: source; this is only an index into it, so every value here *is* the very object
#: `OPERATION_CATALOGUE` holds rather than a copy that could drift from it.
_CATALOGUE_BY_NAME: Final[dict[str, OperationMetadata]] = {
    operation.name: operation for operation in OPERATION_CATALOGUE
}


# --- direct-entry type guards -------------------------------------------------


def _require_str(value: object, label: str) -> str:
    """Raise unless `value` is a `str`, returning it narrowed."""
    if isinstance(value, bool) or not isinstance(value, str):
        raise ContractSemanticError(f"{label}: expected a string, got {type(value).__name__}")
    return value


def _require_sequence(value: object, label: str) -> Sequence[object]:
    """Raise unless `value` is a non-string sequence, returning it narrowed."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractSemanticError(f"{label}: expected a sequence, got {type(value).__name__}")
    return value


def _require_type(value: object, expected: type, label: str) -> None:
    """Raise unless `value` is an instance of `expected`.

    Every public function here is a direct entry point: a caller may hand-build a
    dataclass rather than decode one through `from_wire`, and a frozen dataclass
    enforces nothing about the types of the fields it was handed. A wrongly typed
    value must surface as a :class:`ContractSemanticError` rather than as a raw
    `TypeError`/`AttributeError` from whatever this module does with it next.
    """
    if not isinstance(value, expected):
        raise ContractSemanticError(
            f"{label}: expected {expected.__name__}, got {type(value).__name__}"
        )


# --- schema value domains -----------------------------------------------------

#: `IdempotencyKey`'s and `OpaqueToken`'s schema `maxLength`. Structural decoding
#: enforces neither -- `from_wire` checks that a value is a string and stops there --
#: so an overlong value arrives here intact and is either rejected in this module or
#: not at all.
_IDEMPOTENCY_KEY_MAX_LENGTH: Final = 128
_OPAQUE_TOKEN_MAX_LENGTH: Final = 512

#: Compiled from the *generated* pattern constants rather than from a regex copied
#: out of the schema, so a canonical pattern that is later widened or narrowed
#: reaches this module through the generator instead of leaving a second spelling
#: here to drift silently out of agreement with the contract.
_IDEMPOTENCY_KEY_RE: Final = re.compile(IDEMPOTENCY_KEY_PATTERN)
_OPAQUE_TOKEN_RE: Final = re.compile(OPAQUE_TOKEN_PATTERN)


def _validate_idempotency_key(value: object, label: str) -> str:
    """Raise unless `value` is a schema-valid `IdempotencyKey`, returning it narrowed.

    `fullmatch`, never `.match`: the pattern ends in `$`, which in Python matches
    immediately before a final newline, so `"key-1\\n"` would otherwise be accepted
    here while strict schema validation refuses it -- and a trailing newline in a key
    is exactly the kind of value that survives a copy-paste and then fails to match
    the *same* mutation on retry, which is the one thing the key exists to prevent.
    """
    text = _require_str(value, label)
    if len(text) > _IDEMPOTENCY_KEY_MAX_LENGTH or not _IDEMPOTENCY_KEY_RE.fullmatch(text):
        raise ContractSemanticError(f"{label}: {text!r} is not a valid IdempotencyKey")
    return text


def _validate_opaque_token(value: object, label: str) -> str:
    """Raise unless `value` is a schema-valid `OpaqueToken`, returning it narrowed."""
    text = _require_str(value, label)
    if len(text) > _OPAQUE_TOKEN_MAX_LENGTH or not _OPAQUE_TOKEN_RE.fullmatch(text):
        raise ContractSemanticError(f"{label}: {text!r} is not a valid OpaqueToken")
    return text


# --- catalogue lookup ---------------------------------------------------------


def get_operation_metadata(operation: str) -> OperationMetadata:
    """Return the frozen catalogue metadata for `operation`.

    An unknown name is rejected rather than defaulted. That deliberately includes
    the runtime probes (`service.health`, `service.readiness`, `service.discover`),
    which are a separate contract and carry no catalogue metadata at all, and any
    plausible-looking name v1 does not define. Returning some fallback posture for
    a name this build has never seen would be stating a scope, capability, and
    error set nothing stands behind.

    This resolves metadata only. It does not dispatch, and it does not look at, or
    validate, an operation's `input` payload.
    """
    name = _require_str(operation, "operation")
    metadata = _CATALOGUE_BY_NAME.get(name)
    if metadata is None:
        raise ContractSemanticError(f"unknown application operation {name!r}")
    return metadata


# --- request-metadata agreement -----------------------------------------------


def _validate_required_scopes(operation: OperationMetadata, metadata: RequestMetadata) -> None:
    """Raise unless the request holds every scope the catalogue requires.

    Extra scopes are left alone: a caller presenting more than this operation needs
    is not making a claim about this operation, and narrowing is never the failure
    mode worth rejecting.
    """
    raw_scopes = _require_sequence(metadata.scopes, "RequestMetadata.scopes")
    held = {
        _require_str(scope, f"RequestMetadata.scopes[{index}]")
        for index, scope in enumerate(raw_scopes)
    }
    missing = [scope for scope in operation.scope.required_scopes if scope not in held]
    if missing:
        raise ContractSemanticError(
            f"operation {operation.name!r} requires scope(s) {missing} that "
            "RequestMetadata.scopes does not hold"
        )


def _validate_required_capability(operation: OperationMetadata, metadata: RequestMetadata) -> None:
    """Raise unless the request declares the catalogue capability, required, at or
    above the catalogue minimum.

    The declaration has to be present and marked `required`: an operation whose
    capability the caller declared *optional* is a caller saying it will degrade
    gracefully if the capability is missing, which for a catalogue-required
    capability is not a degradation the operation supports.

    A *stricter* minimum is accepted, since demanding a higher version can only
    narrow what satisfies the request. Versions are compared as contract versions
    rather than lexically, so `"1.10"` correctly exceeds `"1.9"`.

    Extra requirements for other capabilities stay untouched -- they can only add
    conditions the request must also meet -- but every declared id must be unique.
    A request naming one id twice at two versions leaves nothing able to say which
    is in force, so it is rejected rather than resolved by picking one.
    """
    required = operation.required_capability
    raw_requirements = _require_sequence(
        metadata.required_capabilities, "RequestMetadata.required_capabilities"
    )

    declared_ids: list[str] = []
    matches: list[CapabilityRequirement] = []
    for index, requirement in enumerate(raw_requirements):
        label = f"RequestMetadata.required_capabilities[{index}]"
        _require_type(requirement, CapabilityRequirement, label)
        assert isinstance(requirement, CapabilityRequirement)
        capability_id = _require_str(requirement.id, f"{label}.id")
        declared_ids.append(capability_id)
        if capability_id == required.id:
            matches.append(requirement)

    duplicates = sorted({name for name in declared_ids if declared_ids.count(name) > 1})
    if duplicates:
        raise ContractSemanticError(
            f"RequestMetadata.required_capabilities declares duplicate capability id(s) "
            f"{duplicates}"
        )

    if len(matches) != 1:
        raise ContractSemanticError(
            f"operation {operation.name!r} requires exactly one RequestMetadata capability "
            f"requirement for {required.id!r}, found {len(matches)}"
        )
    declaration = matches[0]

    if declaration.required is not True:
        raise ContractSemanticError(
            f"operation {operation.name!r} requires capability {required.id!r} to be declared "
            "`required`, found an optional requirement"
        )

    minimum = _require_str(
        declaration.minimum_version,
        f"RequestMetadata.required_capabilities[{required.id}].minimum_version",
    )
    try:
        comparison = compare_contract_versions(minimum, required.minimum_version)
    except ValueError as error:
        raise ContractSemanticError(
            f"RequestMetadata.required_capabilities[{required.id}].minimum_version: {error}"
        ) from error
    if comparison < 0:
        raise ContractSemanticError(
            f"operation {operation.name!r} requires capability {required.id!r} at "
            f"{required.minimum_version!r} or stricter, found {minimum!r}"
        )


def _validate_idempotency(operation: OperationMetadata, metadata: RequestMetadata) -> None:
    """Raise unless the request's idempotency key agrees with the catalogue posture.

    Both directions are enforced. A required key that is absent is a request that
    can become a second mutation on a network-level repeat. A key supplied to an
    operation that does not honour one is worse than useless: silently ignoring it
    would let the caller believe the call is replay-safe when nothing makes it so.

    A key that is present is also held to `IdempotencyKey`'s own value domain before
    either posture is judged. Presence is not the property that makes a key work: two
    attempts at the same mutation have to carry the *same* key, and a value the server
    will reject as malformed is a key that was never able to do that. Rejecting it as
    a contract violation rather than passing it through is what keeps the caller from
    learning at the second attempt that the first one was never replay-safe.
    """
    key = metadata.idempotency_key
    if key is not None:
        _validate_idempotency_key(key, "RequestMetadata.idempotency_key")
    if operation.idempotency.required and key is None:
        raise ContractSemanticError(
            f"operation {operation.name!r} requires RequestMetadata.idempotency_key"
        )
    if not operation.idempotency.supports_idempotency_key and key is not None:
        raise ContractSemanticError(
            f"operation {operation.name!r} does not honour RequestMetadata.idempotency_key"
        )


def _validate_precondition(operation: OperationMetadata, metadata: RequestMetadata) -> None:
    """Raise unless the request's mutation precondition agrees with the catalogue posture.

    As with the idempotency key, an unsupported precondition is rejected rather
    than dropped: a caller that supplied one believes the mutation is guarded by
    it. And as with the key, a precondition that is present must carry a record
    version inside `OpaqueToken`'s value domain: a malformed version cannot match
    the record the caller means to guard, so a mutation sent with one is unguarded
    while looking guarded -- the precise failure the precondition exists to prevent.
    """
    precondition = metadata.mutation_precondition
    if precondition is not None:
        _require_type(
            precondition, MutationPrecondition, "RequestMetadata.mutation_precondition"
        )
        _validate_opaque_token(
            precondition.record_version,
            "RequestMetadata.mutation_precondition.record_version",
        )
    if operation.precondition.required and precondition is None:
        raise ContractSemanticError(
            f"operation {operation.name!r} requires RequestMetadata.mutation_precondition"
        )
    if not operation.precondition.supports_mutation_precondition and precondition is not None:
        raise ContractSemanticError(
            f"operation {operation.name!r} does not honour RequestMetadata.mutation_precondition"
        )


def validate_operation_request_metadata(
    operation: str, metadata: object
) -> OperationMetadata:
    """Raise unless `metadata` is well formed for the catalogue operation `operation`,
    returning that operation's frozen metadata.

    Exactly the catalogue-governed part of a request is checked: workspace-id
    agreement with the operation's scope kind, the required scopes, the required
    capability declaration, and the idempotency/precondition postures -- including
    the canonical value domains of the two values a request supplies for those
    postures, `IdempotencyKey` and the precondition's `OpaqueToken` record version.
    Neither is enforced anywhere upstream of here: `from_wire` is deliberately
    tolerant and checks only that a value is a string, and a hand-built frozen
    dataclass checks nothing at all, so a malformed key or version reaches this
    function intact.

    Everything a request *cannot* establish about itself is out of scope -- this
    does not decide whether the claimed principal is authenticated, whether the
    server granted the capability, or whether the caller may touch the named
    workspace.

    Extra scopes and extra uniquely named capability requirements are left in
    place: neither widens the authority the request carries.
    """
    _require_type(metadata, RequestMetadata, "metadata")
    assert isinstance(metadata, RequestMetadata)
    catalogue_entry = get_operation_metadata(operation)

    validate_operation_scope_workspace_id(
        catalogue_entry.scope.scope_kind, metadata.workspace_id
    )
    _validate_required_scopes(catalogue_entry, metadata)
    _validate_required_capability(catalogue_entry, metadata)
    _validate_idempotency(catalogue_entry, metadata)
    _validate_precondition(catalogue_entry, metadata)
    return catalogue_entry


# --- allowed errors -----------------------------------------------------------


def validate_operation_error(operation: str, error: object) -> None:
    """Raise unless `error` is an error the catalogue permits `operation` to fail with.

    The allow-list is checked first, so a code outside it -- including one this
    build has never seen -- is reported as a disallowed error for this operation
    rather than reaching the retry-class rule at all. A code that *is* allowed then
    goes through the existing frozen retry-class check
    (:func:`~omnivia_core.contracts.v1.codec.validate_error_retry_class`), which
    keeps its own :class:`~omnivia_core.contracts.v1.codec.RetryClassMismatchError`:
    a known code paired with the wrong class is a distinct protocol violation from
    an operation failing with an error it is not allowed to raise, and collapsing
    the two would tell a caller less than it needs.
    """
    _require_type(error, ApiError, "error")
    assert isinstance(error, ApiError)
    catalogue_entry = get_operation_metadata(operation)

    code = _require_str(error.code, "ApiError.code")
    if code not in catalogue_entry.allowed_errors:
        raise ContractSemanticError(
            f"operation {catalogue_entry.name!r} is not permitted to fail with error code "
            f"{code!r}"
        )
    validate_error_retry_class(error)
