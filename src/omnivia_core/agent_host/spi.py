"""The ten-hook agent-host provider SPI and its validated DTOs (V06-8, packet A9-P1).

Standard library only, plus read-only imports from the frozen application
contract in :mod:`omnivia_core.contracts.v1`. This module is the *contract*
side of A9-P1: the hook vocabulary, the versioned identity of the SPI, the
request wrapper, the result and error algebra, and nothing else.

What this module deliberately does not have, because the accepted candidate
forbids it: no operation outside the frozen 20-operation catalogue, no error
code of its own, no run-state type, no job-control composition, no storage
handle, and no widening of :class:`RequestMetadata`.

**The wrapper, not a widened request.** A hook call is an
:class:`SpiRequest`: an SPI-local wrapper carrying ``agent``, ``session``,
``run``, ``sequence`` and -- on turn-scoped hooks only -- ``turn_ordinal``.
Any Core application call a hook composes travels in the frozen
:class:`RequestEnvelope` unchanged. The frozen ``RequestMetadata`` defines no
agent, session, run or turn field and sets ``unevaluatedProperties: false`` at
the contract checkpoint, so an adapter that injects one produces a malformed
envelope. :func:`build_nested_envelope` is the only envelope constructor here
and it is structurally unable to carry a host identifier: it accepts the five
frozen metadata inputs and nothing else.

**The turn coordinate is not a sixth identity.** ``turn_ordinal`` locates a
turn-scoped hook against the turn it belongs to so that ``turn.complete`` can
be terminal for one turn while the Workflow Run stays open. It is never an
authorization input: nothing in this package or in
:mod:`omnivia_core.agent_host.mock` derives a capability, a purpose, a
workspace binding or a governance decision from it. The partition is exhaustive
-- :data:`TURN_SCOPED_HOOKS` requires it, :data:`RUN_LEVEL_HOOKS` forbids it --
and :class:`SpiRequest` enforces both directions at construction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields, replace
from enum import Enum
from types import MappingProxyType
from typing import Final

from omnivia_core.contracts.v1.compatibility import effective_capabilities
from omnivia_core.contracts.v1.generated import (
    COMPATIBILITY_STATUS_COMPATIBLE,
    COMPATIBILITY_STATUS_COMPATIBLE_WITH_DEPRECATIONS,
    COMPATIBILITY_STATUS_INCOMPATIBLE,
    COMPATIBILITY_STATUS_UPGRADE_REQUIRED,
    DEFAULT_RETRY_CLASSIFICATION,
    ERROR_CODE_AUTHENTICATION_REQUIRED,
    ERROR_CODE_AUTHORIZATION_DENIED,
    ERROR_CODE_CAPABILITY_NOT_GRANTED,
    ERROR_CODE_CONFLICT,
    ERROR_CODE_DEADLINE_EXCEEDED,
    ERROR_CODE_IDEMPOTENCY_CONFLICT,
    ERROR_CODE_INCOMPATIBLE_VERSION,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
    ERROR_CODE_INVALID_PURPOSE,
    ERROR_CODE_INVALID_REQUEST,
    ERROR_CODE_SIZE_LIMIT_EXCEEDED,
    ERROR_CODE_WORKSPACE_LEASE_UNAVAILABLE,
    ERROR_CODE_WORKSPACE_MIGRATION_REQUIRED,
    ERROR_CODE_WORKSPACE_NOT_GRANTED,
    RETRY_CLASS_NON_RETRYABLE,
    ApiError,
    CapabilityRef,
    CapabilityRequirement,
    CapabilitySet,
    ClientIdentity,
    CompatibilityMetadata,
    Deprecation,
    ErrorResponseEnvelope,
    GrantedAuthority,
    JobReference,
    PrincipalClaim,
    RequestEnvelope,
    RequestMetadata,
    ResponseEnvelope,
    ResponseMetadata,
    SuccessResponseEnvelope,
    UpgradeState,
    VersionCapabilityEnvelope,
    VersionWindow,
)

# --- versioned interface identity (SPI-R-001, SPI-R-002) ---------------------

#: The SPI semantic version this package implements. Distinct from the
#: application, server, workspace-format and client axes, and reported
#: alongside all four at negotiation.
SPI_VERSION: Final = "1.0.0"

#: The SPI version window a provider built from this module accepts. Fixed by
#: `SPI-V-026`, where an adapter declaring 1.2.0 negotiates down to 1.1.0.
SPI_VERSION_MINIMUM: Final = "1.0.0"
SPI_VERSION_MAXIMUM: Final = "1.1.0"

_SEMVER = re.compile(r"\A(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_CONTRACT_VERSION = re.compile(r"\A(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_CAPABILITY_TOKEN = re.compile(
    r"\A[a-z][a-z0-9]*(?:[_-][a-z0-9]+)*(?:\.[a-z][a-z0-9]*(?:[_-][a-z0-9]+)*)+"
    r"@(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z"
)

#: Bounds every scalar the wrapper carries. The corpus schema caps its own
#: strings at 200 characters and its integers at 4096 or 3600000; the wrapper
#: refuses anything past the same fences rather than truncating it.
MAX_LABEL_LENGTH: Final = 200
MAX_SEQUENCE: Final = 4096
MAX_TURN_ORDINAL: Final = 4096
MAX_DEADLINE_MS: Final = 3_600_000
MAX_CAPABILITIES: Final = 12


class SpiContractError(ValueError):
    """A wrapper, DTO or profile value outside the accepted domain.

    Raised at construction, never returned as a hook outcome: a malformed
    value is a programming defect in the adapter, whereas a well-formed value
    the provider refuses is a conformance outcome with a frozen error code.
    """


# --- hooks (section 3.1) -----------------------------------------------------


class Hook(Enum):
    """The ten hooks, closed within the major version (SPI-R-002)."""

    NEGOTIATE = "spi.negotiate"
    RECALL_BEFORE_TURN = "recall.before_turn"
    MEMORY_SEARCH = "memory.search"
    CAPTURE_AFTER_TURN = "capture.after_turn"
    TOOL_RESULT_PERSIST = "tool_result.persist"
    CONTEXT_COMPACT = "context.compact"
    APPROVAL_REQUEST = "approval.request"
    TURN_COMPLETE = "turn.complete"
    TURN_CANCEL = "turn.cancel"
    TURN_RETRY = "turn.retry"


#: The eight hooks whose effect and terminal state belong to one identified
#: turn. Each carries `turn_ordinal`.
TURN_SCOPED_HOOKS: Final[frozenset[Hook]] = frozenset(
    {
        Hook.RECALL_BEFORE_TURN,
        Hook.MEMORY_SEARCH,
        Hook.CAPTURE_AFTER_TURN,
        Hook.TOOL_RESULT_PERSIST,
        Hook.APPROVAL_REQUEST,
        Hook.TURN_COMPLETE,
        Hook.TURN_CANCEL,
        Hook.TURN_RETRY,
    }
)

#: The two hooks outside the turn state machine. Neither may carry
#: `turn_ordinal`: a turn coordinate on a run-level hook would imply a turn
#: position the hook does not have.
RUN_LEVEL_HOOKS: Final[frozenset[Hook]] = frozenset(
    {Hook.NEGOTIATE, Hook.CONTEXT_COMPACT}
)

#: The hooks that can produce a durable effect in Core, and therefore require
#: an idempotency key (SPI-R-023). `context.compact` is not one, and neither is
#: turn control: both compose no Core operation at all.
EFFECTING_HOOKS: Final[frozenset[Hook]] = frozenset(
    {Hook.CAPTURE_AFTER_TURN, Hook.TOOL_RESULT_PERSIST}
)

#: The hooks that read and change nothing. A stale delivery of one is dropped
#: rather than refused (SPI-R-027).
READ_ONLY_HOOKS: Final[frozenset[Hook]] = frozenset(
    {Hook.RECALL_BEFORE_TURN, Hook.MEMORY_SEARCH}
)

#: The three hooks that drive the turn state machine itself.
TURN_CONTROL_HOOKS: Final[frozenset[Hook]] = frozenset(
    {Hook.TURN_COMPLETE, Hook.TURN_CANCEL, Hook.TURN_RETRY}
)

#: Section 3.3, verbatim: which frozen catalogue operations each hook composes.
#: Six hooks compose none. `job.cancel` and `job.retry` appear nowhere, which is
#: what makes SPI-R-041 structural rather than a rule someone remembers.
HOOK_COMPOSITIONS: Final[MappingProxyType[Hook, tuple[str, ...]]] = MappingProxyType(
    {
        Hook.NEGOTIATE: ("workspace.inspect",),
        Hook.RECALL_BEFORE_TURN: (
            "memory.search",
            "knowledge.search",
            "evidence.search",
            "context_pack.build",
        ),
        Hook.MEMORY_SEARCH: ("memory.search", "knowledge.search"),
        Hook.CAPTURE_AFTER_TURN: ("memory.create",),
        Hook.TOOL_RESULT_PERSIST: ("memory.create",),
        Hook.CONTEXT_COMPACT: (),
        Hook.APPROVAL_REQUEST: (),
        Hook.TURN_COMPLETE: (),
        Hook.TURN_CANCEL: (),
        Hook.TURN_RETRY: (),
    }
)

#: The Core durable-job operations no hook may ever compose (SPI-R-041). Named
#: so the boundary layer can assert their absence rather than trust it.
CORE_JOB_CONTROL_OPERATIONS: Final[frozenset[str]] = frozenset(
    {"job.cancel", "job.retry"}
)

#: The frozen `RequestMetadata` field names, and the host identifiers that are
#: not among them. Both are read off the accepted contract rather than
#: restated, so the assertion tracks the contract instead of a copy of it.
HOST_IDENTITY_FIELD_NAMES: Final[frozenset[str]] = frozenset(
    {
        "agent",
        "agent_id",
        "session",
        "session_id",
        "run",
        "run_id",
        "turn",
        "turn_ordinal",
        "task_node_id",
    }
)


class ApprovalKind(Enum):
    """Which approval boundary an `approval.request` crosses (SPI-R-040).

    The two are never interchangeable. A `runtime_tool_approval` is run-local:
    it composes no Core operation, records no Core governance decision and
    grants no capability. A `core_governance_escalation` is a separate
    application-contract path taken by a validated principal holding
    `knowledge.govern`; the SPI reports that it is required and never takes it.
    """

    RUNTIME_TOOL_APPROVAL = "runtime_tool_approval"
    CORE_GOVERNANCE_ESCALATION = "core_governance_escalation"


class Disposition(Enum):
    """The six outcomes the accepted corpus admits."""

    REFUSED = "refused"
    IDEMPOTENT_REPLAY = "idempotent_replay"
    IGNORED = "ignored"
    NEGOTIATED = "negotiated"
    ESCALATION_REQUIRED = "escalation_required"
    ACCEPTED = "accepted"


class Reason(Enum):
    """Why the provider produced the disposition it did.

    The reason -- not the case identifier -- is what the audit policy and the
    conformance report are keyed on, so the provider stays a provider rather
    than a lookup table of expected answers.
    """

    PRE_NEGOTIATION = "pre_negotiation"
    HOST_SOURCE_PATCH = "host_source_patch"
    IDENTITY_INJECTION = "identity_injection"
    RUN_STATE_REQUESTED = "run_state_requested"
    JOB_CONTROL_COMPOSITION = "job_control_composition"
    DIRECT_STORAGE_ACCESS = "direct_storage_access"
    MISSING_DEADLINE = "missing_deadline"
    NESTED_DEADLINE = "nested_deadline"
    MISSING_IDEMPOTENCY_KEY = "missing_idempotency_key"
    AUTHENTICATION = "authentication"
    WORKSPACE_BINDING = "workspace_binding"
    PURPOSE = "purpose"
    CAPABILITY = "capability"
    LEASE_UNAVAILABLE = "lease_unavailable"
    TURN_NOT_OPEN = "turn_not_open"
    TURN_TERMINAL = "turn_terminal"
    STALE_SEQUENCE = "stale_sequence"
    SIZE_LIMIT = "size_limit"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    IDEMPOTENT_REPLAY = "idempotent_replay"
    STATE_REPLAY = "state_replay"
    GOVERNANCE_ESCALATION = "governance_escalation"
    NON_RETRYABLE_FAILURE = "non_retryable_failure"
    UNRECOGNISED_RETRY_CLASS = "unrecognised_retry_class"
    COMPACTION_NOTICE = "compaction_notice"
    NEGOTIATED = "negotiated"
    ACCEPTED = "accepted"


# --- validation helpers ------------------------------------------------------


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SpiContractError(message)


def _require_label(name: str, value: object) -> str:
    """A bounded non-empty printable label. Wrong types are refused, not coerced."""
    _require(type(value) is str, f"{name} must be a string")
    text = str(value)
    _require(
        0 < len(text) <= MAX_LABEL_LENGTH,
        f"{name} must be 1 to {MAX_LABEL_LENGTH} characters",
    )
    _require(text.isprintable(), f"{name} must not carry control characters")
    return text


def _require_bounded_int(name: str, value: object, *, maximum: int) -> int:
    """`bool` is an `int` in Python and is refused here on purpose."""
    _require(type(value) is int, f"{name} must be an integer")
    assert isinstance(value, int)
    _require(0 <= value <= maximum, f"{name} must be between 0 and {maximum}")
    return value


def _require_flag(name: str, value: object) -> bool:
    _require(type(value) is bool, f"{name} must be a boolean")
    return bool(value)


def _require_semantic_version(name: str, value: str) -> tuple[int, int, int]:
    match = _SEMVER.match(value) if type(value) is str else None
    _require(match is not None, f"{name} must be a semantic version")
    assert match is not None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def _require_contract_version(name: str, value: str) -> None:
    _require(
        type(value) is str and bool(_CONTRACT_VERSION.match(value)),
        f"{name} must be a major.minor contract version",
    )


def parse_capability_token(token: str) -> CapabilityRef:
    """Split the corpus's ``id@major.minor`` form into the frozen reference type.

    The corpus and the SPI speak one capability spelling; the frozen contract
    speaks another. Parsing rather than restating means a capability the frozen
    pattern would refuse cannot enter the SPI under an SPI-local spelling.
    """
    _require(
        type(token) is str and bool(_CAPABILITY_TOKEN.match(token)),
        f"capability {token!r} must be `id@major.minor`",
    )
    identifier, _, version = token.partition("@")
    return CapabilityRef(id=identifier, version=version)


def capability_token(ref: CapabilityRef) -> str:
    """The inverse of :func:`parse_capability_token`."""
    return f"{ref.id}@{ref.version}"


def _require_capability_tokens(name: str, values: tuple[str, ...]) -> tuple[str, ...]:
    _require(type(values) is tuple, f"{name} must be a tuple")
    _require(len(values) <= MAX_CAPABILITIES, f"{name} carries too many capabilities")
    _require(len(set(values)) == len(values), f"{name} must not repeat a capability")
    for token in values:
        parse_capability_token(token)
    return values


# --- the SPI request wrapper (SPI-R-007, SPI-R-039) --------------------------


@dataclass(frozen=True)
class SpiProvenance:
    """SPI-local provenance. Correlates a hook call; authorises nothing.

    None of these five values is ever written into ``RequestMetadata``, and
    none widens what a call may do. ``turn_ordinal`` is present on every
    turn-scoped hook and absent from every run-level one; :class:`SpiRequest`
    is what holds that partition, because provenance alone does not know which
    hook it belongs to.
    """

    agent: str
    session: str
    run: str
    sequence: int
    turn_ordinal: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent", _require_label("agent", self.agent))
        object.__setattr__(self, "session", _require_label("session", self.session))
        object.__setattr__(self, "run", _require_label("run", self.run))
        object.__setattr__(
            self,
            "sequence",
            _require_bounded_int("sequence", self.sequence, maximum=MAX_SEQUENCE),
        )
        if self.turn_ordinal is not None:
            object.__setattr__(
                self,
                "turn_ordinal",
                _require_bounded_int(
                    "turn_ordinal", self.turn_ordinal, maximum=MAX_TURN_ORDINAL
                ),
            )


@dataclass(frozen=True)
class HookIntent:
    """What the adapter is asking the hook to do beyond its plain form.

    Every field defaults to the harmless value, so a plain hook call declares
    nothing. The five booleans name the compositions the SPI may never make;
    the provider refuses each one rather than performing it, which is what
    lets an adapter that expects the composition fail against the mock instead
    of in production.
    """

    compose_core_job_control: bool = False
    request_core_run_state: bool = False
    direct_storage_access: bool = False
    requires_host_source_patch: bool = False
    inject_host_identity: bool = False
    promote_to_governed_knowledge: bool = False
    inline_payload_bytes: int = 0
    content_reference: str | None = None
    prior_failure_code: str | None = None
    unrecognised_retry_class: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "compose_core_job_control",
            "request_core_run_state",
            "direct_storage_access",
            "requires_host_source_patch",
            "inject_host_identity",
            "promote_to_governed_knowledge",
        ):
            object.__setattr__(self, name, _require_flag(name, getattr(self, name)))
        object.__setattr__(
            self,
            "inline_payload_bytes",
            _require_bounded_int(
                "inline_payload_bytes", self.inline_payload_bytes, maximum=2**31 - 1
            ),
        )
        for name in ("content_reference", "prior_failure_code", "unrecognised_retry_class"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _require_label(name, value))


@dataclass(frozen=True)
class SpiRequest:
    """One hook call: the SPI wrapper, never a widened application request.

    ``caller``, ``workspace`` and ``purpose`` are *claims* until the provider
    validates them (SPI-R-017). ``deadline_ms`` is ``None``-able at this layer
    on purpose: an adapter that omits it must reach the provider and be
    refused `invalid_request`, not fail to build a request object.
    """

    hook: Hook
    caller: str
    workspace: str
    purpose: str
    provenance: SpiProvenance
    granted_capabilities: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    deadline_ms: int | None = None
    elapsed_ms: int = 0
    idempotency_key: str | None = None
    payload_digest: str = "empty"
    approval_kind: ApprovalKind | None = None
    declared_spi_version: str | None = None
    parent_remaining_ms: int | None = None
    intent: HookIntent = field(default_factory=HookIntent)

    def __post_init__(self) -> None:
        _require(isinstance(self.hook, Hook), "hook must be a Hook member")
        object.__setattr__(self, "caller", _require_label("caller", self.caller))
        object.__setattr__(self, "workspace", _require_label("workspace", self.workspace))
        object.__setattr__(self, "purpose", _require_label("purpose", self.purpose))
        _require(
            isinstance(self.provenance, SpiProvenance),
            "provenance must be an SpiProvenance",
        )
        _require(isinstance(self.intent, HookIntent), "intent must be a HookIntent")
        object.__setattr__(
            self,
            "granted_capabilities",
            _require_capability_tokens("granted_capabilities", self.granted_capabilities),
        )
        object.__setattr__(
            self,
            "required_capabilities",
            _require_capability_tokens(
                "required_capabilities", self.required_capabilities
            ),
        )
        if self.deadline_ms is not None:
            object.__setattr__(
                self,
                "deadline_ms",
                _require_bounded_int("deadline_ms", self.deadline_ms, maximum=MAX_DEADLINE_MS),
            )
        object.__setattr__(
            self,
            "elapsed_ms",
            _require_bounded_int("elapsed_ms", self.elapsed_ms, maximum=MAX_DEADLINE_MS),
        )
        if self.parent_remaining_ms is not None:
            object.__setattr__(
                self,
                "parent_remaining_ms",
                _require_bounded_int(
                    "parent_remaining_ms", self.parent_remaining_ms, maximum=MAX_DEADLINE_MS
                ),
            )
        if self.idempotency_key is not None:
            object.__setattr__(
                self, "idempotency_key", _require_label("idempotency_key", self.idempotency_key)
            )
        object.__setattr__(
            self, "payload_digest", _require_label("payload_digest", self.payload_digest)
        )
        if self.approval_kind is not None:
            _require(
                isinstance(self.approval_kind, ApprovalKind),
                "approval_kind must be an ApprovalKind member",
            )
        _require(
            self.approval_kind is None or self.hook is Hook.APPROVAL_REQUEST,
            "approval_kind belongs to approval.request only",
        )
        if self.declared_spi_version is not None:
            _require_semantic_version("declared_spi_version", self.declared_spi_version)
        _require(
            self.declared_spi_version is None or self.hook is Hook.NEGOTIATE,
            "declared_spi_version belongs to spi.negotiate only",
        )
        # The partition, enforced in both directions.
        if self.hook in TURN_SCOPED_HOOKS:
            _require(
                self.provenance.turn_ordinal is not None,
                f"{self.hook.value} is turn-scoped and must carry turn_ordinal",
            )
        else:
            _require(
                self.provenance.turn_ordinal is None,
                f"{self.hook.value} is run-level and must not carry turn_ordinal",
            )

    @property
    def turn_ordinal(self) -> int | None:
        """Read-through for the coordinate. Never an authorization input."""
        return self.provenance.turn_ordinal

    def with_sequence(self, sequence: int) -> SpiRequest:
        """The same call at a different position in the run."""
        return replace(self, provenance=replace(self.provenance, sequence=sequence))


# --- the result and error algebra (SPI-R-008 to SPI-R-011) -------------------


@dataclass(frozen=True)
class VersionAxes:
    """The five axes negotiation reports (section 7).

    A separate SPI matrix would let an adapter be simultaneously supported and
    unsupported, so all five travel together in one value.
    """

    spi: str
    api: str
    server: str
    workspace_format: str
    client: str

    def __post_init__(self) -> None:
        _require_semantic_version("spi", self.spi)
        _require_semantic_version("server", self.server)
        _require_semantic_version("client", self.client)
        _require_contract_version("api", self.api)
        _require_contract_version("workspace_format", self.workspace_format)


@dataclass(frozen=True)
class HookOutcome:
    """What a hook produced, and everything the boundary layer needs to check it.

    ``response`` is the frozen envelope: exactly one of a success ``result`` or
    an ``error`` is present, because :class:`SuccessResponseEnvelope` and
    :class:`ErrorResponseEnvelope` are separate types with no field in common
    (SPI-R-009). ``composed_operations`` and ``nested_envelopes`` are the
    observable record of what the hook reached in Core, which is what turns the
    corpus's nine ``false`` constants from claims into derivable facts.
    """

    hook: Hook
    disposition: Disposition
    reason: Reason
    audit_record_required: bool
    response: ResponseEnvelope
    error_code: str | None = None
    retry_class: str | None = None
    compatibility_status: str | None = None
    version_axes: VersionAxes | None = None
    composed_operations: tuple[str, ...] = ()
    nested_envelopes: tuple[RequestEnvelope, ...] = ()
    job_handles: tuple[JobReference, ...] = ()
    granted_capabilities: tuple[str, ...] = ()
    effective_capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require(
            (self.error_code is None) == (self.retry_class is None),
            "an error code and its retry class travel together",
        )
        if self.error_code is not None:
            _require(
                self.retry_class == DEFAULT_RETRY_CLASSIFICATION.get(self.error_code),
                f"{self.error_code} must carry the frozen catalogue's retry class",
            )
        _require(
            (self.disposition is Disposition.REFUSED) == (self.error_code is not None),
            "only a refusal carries an error code",
        )
        _require(
            not (self.composed_operations and self.disposition is Disposition.REFUSED),
            "a refused hook composes nothing",
        )

    # The nine facts the corpus fixes, derived from what the hook actually did
    # rather than asserted. `host_source_patched`, `direct_storage_access` and
    # `canonical_mutation_applied` are constant because the provider owns no
    # host tree, no store and no canonical writer to reach in the first place;
    # the boundary layer proves that structurally instead of trusting these.

    @property
    def implies_core_job_cancel(self) -> bool:
        return "job.cancel" in self.composed_operations

    @property
    def implies_core_job_retry(self) -> bool:
        return "job.retry" in self.composed_operations

    @property
    def host_identity_in_request_metadata(self) -> bool:
        """True if any nested envelope leaked SPI-local provenance into metadata."""
        return any(
            envelope_carries_host_identity(envelope) for envelope in self.nested_envelopes
        )

    @property
    def capability_expanded(self) -> bool:
        return not set(self.effective_capabilities) <= set(self.granted_capabilities)


def envelope_carries_host_identity(envelope: RequestEnvelope) -> bool:
    """Whether a nested frozen envelope declares any host-identity metadata field."""
    return bool(
        {f.name for f in fields(envelope.metadata)} & HOST_IDENTITY_FIELD_NAMES
    )


def envelope_leaks_provenance(
    envelope: RequestEnvelope, provenance: SpiProvenance
) -> bool:
    """Whether any metadata *value* carries an agent, session, run or turn value.

    The field-name check alone would miss an adapter smuggling a run identifier
    through ``correlation_id`` or ``trace_id``, so the values are swept too.

    Absent values are dropped from both sides before the comparison. Rendering
    them made ``str(None)`` on a run-level hook's absent ``turn_ordinal`` match
    ``str(None)`` on any absent optional metadata field, so every clean envelope
    reported a leak and the check said nothing about any envelope.
    """
    leaked = {provenance.agent, provenance.session, provenance.run}
    if provenance.turn_ordinal is not None:
        leaked.add(str(provenance.turn_ordinal))
    values = {
        str(value)
        for f in fields(envelope.metadata)
        if (value := getattr(envelope.metadata, f.name)) is not None
    }
    return bool(values & leaked)


# --- envelope construction ---------------------------------------------------


def build_nested_envelope(
    operation: str,
    *,
    request_id: str,
    api_version: str,
    client: ClientIdentity,
    workspace_id: str,
    scopes: tuple[str, ...],
    purpose: str,
    required_capabilities: tuple[CapabilityRequirement, ...],
    principal_claim: PrincipalClaim,
    deadline_ms: int,
    idempotency_key: str | None = None,
) -> RequestEnvelope:
    """Build the frozen envelope a hook composes, unmodified and unwidened.

    The signature is the enforcement: there is no parameter through which an
    agent, session, run or turn value could reach ``RequestMetadata``, and
    ``request_id`` and ``correlation_id`` are derived from the operation rather
    than from host provenance. An adapter that wants host lineage on a Core
    record puts it in the payload, which is A8's carrier, not this one's.
    """
    _require(
        operation not in CORE_JOB_CONTROL_OPERATIONS,
        f"{operation} is Core job control and is never composed by a hook",
    )
    metadata = RequestMetadata(
        request_id=request_id,
        correlation_id=request_id,
        trace_id=request_id,
        api_version=api_version,
        client=client,
        scopes=scopes,
        purpose=purpose,
        required_capabilities=required_capabilities,
        workspace_id=workspace_id,
        deadline_ms=deadline_ms,
        idempotency_key=idempotency_key,
        principal_claim=principal_claim,
    )
    return RequestEnvelope(operation=operation, metadata=metadata, input={})


def build_version_envelope(
    axes: VersionAxes,
    *,
    status: str,
    upgrade_state: str,
    supported_api: VersionWindow,
    supported_workspace: VersionWindow,
    deprecations: tuple[Deprecation, ...],
    capabilities: CapabilitySet,
) -> VersionCapabilityEnvelope:
    """The frozen version and capability envelope every hook result carries.

    ``upgrade_state`` arrives as the frozen constant's bare string and is
    wrapped here: the contract's ``UpgradeState`` is a record whose ``value``
    is that constant, so the SPI keeps one spelling and the envelope keeps the
    contract's shape.
    """
    return VersionCapabilityEnvelope(
        api_version=axes.api,
        server_version=axes.server,
        workspace_format_version=axes.workspace_format,
        compatibility=CompatibilityMetadata(
            selected_api_version=axes.api,
            selected_workspace_version=axes.workspace_format,
            supported_api_versions=supported_api,
            supported_workspace_versions=supported_workspace,
            status=status,
            upgrade_state=UpgradeState(value=upgrade_state),
            deprecations=deprecations,
        ),
        capabilities=capabilities,
    )


def build_response(
    *,
    request_id: str,
    version: VersionCapabilityEnvelope,
    authority: GrantedAuthority,
    audit_reference: str | None,
    result: dict[str, object] | None = None,
    error: ApiError | None = None,
    job: JobReference | None = None,
) -> ResponseEnvelope:
    """Exactly one of ``result`` or ``error``; neither and both are violations."""
    _require(
        (result is None) != (error is None),
        "exactly one of result or error is present",
    )
    metadata = ResponseMetadata(
        request_id=request_id,
        correlation_id=request_id,
        version=version,
        authority=authority,
        job=job,
        audit_reference=audit_reference,
    )
    if error is not None:
        return ErrorResponseEnvelope(metadata=metadata, error=error)
    return SuccessResponseEnvelope(metadata=metadata, result=dict(result or {}))


def frozen_retry_class(error_code: str) -> str:
    """The catalogue's class for a code, failing safe as non-retryable.

    SPI-R-011: an unrecognised code is preserved and never inferred to be
    retryable, and never mapped onto a known one.
    """
    return DEFAULT_RETRY_CLASSIFICATION.get(error_code, RETRY_CLASS_NON_RETRYABLE)


def resolve_effective_capabilities(
    supported: tuple[str, ...], granted: tuple[str, ...]
) -> tuple[str, ...]:
    """The frozen intersection semantics, reused rather than restated (SPI-R-022)."""
    refs = effective_capabilities(
        tuple(parse_capability_token(token) for token in supported),
        tuple(parse_capability_token(token) for token in granted),
    )
    return tuple(capability_token(ref) for ref in refs)


#: Re-exported so a caller does not have to reach into `generated` for the four
#: codes and statuses the SPI's own rules name.
SPI_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        ERROR_CODE_AUTHENTICATION_REQUIRED,
        ERROR_CODE_AUTHORIZATION_DENIED,
        ERROR_CODE_CAPABILITY_NOT_GRANTED,
        ERROR_CODE_CONFLICT,
        ERROR_CODE_DEADLINE_EXCEEDED,
        ERROR_CODE_IDEMPOTENCY_CONFLICT,
        ERROR_CODE_INCOMPATIBLE_VERSION,
        ERROR_CODE_INTERNAL_NON_RECOVERABLE,
        ERROR_CODE_INVALID_PURPOSE,
        ERROR_CODE_INVALID_REQUEST,
        ERROR_CODE_SIZE_LIMIT_EXCEEDED,
        ERROR_CODE_WORKSPACE_LEASE_UNAVAILABLE,
        ERROR_CODE_WORKSPACE_MIGRATION_REQUIRED,
        ERROR_CODE_WORKSPACE_NOT_GRANTED,
    }
)

#: The four frozen compatibility statuses, named so the compatibility layer can
#: reproduce the section 7 matrix without reaching past this module.
SPI_COMPATIBILITY_STATUSES: Final[frozenset[str]] = frozenset(
    {
        COMPATIBILITY_STATUS_COMPATIBLE,
        COMPATIBILITY_STATUS_COMPATIBLE_WITH_DEPRECATIONS,
        COMPATIBILITY_STATUS_UPGRADE_REQUIRED,
        COMPATIBILITY_STATUS_INCOMPATIBLE,
    }
)

__all__ = [
    "CORE_JOB_CONTROL_OPERATIONS",
    "EFFECTING_HOOKS",
    "HOOK_COMPOSITIONS",
    "HOST_IDENTITY_FIELD_NAMES",
    "MAX_CAPABILITIES",
    "MAX_DEADLINE_MS",
    "MAX_LABEL_LENGTH",
    "MAX_SEQUENCE",
    "MAX_TURN_ORDINAL",
    "READ_ONLY_HOOKS",
    "RUN_LEVEL_HOOKS",
    "SPI_COMPATIBILITY_STATUSES",
    "SPI_ERROR_CODES",
    "SPI_VERSION",
    "SPI_VERSION_MAXIMUM",
    "SPI_VERSION_MINIMUM",
    "TURN_CONTROL_HOOKS",
    "TURN_SCOPED_HOOKS",
    "ApprovalKind",
    "Disposition",
    "Hook",
    "HookIntent",
    "HookOutcome",
    "Reason",
    "SpiContractError",
    "SpiProvenance",
    "SpiRequest",
    "VersionAxes",
    "build_nested_envelope",
    "build_response",
    "build_version_envelope",
    "capability_token",
    "envelope_carries_host_identity",
    "envelope_leaks_provenance",
    "frozen_retry_class",
    "parse_capability_token",
    "resolve_effective_capabilities",
]
