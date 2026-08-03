"""Wire codec, envelope totality and trust boundary for the Host Contract v1.

The generated records in :mod:`omnivia_core.host_contract.v1.generated` own the
strict per-record ``to_wire`` / ``from_wire`` pair: every governed pattern,
bound, timestamp, cardinality, closed field set and closed vocabulary is
enforced there, in both directions, so this module never sees a structurally
admitted but contract-invalid value. What it adds on top is the version
admission the schema alone cannot express, plus the semantics around the
records:

- **supported-version admission** -- the schema pins the major, and this build
  speaks exactly one minor window inside it. A schema-valid document from a
  future minor is refused *before* the operation runs, never after;
- **deterministic serialization** -- a canonical JSON form, so two encoders that
  agree on the value agree byte for byte;
- **the total result envelope** -- exactly one of ``data``, ``denial``,
  ``availability`` or ``failure`` matches ``outcome``, with the single governed
  pairing (a degraded success carries ``availability`` alongside ``data``). The
  rule itself is generated from the canonical schema's ``allOf``; this module
  reports it with the document's correlation ID attached;
- **availability and health reading** -- only ``available`` is available, and a
  ``temporarily_unhealthy`` answer reports its retry window rather than being
  retried immediately;
- **the trusted-host authority boundary** -- a ``ShellContext`` is resolved from
  the trusted host by ``contextId``, never from the hosted payload; an authority
  change produces a new immutable context rather than a mutation; and a context
  whose stated ``expiresAt`` has been reached is authority for nothing.

Standard library only. Nothing here may depend on runtime, storage, HTTP, MCP,
CLI, Platform, Dev, or a validation framework.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Final, TypeAlias, cast

from omnivia_core.host_contract.v1.compatibility import require_supported_version
from omnivia_core.host_contract.v1.generated import (
    HOST_NAMESPACES,
    IDENTIFIER_PATTERN,
    CapabilityAvailability,
    CapabilityDenial,
    HostContractDecodeError,
    HostRequest,
    HostResult,
    ShellContext,
)

__all__ = [
    "CONTEXT_AUTHORITY_FIELDS",
    "GENERIC_DENIAL_CAPABILITY_ID",
    "GENERIC_DENIAL_CORRELATION_ID",
    "GENERIC_DENIAL_SAFE_REASON",
    "HOST_OPERATION_EFFECT_CLASSES",
    "PROHIBITED_PAYLOAD_AUTHORITY_FIELDS",
    "DenialDisplayPolicy",
    "HostAuthorityError",
    "admit_display_safe_denial",
    "changed_authority_fields",
    "decode_capability_availability",
    "decode_capability_denial",
    "decode_host_request",
    "decode_host_result",
    "decode_json_document",
    "decode_shell_context",
    "encode_host_request",
    "encode_host_result",
    "encode_shell_context",
    "is_available",
    "is_degraded_success",
    "is_handle_valid",
    "payload_authority_claims",
    "resolve_trusted_context",
    "result_branch",
    "retry_after_seconds",
    "to_canonical_json",
    "to_canonical_json_document",
    "validate_context_rotation",
    "validate_result_totality",
]


# --------------------------------------------------------------------------
# Deterministic JSON
# --------------------------------------------------------------------------


def to_canonical_json(payload: Mapping[str, Any]) -> str:
    """Serialize a wire mapping to the compact canonical JSON form.

    Keys are sorted and separators are fixed, so the same value always produces
    the same bytes regardless of how the mapping was built. ``NaN`` and the
    infinities are rejected rather than emitted as the non-standard literals
    Python would otherwise produce.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def to_canonical_json_document(payload: Mapping[str, Any]) -> str:
    """Serialize a wire mapping to the canonical on-disk JSON form.

    The same ordering guarantees as :func:`to_canonical_json`, indented and
    newline-terminated so checked-in documents diff cleanly.
    """
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2)
    return f"{text}\n"


def decode_json_document(text: str) -> Any:
    """Parse a JSON document, rejecting the non-standard ``NaN``/``Infinity`` literals."""
    return json.loads(text, parse_constant=_reject_json_constant)


def _reject_json_constant(name: str) -> Any:
    raise HostContractDecodeError(f"{name!r} is not valid JSON")


# --------------------------------------------------------------------------
# Envelopes
# --------------------------------------------------------------------------


HOST_OPERATION_EFFECT_CLASSES: Final[tuple[str, ...]] = (
    "read",
    "local_mutation",
    "consequential_external",
)

_IDENTIFIER_RE: Final[re.Pattern[str]] = re.compile(IDENTIFIER_PATTERN)


@dataclass(frozen=True, slots=True)
class PublishedOperation:
    """One immutable host-owned operation-catalogue entry.

    This is a trusted local admission input, not a wire record.  Core defines
    its shape but deliberately publishes no concrete production operation IDs;
    each host owns the entries it supplies.
    """

    namespace: str
    operation_id: str
    request_schema_id: str
    result_schema_id: str
    effect_class: str
    idempotency_required: bool
    required_capabilities: tuple[str, ...]
    minimum_contract_minor: int

    def validate(self) -> None:
        if type(self) is not PublishedOperation:
            raise HostContractDecodeError("OperationCatalogue: malformed operation entry")
        for value in (
            self.namespace,
            self.operation_id,
            self.request_schema_id,
            self.result_schema_id,
        ):
            if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
                raise HostContractDecodeError("OperationCatalogue: malformed operation entry")
        if self.namespace not in HOST_NAMESPACES:
            raise HostContractDecodeError("OperationCatalogue: malformed operation entry")
        if self.effect_class not in HOST_OPERATION_EFFECT_CLASSES:
            raise HostContractDecodeError("OperationCatalogue: malformed operation entry")
        if type(self.idempotency_required) is not bool:
            raise HostContractDecodeError("OperationCatalogue: malformed operation entry")
        if type(self.required_capabilities) is not tuple:
            raise HostContractDecodeError("OperationCatalogue: malformed operation entry")
        if any(
            type(capability) is not str or _IDENTIFIER_RE.fullmatch(capability) is None
            for capability in self.required_capabilities
        ) or len(set(self.required_capabilities)) != len(self.required_capabilities):
            raise HostContractDecodeError("OperationCatalogue: malformed operation entry")
        if type(self.minimum_contract_minor) is not int or self.minimum_contract_minor < 0:
            raise HostContractDecodeError("OperationCatalogue: malformed operation entry")


@dataclass(frozen=True, slots=True)
class OperationCatalogue:
    """The explicit immutable operation catalogue published by a trusted host."""

    operations: tuple[PublishedOperation, ...]

    def validate(self) -> None:
        if type(self) is not OperationCatalogue or type(self.operations) is not tuple:
            raise HostContractDecodeError("OperationCatalogue: malformed catalogue")
        keys: set[tuple[str, str]] = set()
        for operation in self.operations:
            if type(operation) is not PublishedOperation:
                raise HostContractDecodeError("OperationCatalogue: malformed catalogue")
            operation.validate()
            key = (operation.namespace, operation.operation_id)
            if key in keys:
                raise HostContractDecodeError("OperationCatalogue: duplicate operation entry")
            keys.add(key)


#: Which optional branch each ``outcome`` names, for reporting. The rule is
#: enforced by the generated ``HostResult.validate_branches``, which is emitted
#: from the canonical schema's ``allOf``; this mapping only answers *which*
#: branch a valid result carries.
_REQUIRED_BRANCH: Final[Mapping[str, str]] = {
    "success": "data",
    "denied": "denial",
    "unavailable": "availability",
    "failed": "failure",
}


def validate_result_totality(result: HostResult) -> None:
    """Raise unless exactly the branch ``outcome`` names is present.

    The envelope is total: an ``outcome`` without its branch, or with a branch
    belonging to a different outcome, is a protocol violation rather than
    something to resolve by preferring one side.

    One pairing is governed rather than exclusive: a *degraded* success carries
    ``availability`` alongside ``data`` to name the optional capability that was
    missing. That is the only case in which two branch fields may coexist, and
    it never applies to ``denied``, ``unavailable`` or ``failed``.

    The rule itself is the canonical schema's, generated rather than restated
    here. This adds the document's ``correlationId`` to the refusal, so a caller
    can report a generic safe failure keyed by it.
    """
    try:
        result.validate_branches()
    except HostContractDecodeError as error:
        if error.correlation_id is None and isinstance(result.correlation_id, str):
            error.correlation_id = result.correlation_id
        raise


def decode_host_result(payload: object) -> HostResult:
    """Decode a result envelope, admitting only the supported contract version.

    ``HostResult.from_wire`` already enforces every governed constraint,
    including the total-envelope rule. This adds the version admission: a
    result from an unsupported major or minor is refused before anything reads
    its outcome.
    """
    result = HostResult.from_wire(payload)
    require_supported_version(result.contract_version)
    return result


def encode_host_result(result: HostResult) -> dict[str, Any]:
    """Validate a result envelope and encode it to its wire mapping.

    Totality is checked explicitly before emission, so an envelope assembled in
    code rather than decoded can never be published with a missing or
    mismatched branch.
    """
    validate_result_totality(result)
    result.validate()
    require_supported_version(result.contract_version)
    return result.to_wire()


def result_branch(result: HostResult) -> str:
    """Return the branch name ``outcome`` selects.

    A degraded success answers ``"data"``: the availability it also carries
    explains the degradation, it does not replace the answer.
    """
    validate_result_totality(result)
    return _REQUIRED_BRANCH[result.outcome]


def is_degraded_success(result: HostResult) -> bool:
    """Return whether this is a success that also reports a missing capability."""
    return result.outcome == "success" and result.availability is not None


def decode_host_request(payload: object) -> HostRequest:
    """Structurally decode a request without claiming host semantic acceptance.

    Unknown top-level fields are ignored rather than refused: the canonical
    schema leaves the request open so an intermediary can forward a newer
    peer's safe additions. Ignoring them is what keeps them inert -- see
    :func:`payload_authority_claims` for the authority half of that rule.

    Openness stops at the declared version. A request naming a major this build
    does not speak, or a minor outside its support window, is refused here --
    before dispatch, never after -- because a newer minor may attach meaning to
    a field this build would otherwise ignore.
    Operation publication is host-owned state and therefore cannot be supplied
    to this global wire function. :class:`~omnivia_core.host_contract.v1.bound.BoundHost`
    performs the separate semantic admission step against its captured catalogue.
    """
    request = HostRequest.from_wire(payload)
    require_supported_version(request.contract_version)
    return request


def encode_host_request(request: HostRequest) -> dict[str, Any]:
    """Validate and encode a request without claiming host semantic acceptance."""
    request.validate()
    require_supported_version(request.contract_version)
    return request.to_wire()


def decode_shell_context(payload: object) -> ShellContext:
    """Decode a trusted-host ShellContext."""
    return ShellContext.from_wire(payload)


def encode_shell_context(context: ShellContext) -> dict[str, Any]:
    """Validate a ShellContext and encode it to its wire mapping."""
    return context.to_wire()


def decode_capability_denial(payload: object) -> CapabilityDenial:
    """Decode a denial, failing closed on an unknown category.

    An unknown category is never displayed as itself and never downgraded to a
    known one. The raised error carries the document's ``correlationId`` when it
    had a usable one, so a caller can show a generic safe denial keyed by the
    correlation ID without reading anything else out of the rejected document.
    """
    try:
        return CapabilityDenial.from_wire(payload)
    except HostContractDecodeError as error:
        if error.correlation_id is None:
            error.correlation_id = _safe_correlation_id(payload)
        raise


#: Fixed display-safe fallback values. They reveal nothing about the rejected
#: category, capability, reason, policy or secret-like content.
GENERIC_DENIAL_CAPABILITY_ID: Final[str] = "host.denial"
GENERIC_DENIAL_CORRELATION_ID: Final[str] = "correlation-unavailable"
GENERIC_DENIAL_SAFE_REASON: Final[str] = (
    "The request could not be completed. Reference the correlation ID for support."
)

#: A host-owned validator/redactor. Returning an exact string admits that
#: display text (possibly redacted); ``None`` rejects it. Core cannot infer
#: content safety from a closed record or a regular expression.
DenialDisplayPolicy: TypeAlias = Callable[[CapabilityDenial], str | None]


def admit_display_safe_denial(
    payload: object, policy: DenialDisplayPolicy | None = None
) -> CapabilityDenial:
    """Return a denial admitted by trusted host display policy, or a fixed refusal.

    Structural decoding proves only the governed record shape. It does not prove
    that ``safeReason`` contains no token, credential, private key, policy detail
    or cross-Workspace identifier. Therefore the original denial is usable only
    when an explicitly trusted host policy returns admitted/redacted display
    text. Missing policy, malformed records, unknown categories, policy errors
    and rejected text all return the same generic correlation-bound denial.

    A policy is host-owned code, so it can raise for reasons this contract knows
    nothing about -- an unreachable redaction service, a lookup miss, a bug. A
    raised policy is a policy that did not admit the text, and the whole point of
    this function is that its caller always has something safe to display, so the
    exception is answered with the generic denial rather than propagated into a
    display path. Only ``BaseException`` (``KeyboardInterrupt``, ``SystemExit``)
    still travels, because those are not policy verdicts.
    """
    correlation_id = _safe_correlation_id(payload)
    if type(payload) is CapabilityDenial:
        denial = payload
        try:
            denial.validate()
        except HostContractDecodeError:
            return _generic_denial(correlation_id)
        correlation_id = denial.correlation_id
    else:
        try:
            denial = decode_capability_denial(payload)
        except HostContractDecodeError:
            return _generic_denial(correlation_id)
    if policy is None:
        return _generic_denial(correlation_id)
    try:
        safe_reason = policy(denial)
    except Exception:  # noqa: BLE001 -- any policy failure is a refusal, see above
        return _generic_denial(correlation_id)
    if type(safe_reason) is not str:
        return _generic_denial(correlation_id)
    admitted = replace(denial, safe_reason=safe_reason)
    try:
        admitted.validate()
    except HostContractDecodeError:
        return _generic_denial(correlation_id)
    return admitted


def _generic_denial(correlation_id: str | None) -> CapabilityDenial:
    return CapabilityDenial(
        category="blocked_by_policy",
        capability_id=GENERIC_DENIAL_CAPABILITY_ID,
        safe_reason=GENERIC_DENIAL_SAFE_REASON,
        approval_available=False,
        correlation_id=correlation_id or GENERIC_DENIAL_CORRELATION_ID,
    )


def decode_capability_availability(payload: object) -> CapabilityAvailability:
    """Decode an availability answer, failing closed on an unknown state."""
    return CapabilityAvailability.from_wire(payload)


def _safe_correlation_id(payload: object) -> str | None:
    if type(payload) in (dict, MappingProxyType):
        value = cast(Mapping[str, Any], payload).get("correlationId")
        if (
            type(value) is str
            and len(value) <= 128
            and _IDENTIFIER_RE.fullmatch(value) is not None
        ):
            return value
    return None


# --------------------------------------------------------------------------
# Availability and health
# --------------------------------------------------------------------------


def is_available(availability: CapabilityAvailability) -> bool:
    """Return whether the capability may be used.

    Only the ``available`` state is available. Every other governed state --
    including ``temporarily_unhealthy`` -- is a refusal, and an unknown state
    never reaches here because decoding fails closed first.
    """
    return availability.state == "available"


def retry_after_seconds(availability: CapabilityAvailability) -> int | None:
    """Return the minimum wait the host asked for, if it stated one."""
    return availability.retry_after_seconds


# --------------------------------------------------------------------------
# Trusted-host authority boundary
# --------------------------------------------------------------------------


class HostAuthorityError(Exception):
    """Raised when a value would establish or carry authority it is not entitled to.

    This is deliberately not a decode error. The values involved are
    structurally valid Host Contract records; what fails is the trust rule
    about *where* authority may come from and *how* it may change.
    """


#: Authority spellings a hosted payload may never use to establish authority.
#: Transcribed verbatim from the governed migration fixture's
#: ``prohibitedSynthesis`` list.
PROHIBITED_PAYLOAD_AUTHORITY_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "principalRef",
        "organisationRef",
        "workspaceRef",
        "permissionSummaryRef",
        "entitlementSummaryRef",
        "environmentId",
    }
)

#: The ShellContext fields the specification names as rotation triggers: a
#: Workspace, principal, entitlement, permission, runtime or environment change.
#: This is a reporting aid, not the rotation rule -- the record is immutable, so
#: :func:`validate_context_rotation` requires a new ID for *any* difference.
CONTEXT_AUTHORITY_FIELDS: Final[tuple[str, ...]] = (
    "principalRef",
    "workspaceRef",
    "entitlementSummaryRef",
    "permissionSummaryRef",
    "runtimeId",
    "environmentId",
)


def payload_authority_claims(request: HostRequest) -> tuple[str, ...]:
    """Return the prohibited authority spellings the request's payload contains.

    A non-empty result does not make the envelope invalid: the canonical schema
    accepts it, and the trusted Host Adapter ignores the fields and audits the
    attempt. This function is how an adapter sees the attempt in order to audit
    it -- never how it reads authority.
    """
    if type(request) is not HostRequest:
        raise HostAuthorityError("authority audit requires an exact HostRequest")
    found: set[str] = set()
    _collect_payload_authority_claims(request.payload, found, set())
    return tuple(sorted(found))


def _collect_payload_authority_claims(
    value: object, found: set[str], seen: set[int]
) -> None:
    """Walk JSON containers without invoking subclass hooks or echoing values."""
    if type(value) in (dict, MappingProxyType):
        identity = id(value)
        if identity in seen:
            return
        seen.add(identity)
        for key, nested in cast(Mapping[object, object], value).items():
            if type(key) is not str:
                raise HostAuthorityError("authority audit found a malformed payload container")
            if key in PROHIBITED_PAYLOAD_AUTHORITY_FIELDS:
                found.add(key)
            _collect_payload_authority_claims(nested, found, seen)
        return
    if type(value) in (list, tuple):
        identity = id(value)
        if identity in seen:
            return
        seen.add(identity)
        for nested in cast(tuple[object, ...] | list[object], value):
            _collect_payload_authority_claims(nested, found, seen)
        return
    if value is None or type(value) in (str, bool, int, float):
        return
    raise HostAuthorityError("authority audit found a malformed payload container")


def resolve_trusted_context(
    request: HostRequest, context: ShellContext, *, now: datetime | None = None
) -> ShellContext:
    """Return the trusted context this request runs under.

    ``context`` must be the record the trusted host holds for the request's
    ``contextId``; a mismatch is refused rather than reconciled. The request's
    ``payload`` is not consulted at all, so a hosted artefact cannot widen its
    own authority by embedding principal, Workspace, permission, entitlement or
    environment values in it.

    ``expiresAt`` is the record's own statement of when it stops being
    authority, so it is enforced here rather than left to the caller: a context
    whose expiry is at or before the moment of the check resolves to nothing.
    The boundary is exclusive of the instant itself because the field names the
    first instant the record is no longer valid, not the last instant it is.

    ``now`` names the instant the check is made at. It defaults to the current
    UTC instant and must be time-zone aware, so a caller pinning it for a
    deterministic check cannot silently compare against a floating local clock.
    """
    _require_valid_trust_request(request)
    _require_valid_trust_context(context)
    if context.context_id != request.context_id:
        raise HostAuthorityError("trusted context does not match the request context")
    if _has_expired(context, _authority_instant(now)):
        raise HostAuthorityError("trusted context has expired")
    return context


def _authority_instant(now: datetime | None) -> datetime:
    """Return the instant an authority check is made at, refusing a floating clock."""
    if now is None:
        return datetime.now(UTC)
    if type(now) is not datetime or now.utcoffset() is None:
        raise HostAuthorityError("authority check requires a time-zone-aware instant")
    return now


def _has_expired(context: ShellContext, now: datetime) -> bool:
    """Return whether the context's own ``expiresAt`` has been reached.

    An absent ``expiresAt`` states no expiry, which is the record saying its
    validity ends with the context itself rather than with a clock. An
    ``expiresAt`` this build cannot read as an instant is not treated as
    open-ended: the record has already passed schema validation, so a value
    that still will not resolve is a record this build cannot reason about, and
    it is refused like any other malformed trusted record.
    """
    expires_at = context.expires_at
    if expires_at is None:
        return False
    text = f"{expires_at[:-1]}+00:00" if expires_at[-1:] in ("Z", "z") else expires_at
    # RFC 3339 permits the leap second the schema validates against ``:59``;
    # ``fromisoformat`` does not accept it, so it is read the same way here.
    text = _LEAP_SECOND_RE.sub(":59", text)
    try:
        expiry = datetime.fromisoformat(text)
    except ValueError:
        raise HostAuthorityError("invalid trusted record") from None
    if expiry.utcoffset() is None:
        raise HostAuthorityError("invalid trusted record")
    return expiry <= now


#: The seconds field of a governed RFC 3339 timestamp, when it is the leap
#: second. Minute and time-zone-offset minute fields are ``[0-5]\d``, so ``:60``
#: cannot be either of them.
_LEAP_SECOND_RE: Final[re.Pattern[str]] = re.compile(r":60(?=(?:\.\d+)?[+-]\d{2}:\d{2}$)")


def _require_valid_trust_request(request: HostRequest) -> None:
    if type(request) is not HostRequest:
        raise HostAuthorityError("invalid trusted record")
    try:
        request.validate()
    except HostContractDecodeError:
        raise HostAuthorityError("invalid trusted record") from None
    if any(
        type(value) is not str
        for value in (
            request.contract_version,
            request.request_id,
            request.namespace,
            request.operation,
            request.context_id,
        )
    ):
        raise HostAuthorityError("invalid trusted record")


def _require_valid_trust_context(context: ShellContext) -> None:
    if type(context) is not ShellContext:
        raise HostAuthorityError("invalid trusted record")
    try:
        context.validate()
    except HostContractDecodeError:
        raise HostAuthorityError("invalid trusted record") from None
    string_values = (
        context.context_id,
        context.principal_ref,
        context.organisation_ref,
        context.workspace_ref,
        context.entitlement_summary_ref,
        context.permission_summary_ref,
        context.runtime_id,
        context.environment_id,
        context.target,
        context.issued_at,
    )
    if any(type(value) is not str for value in string_values) or type(
        context.context_version
    ) is not int:
        raise HostAuthorityError("invalid trusted record")
    if context.expires_at is not None and type(context.expires_at) is not str:
        raise HostAuthorityError("invalid trusted record")


def changed_authority_fields(old: ShellContext, new: ShellContext) -> tuple[str, ...]:
    """Return which governed rotation triggers differ between two contexts."""
    _require_valid_trust_context(old)
    _require_valid_trust_context(new)
    return tuple(
        name
        for name in CONTEXT_AUTHORITY_FIELDS
        if getattr(old, _ATTRIBUTES[name]) != getattr(new, _ATTRIBUTES[name])
    )


_ATTRIBUTES: Final[Mapping[str, str]] = {
    "principalRef": "principal_ref",
    "workspaceRef": "workspace_ref",
    "entitlementSummaryRef": "entitlement_summary_ref",
    "permissionSummaryRef": "permission_summary_ref",
    "runtimeId": "runtime_id",
    "environmentId": "environment_id",
}


def validate_context_rotation(old: ShellContext, new: ShellContext) -> None:
    """Raise unless ``new`` is a lawful successor to ``old``.

    The record is immutable, so *any* difference makes ``new`` a different
    record: it must carry a new ``contextId`` and a higher ``contextVersion``.
    Re-presenting an identical record under the same ID is not a change and is
    accepted. Handles bound to the superseded ID stop being valid -- see
    :func:`is_handle_valid`.
    """
    _require_valid_trust_context(old)
    _require_valid_trust_context(new)
    if old.to_wire() == new.to_wire():
        return
    if old.context_id == new.context_id:
        raise HostAuthorityError("ShellContext changed without a new contextId")
    if new.context_version <= old.context_version:
        raise HostAuthorityError("ShellContext must advance contextVersion")


def is_handle_valid(
    current: ShellContext, handle_context_id: str, *, now: datetime | None = None
) -> bool:
    """Return whether a handle issued against ``handle_context_id`` is still usable.

    A handle bound to a superseded context is invalid: rotation exists precisely
    so authority tied to the old context stops being honoured. An expired
    context supersedes itself, so no handle is usable against one, including a
    handle naming its own ``contextId`` -- expiry that a still-held handle could
    outlive would not be expiry at all.

    ``now`` names the instant the check is made at, exactly as in
    :func:`resolve_trusted_context`.
    """
    _require_valid_trust_context(current)
    if _has_expired(current, _authority_instant(now)):
        return False
    if type(handle_context_id) is not str or _IDENTIFIER_RE.fullmatch(handle_context_id) is None:
        return False
    return handle_context_id == current.context_id
