"""The capability gateway: from a proposed action to an authorized invocation (RT-204).

This is the seam a run crosses before anything it proposes can reach the world. It
answers one question -- *may this run invoke this capability, through this binding,
right now* -- and it answers it from records that already exist: the run's pinned
`PolicySnapshot`, the `CapabilityGrant` that policy backs, the `Approval` recorded for
a wait, and the `EvidenceItem`s the runtime itself retained.

Deliberately *pure*. Nothing here opens a database, holds an adapter, calls a registry,
reads a clock or touches a transport. The binding inventory is an argument, the instant
is an argument, and the persisted records are arguments. That is what makes every rule
below testable as a rule rather than as a fixture, and it is why the module can be read
end to end to see what authority actually rests on.

What it deliberately does **not** decide, because no accepted contract states it:

* *There is no ActionProposal wire record.* :class:`ActionProposal` is a local,
  in-process value with no `to_wire`/`from_wire` and no schema. A proposal carries the
  `request_digest` of the bytes it will send, never the bytes; nothing here decodes
  caller arguments, so no second canonical decode seam is created by the back door.
* *There is no capability catalogue here.* The set of capabilities that exist is the
  pinned policy's `granted_capabilities`, read through
  :func:`validate_capability_grant`; this module publishes no capability list of its own.
* *There is no approval-to-grant relationship, and no effect-class approval map.* The
  accepted contract gives an `Approval` no field naming a grant it authorised, and it
  nowhere states which effect classes require one. So the gateway neither invents a
  requirement nor waives one: an approval that is *supplied* must actually authorize --
  decided, approved, in-window, for this run -- and a caller that supplies none is
  making a claim this module cannot check, which is stated here and asserted in the
  RT-204 tests rather than silently assumed either way.
* *There is no capability-version pinning and no cross-domain version mapping.* A
  proposal states a `ContractVersion` floor and a candidate states the `ContractVersion`
  it implements; both are compared with :func:`compare_contract_versions`, in that one
  version domain. No `ReleaseVersion`, adapter version or module version is translated
  into it.

`EffectClass` is the Host Contract's own closed vocabulary, reused verbatim through
`HOST_OPERATION_EFFECT_CLASSES` exactly as `omnivia_core.control_plane.effects` reuses
it. This module defines no second taxonomy of consequence.

Refusals carry a category and no value. Every message is one of the frozen `_MESSAGE_*`
constants below, so no scope, purpose, capability id, binding id, digest, token or
caller string can be republished through a log line, an audit record or an `ApiError`.
The codes are the application contract's own error vocabulary; no new wire surface is
added and the frozen operation catalogue is untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol

from omnivia_core.contracts.v1 import (
    APPROVAL_DECISION_APPROVED,
    CAPABILITY_ID_PATTERN,
    CONTENT_CHECKSUM_PATTERN,
    CONTRACT_VERSION_PATTERN,
    DEFAULT_RETRY_CLASSIFICATION,
    ERROR_CODE_AUTHORIZATION_DENIED,
    ERROR_CODE_CAPABILITY_NOT_GRANTED,
    ERROR_CODE_CONFLICT,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_PURPOSE,
    ERROR_CODE_INVALID_REQUEST,
    IDEMPOTENCY_KEY_PATTERN,
    IDENTIFIER_PATTERN,
    PURPOSE_PATTERN,
    RETRY_CLASS_NON_RETRYABLE,
    SCOPE_PATTERN,
    WORKSPACE_ID_PATTERN,
    ApiError,
    Approval,
    CapabilityGrant,
    ContractSemanticError,
    EvidenceItem,
    PolicySnapshot,
    compare_contract_versions,
    is_authoritative_source,
    permits_new_effect,
    validate_approval,
    validate_capability_grant,
    validate_evidence_item,
)
from omnivia_core.host_contract.v1 import HOST_OPERATION_EFFECT_CLASSES


class CapabilityGatewayRefusal(Exception):
    """One refusal from the gateway or from a resolver, in the contract's vocabulary.

    The code and the retry class are the contract; the message is not. The retry class
    is read from the frozen classification rather than chosen here, so a refusal cannot
    advertise a retry posture the taxonomy does not give it, and an unclassified code
    fails safe as non-retryable.

    One class rather than a hierarchy. A caller that must not act needs to know *that*
    and in which category; splitting resolution refusals from authorization refusals
    would invite a handler that catches one and proceeds, which is exactly the shape of
    mistake a fail-closed gate exists to prevent.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_class = DEFAULT_RETRY_CLASSIFICATION.get(
            code, RETRY_CLASS_NON_RETRYABLE
        )

    def as_api_error(self) -> ApiError:
        """This refusal as the contract's own error DTO."""
        return ApiError(
            code=self.code, message=self.message, retry_class=self.retry_class
        )


# --- refusal messages ---------------------------------------------------------
#
# Frozen here rather than built at the raise site, for the same reason the application
# authorization seam freezes its own: a refusal produced from caller-controlled input is
# rendered into logs, audit records and a wire error, so anything interpolated into one
# is republished everywhere those go. The whole set a caller may ever see is enumerable.

_MESSAGE_MALFORMED_PROPOSAL: Final = (
    "the proposed action is not a well-formed action proposal"
)
_MESSAGE_UNCANONICAL_VALUE: Final = "the proposed action states a value outside the contract's value domain for its field"
_MESSAGE_LIST_TOO_LONG: Final = (
    "the proposed action states more list entries than are permitted"
)
_MESSAGE_INSTANT_NOT_USABLE: Final = (
    "the authorization instant is not a usable absolute instant"
)
_MESSAGE_AUTHORITY_STATE: Final = (
    "this service's runtime authority state cannot be applied"
)
_MESSAGE_INVENTORY_STATE: Final = "this service's binding inventory cannot be applied"
_MESSAGE_RUN_MAY_NOT_ACT: Final = "the run is not in a status that permits a new effect"
_MESSAGE_RUN_MISMATCH: Final = (
    "the proposed action names a run or workspace this authority is not for"
)
_MESSAGE_EFFECT_CLASS_UNKNOWN: Final = (
    "the proposed action states an effect class this build cannot apply"
)
_MESSAGE_EFFECT_CLASS_NOT_PERMITTED: Final = (
    "the run may not act in the effect class this action states"
)
_MESSAGE_GRANT_NOT_POLICY_BACKED: Final = (
    "no grant this run's pinned policy backs authorizes this action"
)
_MESSAGE_GRANT_NOT_IN_WINDOW: Final = (
    "the grant is outside the window it was issued for"
)
_MESSAGE_NO_BINDING: Final = "no approved binding satisfies the proposed action"
_MESSAGE_BINDING_AMBIGUOUS: Final = (
    "more than one approved binding satisfies the proposed action"
)
_MESSAGE_BINDING_MISMATCH: Final = (
    "the resolved binding is not the binding this action was authorized for"
)
_MESSAGE_SCOPES_NOT_GRANTED: Final = (
    "the proposed action requests a scope the grant or the binding does not carry"
)
_MESSAGE_PURPOSE_NOT_GRANTED: Final = (
    "the proposed action states a purpose the grant was not issued for"
)
_MESSAGE_APPROVAL_DOES_NOT_AUTHORIZE: Final = (
    "the recorded approval does not authorize this action"
)
_MESSAGE_EVIDENCE_NOT_AUTHORITATIVE: Final = (
    "evidence this action requires is missing, unretained or subordinate"
)


# --- value domains -------------------------------------------------------------
#
# The published wire patterns are used by reference, never re-spelled. Their schema
# `minLength`/`maxLength` bounds are not published, so the shared bound is restated as a
# constant: a bound is load-bearing on its own, since a value can match its pattern at
# any length at all.

_MAX_VALUE_LENGTH: Final = 128
"""The shared schema `maxLength` of every bounded scalar this seam reads."""

_CAPABILITY_ID_MIN_LENGTH: Final = 3
"""`CapabilityId`'s schema `minLength`: a namespace, a dot and a name at minimum."""

_CONTRACT_VERSION_MAX_LENGTH: Final = 32
"""`ContractVersion`'s own, shorter, schema `maxLength`."""

_CONTENT_CHECKSUM_LENGTH: Final = 71
"""`sha256:` and sixty-four lowercase hex digits: the only length the pattern admits."""

_MAX_LIST_ITEMS: Final = 64
"""The schema `maxItems` shared by the scope and identifier lists read here."""

_IDENTIFIER_RE: Final = re.compile(IDENTIFIER_PATTERN)
_WORKSPACE_ID_RE: Final = re.compile(WORKSPACE_ID_PATTERN)
_CAPABILITY_ID_RE: Final = re.compile(CAPABILITY_ID_PATTERN)
_CONTRACT_VERSION_RE: Final = re.compile(CONTRACT_VERSION_PATTERN)
_SCOPE_RE: Final = re.compile(SCOPE_PATTERN)
_PURPOSE_RE: Final = re.compile(PURPOSE_PATTERN)
_IDEMPOTENCY_KEY_RE: Final = re.compile(IDEMPOTENCY_KEY_PATTERN)
_CONTENT_CHECKSUM_RE: Final = re.compile(CONTENT_CHECKSUM_PATTERN)

#: Credential shapes refused wherever they appear, whatever domain admits them. The
#: detectors are the conservative raw-byte ones the accepted Release Package scan is
#: pinned to, spelled for text: each needs structure a benign lookalike does not have.
#:
#: This is a second lock, not a duplicate of the domain check. `Identifier` admits
#: `[A-Za-z0-9._:-]`, which is every character of a JWT, an AWS access key id and a
#: Slack token -- so a canonical value domain is exactly where a credential hides. A
#: value that reaches here is copied into an `AuthorizedInvocation` and from there into
#: whatever records the invocation, so admitting one publishes it.
_SECRET_SHAPES: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9]{8,}-[A-Za-z0-9-]{8,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"\b[sr]k_(?:live|test)_[0-9A-Za-z]{16,}\b"),
    re.compile(
        r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"
    ),
)


def _usable(
    value: object,
    pattern: re.Pattern[str],
    *,
    minimum_length: int = 1,
    maximum_length: int = _MAX_VALUE_LENGTH,
) -> bool:
    """Whether `value` is a string this seam may read, decide from, and carry forward.

    Type, length, spelling and credential shape together, because any one of them alone
    lets something through. A non-string reaches here from a hand-built dataclass; an
    unbounded string matches most of these patterns at any length; a bounded string of
    the wrong shape is not the value the contract names; and a perfectly canonical
    string can still be somebody's token.

    `fullmatch`, never `match`. Every published pattern ends in `$`, which in Python
    matches immediately before a final newline, so a value spelled with a trailing
    newline would otherwise be accepted here while strict schema validation refuses it.
    Control characters are refused by the same call: no pattern this module reads admits
    one anywhere in a value.
    """
    return (
        isinstance(value, str)
        and minimum_length <= len(value) <= maximum_length
        and pattern.fullmatch(value) is not None
        and not any(shape.search(value) for shape in _SECRET_SHAPES)
    )


def _usable_list(values: object, pattern: re.Pattern[str]) -> bool:
    """Whether `values` is a bounded tuple of strings every one of which is usable.

    A tuple, not any sequence: a bare string is a sequence of characters, and reading
    `"admin"` as five one-character scopes is not what a caller passing it meant.
    Cardinality is part of the domain -- an unbounded list is a caller choosing how much
    state this seam carries into the invocation it authorizes.
    """
    return (
        type(values) is tuple
        and len(values) <= _MAX_LIST_ITEMS
        and all(_usable(value, pattern) for value in values)
    )


def _instant(value: object) -> datetime | None:
    """`value` as an absolute instant, or `None` if it is not one.

    Timezone-aware only. A naive timestamp cannot be ordered against another instant
    without assuming an offset, and a gate that guessed one would compare a grant's
    expiry against a wall clock in an unknown zone.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


# --- the values this seam decides over -----------------------------------------


@dataclass(frozen=True, slots=True)
class ActionProposal:
    """What a run proposes to do, before anything has been decided about it.

    A local value, not a wire record: it has no schema, no `to_wire` and no `from_wire`,
    and RT-204 adds none. Everything on it is either a canonical scalar the contract
    already defines or a digest of bytes this module never sees. `request_digest` is the
    checksum of the request the invocation will carry -- the arguments themselves stay
    with the caller, so no decoding of caller-supplied payloads happens at this seam.

    `minimum_version` is a `ContractVersion` floor, not a pin: the resolver selects the
    highest binding that satisfies it. `required_evidence_ids` names evidence this
    action must already be grounded in; the accepted contract states no rule making
    evidence mandatory for any particular effect class, so an empty tuple requires
    nothing and states nothing.
    """

    workspace_id: str
    run_id: str
    capability_id: str
    minimum_version: str
    effect_class: str
    purpose: str
    requested_scopes: tuple[str, ...]
    idempotency_key: str
    request_digest: str
    required_evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BindingCandidate:
    """One binding the service holds, as the service knows it.

    A local trust input, not a wire record, and not an adapter handle: there is no
    endpoint, process, socket or credential here. What a candidate carries is what a
    decision can be taken from -- who supplies it, what it implements, where it is
    placed, and what the service established about it.

    Every boolean defaults to the refusing value and `discovery_only` defaults to
    `True`, so a candidate assembled from partial information authorizes nothing.
    Discovery is not authority: a binding the service merely found is a candidate for
    approval, never a binding to activate.
    """

    binding_id: str
    source_id: str
    capability_id: str
    version: str
    effect_class: str
    scopes: tuple[str, ...]
    workspace_id: str
    installation_id: str
    approved: bool = False
    healthy: bool = False
    trusted: bool = False
    discovery_only: bool = True


@dataclass(frozen=True, slots=True)
class RuntimeAuthority:
    """What the *service* established about a run, before the run proposed anything.

    Every field is a server-side fact read from a persisted record: the run's status,
    the `PolicySnapshot` it is pinned to, the `CapabilityGrant` that policy backs, the
    `Approval` recorded for a wait, and the evidence the runtime retained. Nothing a
    proposal says can add to any of it.

    `permitted_effect_classes` defaults to empty, so an under-specified authority
    refuses every action rather than permitting a read. It is an explicit allowlist the
    service holds -- not a map from effect class to approval requirement, which no
    accepted contract states and this module does not invent.
    """

    workspace_id: str
    run_id: str
    installation_id: str
    run_status: str
    policy: PolicySnapshot
    grant: CapabilityGrant
    permitted_effect_classes: frozenset[str] = frozenset()
    approval: Approval | None = None
    evidence: tuple[EvidenceItem, ...] = ()


@dataclass(frozen=True, slots=True)
class AuthorizedInvocation:
    """The single output of a successful authorization: what may now be invoked.

    Every field is either a server fact or a proposal fact the gateway has proved. It
    names the binding by identity and version and the authority by grant and policy, so
    whatever performs the invocation and whatever records it are reading the same
    decision rather than re-deriving one.

    There is no adapter handle, endpoint or credential here, and there must never be
    one. This value is passed on, logged and correlated; authority is what it carries,
    and the means of acting is dispatch's to hold.
    """

    workspace_id: str
    run_id: str
    capability_id: str
    binding_id: str
    binding_source_id: str
    binding_version: str
    effect_class: str
    scopes: tuple[str, ...]
    purpose: str
    capability_grant_id: str
    policy_snapshot_id: str
    idempotency_key: str
    request_digest: str
    evidence_item_ids: tuple[str, ...]
    authorized_at: datetime
    approval_id: str | None = None


class BindingResolver(Protocol):
    """Turns a proposal into the one binding that may serve it, or refuses.

    A resolver decides *which* binding, never *whether* the run may act: it is consulted
    from inside :func:`authorize_invocation`, which checks the authority both before and
    after it. A resolver that returned something unapproved would still be refused.
    """

    def resolve(
        self, proposal: ActionProposal, *, authority: RuntimeAuthority
    ) -> BindingCandidate: ...


@dataclass(frozen=True, slots=True)
class DeterministicBindingResolver:
    """Selects one binding from a fixed inventory, by rule, with no lookup anywhere.

    The inventory is supplied whole. Nothing is read from a registry, a database or a
    process, so the same inventory and the same proposal always select the same binding
    -- which is what makes the selection auditable rather than merely repeatable today.

    The rules, in order:

    1. Every candidate must be well formed. A malformed inventory is a fault in this
       service, not something the run did, and resolution stops rather than selecting
       from the part of it that happens to parse.
    2. Only candidates for the proposed capability are considered.
    3. Discovery-only, unapproved, unhealthy, untrusted and misplaced candidates are
       excluded -- before selection, so a discovered binding can never be activated and
       can never even make an approved one look ambiguous.
    4. Only candidates at or above the proposal's version floor survive.
    5. The highest surviving version wins.
    6. Two distinct candidates at that same highest version are an ambiguity, not a
       coin toss: preferring either would make authority depend on inventory order.
    """

    candidates: tuple[BindingCandidate, ...] = ()

    def resolve(
        self, proposal: ActionProposal, *, authority: RuntimeAuthority
    ) -> BindingCandidate:
        """The one binding that serves `proposal`, or a refusal."""
        if type(self.candidates) is not tuple:
            raise _refusal(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_INVENTORY_STATE
            )
        for candidate in self.candidates:
            _require_well_formed_candidate(candidate)

        eligible = [
            candidate
            for candidate in self.candidates
            if candidate.capability_id == proposal.capability_id
            and candidate.approved
            and not candidate.discovery_only
            and candidate.healthy
            and candidate.trusted
            and candidate.workspace_id == authority.workspace_id
            and candidate.installation_id == authority.installation_id
            and compare_contract_versions(candidate.version, proposal.minimum_version)
            >= 0
        ]
        if not eligible:
            raise _refusal(ERROR_CODE_CAPABILITY_NOT_GRANTED, _MESSAGE_NO_BINDING)

        best = eligible[0]
        for candidate in eligible[1:]:
            if compare_contract_versions(candidate.version, best.version) > 0:
                best = candidate
        tied = {
            (candidate.binding_id, candidate.source_id)
            for candidate in eligible
            if compare_contract_versions(candidate.version, best.version) == 0
        }
        if len(tied) > 1:
            raise _refusal(ERROR_CODE_CONFLICT, _MESSAGE_BINDING_AMBIGUOUS)
        return best


def _refusal(code: str, message: str) -> CapabilityGatewayRefusal:
    return CapabilityGatewayRefusal(code, message)


def _denied(message: str) -> CapabilityGatewayRefusal:
    return CapabilityGatewayRefusal(ERROR_CODE_AUTHORIZATION_DENIED, message)


def _require_well_formed_candidate(candidate: object) -> None:
    """Refuse resolution unless `candidate` is a binding this service can decide from.

    Reported as unusable service state rather than as something the run was not granted:
    a malformed inventory row is this build's fault, and telling a caller it lacks a
    grant would send it off to ask for a wider one that could never help.
    """
    if type(candidate) is not BindingCandidate:
        raise _refusal(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_INVENTORY_STATE)
    well_formed = (
        _usable(candidate.binding_id, _IDENTIFIER_RE)
        and _usable(candidate.source_id, _IDENTIFIER_RE)
        and _usable(
            candidate.capability_id,
            _CAPABILITY_ID_RE,
            minimum_length=_CAPABILITY_ID_MIN_LENGTH,
        )
        and _usable(
            candidate.version,
            _CONTRACT_VERSION_RE,
            maximum_length=_CONTRACT_VERSION_MAX_LENGTH,
        )
        and candidate.effect_class in HOST_OPERATION_EFFECT_CLASSES
        and _usable_list(candidate.scopes, _SCOPE_RE)
        and _usable(candidate.workspace_id, _WORKSPACE_ID_RE)
        and _usable(candidate.installation_id, _IDENTIFIER_RE)
        and all(
            type(flag) is bool
            for flag in (
                candidate.approved,
                candidate.healthy,
                candidate.trusted,
                candidate.discovery_only,
            )
        )
    )
    if not well_formed:
        raise _refusal(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_INVENTORY_STATE)


def _require_well_formed_proposal(proposal: object) -> ActionProposal:
    """Refuse unless `proposal` is a proposal in every value domain it states.

    Shape first, then values. Structural typing proves a capability id is a string,
    never that it is a `CapabilityId`; and `purpose="\\x07"`, a 4096-character scope and
    a `request_digest` that is not a checksum are all perfectly typed and none of them is
    a value any accepted contract admits.

    At least one requested scope, always. A proposal requesting no scope makes the scope
    check below vacuous, and an action authorized against nothing is not an authorized
    action.
    """
    if type(proposal) is not ActionProposal:
        raise _refusal(ERROR_CODE_INVALID_REQUEST, _MESSAGE_MALFORMED_PROPOSAL)
    for values in (proposal.requested_scopes, proposal.required_evidence_ids):
        if type(values) is tuple and len(values) > _MAX_LIST_ITEMS:
            raise _refusal(ERROR_CODE_INVALID_REQUEST, _MESSAGE_LIST_TOO_LONG)
    canonical = (
        _usable(proposal.workspace_id, _WORKSPACE_ID_RE)
        and _usable(proposal.run_id, _IDENTIFIER_RE)
        and _usable(
            proposal.capability_id,
            _CAPABILITY_ID_RE,
            minimum_length=_CAPABILITY_ID_MIN_LENGTH,
        )
        and _usable(
            proposal.minimum_version,
            _CONTRACT_VERSION_RE,
            maximum_length=_CONTRACT_VERSION_MAX_LENGTH,
        )
        and _usable(proposal.purpose, _PURPOSE_RE)
        and _usable_list(proposal.requested_scopes, _SCOPE_RE)
        and len(proposal.requested_scopes) > 0
        and _usable(proposal.idempotency_key, _IDEMPOTENCY_KEY_RE)
        and _usable(
            proposal.request_digest,
            _CONTENT_CHECKSUM_RE,
            minimum_length=_CONTENT_CHECKSUM_LENGTH,
            maximum_length=_CONTENT_CHECKSUM_LENGTH,
        )
        and _usable_list(proposal.required_evidence_ids, _IDENTIFIER_RE)
    )
    if not canonical:
        raise _refusal(ERROR_CODE_INVALID_REQUEST, _MESSAGE_UNCANONICAL_VALUE)
    return proposal


def _require_usable_authority(authority: object) -> RuntimeAuthority:
    """Refuse unless `authority` is service state a decision can be taken from.

    The persisted records are checked by the contract's own validators further down,
    where the trusted context they need is available. What is checked here is that this
    is an authority at all: the right type, canonical identifiers, and an effect-class
    allowlist that is a set of classes this build knows.
    """
    if type(authority) is not RuntimeAuthority:
        raise _refusal(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_AUTHORITY_STATE)
    usable = (
        _usable(authority.workspace_id, _WORKSPACE_ID_RE)
        and _usable(authority.run_id, _IDENTIFIER_RE)
        and _usable(authority.installation_id, _IDENTIFIER_RE)
        and isinstance(authority.run_status, str)
        and type(authority.policy) is PolicySnapshot
        and type(authority.grant) is CapabilityGrant
        and type(authority.permitted_effect_classes) is frozenset
        and authority.permitted_effect_classes
        <= frozenset(HOST_OPERATION_EFFECT_CLASSES)
        and (authority.approval is None or type(authority.approval) is Approval)
        and type(authority.evidence) is tuple
        and all(type(item) is EvidenceItem for item in authority.evidence)
    )
    if not usable:
        raise _refusal(ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_AUTHORITY_STATE)
    return authority


def _require_policy_backed_grant(
    authority: RuntimeAuthority, proposal: ActionProposal
) -> None:
    """Refuse unless the run holds a grant this run's pinned policy actually backs.

    Delegated whole to :func:`validate_capability_grant`, which is where *discovery is
    not authority* is already a refusal: a capability that appears only in the policy's
    `discovered_capabilities`, or one narrowed out of the granted set since, backs
    nothing. That rule is not restated here and so cannot drift from it.

    Two things that validator cannot decide are decided here. It is handed the policy as
    trusted context and never compares it with the `policy_snapshot_id` the grant names,
    so a grant naming a *different* snapshot would be checked against a policy it does
    not name -- a check of nothing. And a grant is for one capability, which is not
    necessarily the one proposed.

    The contract's refusal quotes the capability it refused, so it is dropped rather
    than chained -- recorded in a sentinel and answered after the handler exits, because
    `raise ... from None` would still leave the quoted value on `__context__`.
    """
    backed = True
    try:
        validate_capability_grant(
            authority.grant,
            run_id=authority.run_id,
            workspace_id=authority.workspace_id,
            policy=authority.policy,
        )
    except ContractSemanticError:
        backed = False
    if (
        not backed
        or authority.grant.policy_snapshot_id != authority.policy.policy_snapshot_id
        or authority.grant.capability_id != proposal.capability_id
    ):
        raise _refusal(
            ERROR_CODE_CAPABILITY_NOT_GRANTED, _MESSAGE_GRANT_NOT_POLICY_BACKED
        )


def _require_grant_in_window(grant: CapabilityGrant, now: datetime) -> None:
    """Refuse unless `now` falls inside the window the grant was issued for.

    Both ends. A grant that has expired is the obvious half; a grant whose `granted_at`
    is still ahead of `now` is the other, and admitting one would let a grant issued for
    later authorize something now.
    """
    granted_at = _instant(grant.granted_at)
    if granted_at is None or now < granted_at:
        raise _refusal(ERROR_CODE_CAPABILITY_NOT_GRANTED, _MESSAGE_GRANT_NOT_IN_WINDOW)
    if grant.expires_at is not None:
        expires_at = _instant(grant.expires_at)
        if expires_at is None or now >= expires_at:
            raise _refusal(
                ERROR_CODE_CAPABILITY_NOT_GRANTED, _MESSAGE_GRANT_NOT_IN_WINDOW
            )


def _require_matching_binding(
    binding: object, proposal: ActionProposal, authority: RuntimeAuthority
) -> BindingCandidate:
    """Refuse unless what a resolver returned is the binding this action was proved for.

    A resolver is an argument, so what it returns is not a fact until it is checked. The
    same rules the deterministic resolver applies are applied again here to whatever
    comes back -- identity, version floor, placement, approval, health, trust, and the
    effect class the proposal declared -- so a resolver cannot widen authority by
    returning a discovered, misplaced or more consequential binding than was asked for.
    """
    _require_well_formed_candidate(binding)
    assert isinstance(binding, BindingCandidate)
    if (
        binding.capability_id != proposal.capability_id
        or compare_contract_versions(binding.version, proposal.minimum_version) < 0
        or binding.effect_class != proposal.effect_class
        or binding.workspace_id != authority.workspace_id
        or binding.installation_id != authority.installation_id
        or not binding.approved
        or binding.discovery_only
        or not binding.healthy
        or not binding.trusted
    ):
        raise _denied(_MESSAGE_BINDING_MISMATCH)
    return binding


def _require_approval_authorizes(
    approval: Approval | None, authority: RuntimeAuthority
) -> str | None:
    """The id of an approval that authorizes, `None` when none was supplied, or a refusal.

    The accepted contract records an approval against a `Wait` and gives it no field
    naming a grant or an effect class, and it nowhere states which actions require one.
    So this decides only what the contract does state: an approval that exists must be
    for this run, well formed, actually decided, decided *approved*, and decided before
    the deadline the request set. A rejected, pending, expired or foreign approval
    authorizes nothing.

    A caller supplying no approval is making a claim this module cannot check. It is
    neither treated as an approval nor as a refusal, and the ambiguity is deliberately
    left visible here rather than closed by an invented effect-class rule.
    """
    if approval is None:
        return None
    well_formed = True
    try:
        validate_approval(
            approval, run_id=authority.run_id, workspace_id=authority.workspace_id
        )
    except ContractSemanticError:
        well_formed = False
    if not well_formed or approval.decision != APPROVAL_DECISION_APPROVED:
        raise _denied(_MESSAGE_APPROVAL_DOES_NOT_AUTHORIZE)
    decided_at = _instant(approval.decided_at)
    if decided_at is None:
        raise _denied(_MESSAGE_APPROVAL_DOES_NOT_AUTHORIZE)
    if approval.expires_at is not None:
        expires_at = _instant(approval.expires_at)
        if expires_at is None or decided_at > expires_at:
            raise _denied(_MESSAGE_APPROVAL_DOES_NOT_AUTHORIZE)
    return approval.approval_id


def _require_retained_evidence(
    proposal: ActionProposal, authority: RuntimeAuthority
) -> tuple[str, ...]:
    """The evidence ids this action requires, each proved authoritative and retained.

    Only evidence the runtime itself recorded may be authority: an external log, an
    agent-lane ledger and a control-plane projection corroborate the runtime's record
    and never replace it. Retention is the second half -- evidence that was captured and
    then released cannot ground anything at the moment of acting.

    An evidence id the authority does not hold is a refusal rather than a skip, and an
    id the authority holds twice is unusable service state: which of two records with
    one id was meant is not a question this seam may answer by taking the first.
    """
    if not proposal.required_evidence_ids:
        return ()
    held: dict[str, EvidenceItem] = {}
    for item in authority.evidence:
        if item.evidence_item_id in held:
            raise _refusal(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE, _MESSAGE_AUTHORITY_STATE
            )
        held[item.evidence_item_id] = item
    for evidence_item_id in proposal.required_evidence_ids:
        required = held.get(evidence_item_id)
        if required is None:
            raise _denied(_MESSAGE_EVIDENCE_NOT_AUTHORITATIVE)
        recorded = True
        try:
            validate_evidence_item(
                required, run_id=authority.run_id, workspace_id=authority.workspace_id
            )
        except ContractSemanticError:
            recorded = False
        if (
            not recorded
            or not required.retained
            or not required.authoritative
            or not is_authoritative_source(required.source.source_kind)
        ):
            raise _denied(_MESSAGE_EVIDENCE_NOT_AUTHORITATIVE)
    return tuple(proposal.required_evidence_ids)


def authorize_invocation(
    proposal: ActionProposal,
    *,
    authority: RuntimeAuthority,
    resolver: BindingResolver,
    now: datetime,
) -> AuthorizedInvocation:
    """Authorize one proposed action, or refuse it in the contract's vocabulary.

    What is decided here is everything a proposal cannot establish about itself:

    1. that the proposal is a proposal -- in shape, in bounds, and in every value domain
       the contract states, with no credential-shaped value anywhere in it;
    2. that the instant to decide against is an absolute instant;
    3. that this service holds usable authority for a run at all;
    4. that the proposal and the authority are for the same run in the same workspace;
    5. that the run's status permits a new effect;
    6. that the effect class is one this build knows and one this run may act in;
    7. that a `CapabilityGrant` this run's pinned policy actually backs covers the
       capability proposed;
    8. that `now` falls inside the window that grant was issued for;
    9. that exactly one approved, healthy, trusted, correctly placed binding satisfies
       the proposal -- and that what came back really is that binding;
    10. that every requested scope is carried by both the grant and the binding;
    11. that the purpose is the one the grant was issued for;
    12. that a supplied approval actually authorizes; and
    13. that every piece of evidence the action requires was recorded by the runtime and
        is retained.

    Order is deliberate. Malformedness is reported as malformedness, before any
    authority question, so a caller is never told it lacks a grant when what it sent was
    not a proposal. This service's own state is checked before the caller is judged by
    it. The grant is proved before a binding is resolved, because resolving a binding for
    a run that holds no grant is work done on behalf of an action that could never
    proceed. Scope, purpose, approval and evidence come last, so the narrowest true
    statement about a refusal is the one that surfaces.

    Fail-closed throughout: every check is a positive proof, and there is no path that
    reaches the return by exhausting the checks that did not apply.
    """
    # 1/2. The proposal, and the instant to decide it against.
    proposal = _require_well_formed_proposal(proposal)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise _refusal(ERROR_CODE_INVALID_REQUEST, _MESSAGE_INSTANT_NOT_USABLE)

    # 3. This service's own state, checked before the caller is judged by it.
    authority = _require_usable_authority(authority)

    # 4. One run, one workspace. Authority for another run is not authority.
    if (
        proposal.workspace_id != authority.workspace_id
        or proposal.run_id != authority.run_id
    ):
        raise _denied(_MESSAGE_RUN_MISMATCH)

    # 5. Status. Only a running run may declare a new effect: an admitted run has not
    # started, a waiting one is suspended, a terminal one is finished, an `uncertain` one
    # owes a reconciliation, and an unrecognized status grants nothing.
    if not permits_new_effect(authority.run_status):
        raise _denied(_MESSAGE_RUN_MAY_NOT_ACT)

    # 6. Effect class: known to this build, then permitted for this run.
    if proposal.effect_class not in HOST_OPERATION_EFFECT_CLASSES:
        raise _refusal(ERROR_CODE_INVALID_REQUEST, _MESSAGE_EFFECT_CLASS_UNKNOWN)
    if proposal.effect_class not in authority.permitted_effect_classes:
        raise _denied(_MESSAGE_EFFECT_CLASS_NOT_PERMITTED)

    # 7/8. The grant, and the window it was issued for.
    _require_policy_backed_grant(authority, proposal)
    _require_grant_in_window(authority.grant, now)

    # 9. The binding. A resolver's answer is checked, never trusted.
    binding = _require_matching_binding(
        resolver.resolve(proposal, authority=authority), proposal, authority
    )

    # 10. Scopes, against both. The grant says what the run was authorized to do; the
    # binding says what this particular binding can act within. A scope missing from
    # either is a scope this action does not have.
    requested = frozenset(proposal.requested_scopes)
    if not (
        requested <= frozenset(authority.grant.scopes)
        and requested <= frozenset(binding.scopes)
    ):
        raise _denied(_MESSAGE_SCOPES_NOT_GRANTED)

    # 11. Purpose. A grant states the one purpose it was issued for, so this is equality
    # rather than membership: acting for a different purpose is acting outside the
    # decision that issued the grant, however narrow the other purpose looks.
    if proposal.purpose != authority.grant.purpose:
        raise _refusal(ERROR_CODE_INVALID_PURPOSE, _MESSAGE_PURPOSE_NOT_GRANTED)

    # 12/13. Approval and evidence.
    approval_id = _require_approval_authorizes(authority.approval, authority)
    evidence_item_ids = _require_retained_evidence(proposal, authority)

    return AuthorizedInvocation(
        workspace_id=authority.workspace_id,
        run_id=authority.run_id,
        capability_id=proposal.capability_id,
        binding_id=binding.binding_id,
        binding_source_id=binding.source_id,
        binding_version=binding.version,
        effect_class=binding.effect_class,
        scopes=tuple(sorted(requested)),
        purpose=authority.grant.purpose,
        capability_grant_id=authority.grant.capability_grant_id,
        policy_snapshot_id=authority.grant.policy_snapshot_id,
        idempotency_key=proposal.idempotency_key,
        request_digest=proposal.request_digest,
        evidence_item_ids=evidence_item_ids,
        authorized_at=now,
        approval_id=approval_id,
    )


__all__ = [
    "ActionProposal",
    "AuthorizedInvocation",
    "BindingCandidate",
    "BindingResolver",
    "CapabilityGatewayRefusal",
    "DeterministicBindingResolver",
    "RuntimeAuthority",
    "authorize_invocation",
]
