"""Synchronous, provider-neutral WorkerAdapter port (RT-108).

This is a private Core-internal port, not a public contract.  It negotiates a
bounded capability descriptor, then drives a deterministic scripted sequence
of worker events through vocabulary, bounds, secret and lineage validation,
normalizing each into an event carrying the exact host lineage it belongs to.

The adapter holds no database, dispatcher, tool, credential or policy handle.
It never persists or executes anything it normalizes; it only reports
open/start/resume/cancel/close results and hands validated events to a
caller-supplied sink.  RT-109 owns restart recovery for whatever the caller
does with those results — this port has no opinion on it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Final, Protocol

CONTRACT_VERSION: Final = 1

CAPABILITY_PLAN_PROPOSALS: Final = "plan_proposed"
CAPABILITY_ACTION_PROPOSALS: Final = "action_proposed"
CAPABILITY_CHILD_PROPOSALS: Final = "child_proposed"
CAPABILITY_ARTIFACT_PROPOSALS: Final = "artifact_proposed"

DISABLEABLE_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {
        CAPABILITY_PLAN_PROPOSALS,
        CAPABILITY_ACTION_PROPOSALS,
        CAPABILITY_CHILD_PROPOSALS,
        CAPABILITY_ARTIFACT_PROPOSALS,
    }
)

EVENT_KINDS: Final[frozenset[str]] = frozenset(
    {
        "message_delta",
        "message_completed",
        "plan_proposed",
        "action_proposed",
        "wait_requested",
        "child_proposed",
        "artifact_proposed",
        "usage_updated",
        "turn_completed",
        "turn_failed",
        "diagnostic",
    }
)

_MAX_SOURCE_EVENT_ID_LENGTH: Final = 128
_MAX_SAFE_SUMMARY_LENGTH: Final = 500
_MAX_EXTERNAL_ID_LENGTH: Final = 128
_MAX_PAYLOAD_REF_LENGTH: Final = 200

_PAYLOAD_REF_PATTERN: Final = re.compile(r"^payload:[A-Za-z0-9_.:-]{1,192}$")

_SECRET_PATTERN: Final = re.compile(
    r"(?i)(api[_-]?key|secret|password|passwd|access[_-]?token|"
    r"bearer\s+[a-z0-9._-]{8,}|authorization\s*:|private[_-]?key|-----BEGIN)"
)

_MAX_IDENTIFIER_LENGTH: Final = 128
_CONTROL_CHAR_PATTERN: Final = re.compile(r"[\x00-\x1f\x7f]")


def _validate_identifier(
    field_name: str, value: str, *, max_length: int = _MAX_IDENTIFIER_LENGTH
) -> None:
    """Bound-check one identifier/version/trust field and refuse it as a secret channel."""
    if not value or len(value) > max_length:
        raise WorkerContractError(f"{field_name} is empty or exceeds its bound")
    if _CONTROL_CHAR_PATTERN.search(value):
        raise WorkerContractError(f"{field_name} contains control characters")
    if _SECRET_PATTERN.search(value):
        raise WorkerContractError(f"{field_name} must not carry secret-bearing content")


_STATE_OPEN: Final = "open"
_STATE_RUNNING: Final = "running"
_STATE_WAITING: Final = "waiting"
_STATE_COMPLETED: Final = "completed"
_STATE_FAILED: Final = "failed"
_STATE_CANCELLED: Final = "cancelled"
_STATE_CLOSED: Final = "closed"
_STATE_QUARANTINED: Final = "quarantined"

_CANCELLABLE_STATES: Final[frozenset[str]] = frozenset(
    {_STATE_OPEN, _STATE_RUNNING, _STATE_WAITING}
)


class WorkerAdapterError(Exception):
    """Base error for the WorkerAdapter port."""


class WorkerContractError(WorkerAdapterError):
    """Descriptor negotiation refused an unsupported version or capability."""


class WorkerStateError(WorkerAdapterError):
    """A call violated the open/start/wait/resume/cancel/close/dispose machine."""


class WorkerEventRejected(WorkerAdapterError):
    """A scripted event failed vocabulary, bounds, secret or lineage validation."""


class WorkerEventConflict(WorkerAdapterError):
    """A duplicate source_event_id carried different content and quarantined."""


class WorkerScriptIncomplete(WorkerAdapterError):
    """A script ran out of events without reaching a terminal turn state."""


class WorkerSinkError(WorkerAdapterError):
    """The caller-supplied sink raised while accepting a normalized event."""


def _validate_event_identifier(field_name: str, value: str, *, max_length: int) -> None:
    """Reject an event identifier that could be an unbounded or secret channel."""
    if not value or len(value) > max_length:
        raise WorkerEventRejected(f"{field_name} is empty or exceeds its bound")
    if _CONTROL_CHAR_PATTERN.search(value):
        raise WorkerEventRejected(f"{field_name} contains control characters")
    if _SECRET_PATTERN.search(value):
        raise WorkerEventRejected(f"{field_name} must not carry secret-bearing content")


def _lineage_id(*parts: str) -> str:
    """Return a bounded deterministic identifier derived only from its parts."""
    digest = sha256("|".join(parts).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


@dataclass(frozen=True, slots=True)
class HostLineage:
    """The exact workspace/run/step/attempt lineage a worker session belongs to."""

    workspace_id: str
    run_id: str
    run_step_id: str
    attempt_id: str

    def __post_init__(self) -> None:
        for name in ("workspace_id", "run_id", "run_step_id", "attempt_id"):
            _validate_identifier(f"lineage field {name!r}", getattr(self, name))


@dataclass(frozen=True, slots=True)
class WorkerCapabilities:
    """Truthful, frozen booleans describing what a worker adapter actually supports."""

    streaming: bool
    resume_session: bool
    cancel_turn: bool
    plan_events: bool
    action_proposals: bool
    child_proposals: bool
    structured_output: bool
    usage_events: bool


ADAPTER_TYPE_DETERMINISTIC: Final = "deterministic_scripted"
_DETERMINISTIC_ADAPTER_VERSION: Final = "1.0.0"
_DETERMINISTIC_TRUST_PROFILE_ID: Final = "core.deterministic_scripted"

_DETERMINISTIC_CAPABILITIES: Final = WorkerCapabilities(
    streaming=True,
    resume_session=True,
    cancel_turn=True,
    plan_events=True,
    action_proposals=True,
    child_proposals=True,
    structured_output=False,
    usage_events=True,
)


@dataclass(frozen=True, slots=True)
class WorkerDescriptor:
    """The negotiated identity, contract version and capabilities of a session."""

    adapter_type: str
    adapter_version: str
    contract_version: int
    capabilities: WorkerCapabilities
    trust_profile_id: str
    protocol_name: str | None = None
    protocol_version: str | None = None
    required_host_capabilities: tuple[str, ...] = ()
    disabled_capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        _validate_identifier("adapter_type", self.adapter_type)
        _validate_identifier("adapter_version", self.adapter_version)
        if self.contract_version < 1:
            raise WorkerContractError("contract_version must be a positive integer")
        _validate_identifier("trust_profile_id", self.trust_profile_id)
        if self.protocol_name is not None:
            _validate_identifier("protocol_name", self.protocol_name)
        if self.protocol_version is not None:
            _validate_identifier("protocol_version", self.protocol_version)
        if (self.protocol_name is None) != (self.protocol_version is None):
            raise WorkerContractError(
                "protocol_name and protocol_version must be provided together"
            )
        for capability in self.required_host_capabilities:
            _validate_identifier("required_host_capabilities entry", capability)
        unknown_disabled = self.disabled_capabilities - DISABLEABLE_CAPABILITIES
        if unknown_disabled:
            raise WorkerContractError(
                f"cannot disable non-allowlisted capabilities {sorted(unknown_disabled)!r}"
            )


def negotiate_descriptor(
    *, requested_contract_version: int, disable: Iterable[str] = ()
) -> WorkerDescriptor:
    """Negotiate the deterministic adapter's descriptor for the fixed contract version."""
    if requested_contract_version != CONTRACT_VERSION:
        raise WorkerContractError(
            f"unsupported contract version {requested_contract_version!r}; "
            f"only {CONTRACT_VERSION!r} is negotiable"
        )
    return WorkerDescriptor(
        adapter_type=ADAPTER_TYPE_DETERMINISTIC,
        adapter_version=_DETERMINISTIC_ADAPTER_VERSION,
        contract_version=CONTRACT_VERSION,
        capabilities=_DETERMINISTIC_CAPABILITIES,
        trust_profile_id=_DETERMINISTIC_TRUST_PROFILE_ID,
        disabled_capabilities=frozenset(disable),
    )


@dataclass(frozen=True, slots=True)
class ScriptedWorkerEvent:
    """One raw event a scripted oracle plays back for a session's turn."""

    source_event_id: str
    kind: str
    occurred_at_us: int
    safe_summary: str
    external_session_id: str | None = None
    external_turn_id: str | None = None
    payload_ref: str | None = None
    claimed_lineage: HostLineage | None = None


@dataclass(frozen=True, slots=True)
class NormalizedWorkerEvent:
    """A scripted event after vocabulary, bounds and lineage validation."""

    source_event_id: str
    lineage: HostLineage
    session_id: str
    turn_id: str
    kind: str
    occurred_at_us: int
    safe_summary: str
    external_session_id: str | None
    external_turn_id: str | None
    payload_ref: str | None


class WorkerEventSink(Protocol):
    """Receives each normalized event as it is produced. Never stores state here."""

    def __call__(self, event: NormalizedWorkerEvent, /) -> None: ...


class WorkerAbortSignal(Protocol):
    """Cooperative abort check consulted between scripted events."""

    def is_set(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class WorkerOpenResult:
    session_id: str
    lineage: HostLineage
    descriptor: WorkerDescriptor


@dataclass(frozen=True, slots=True)
class WorkerStartResult:
    session_id: str
    turn_id: str
    lineage: HostLineage
    state: str


@dataclass(frozen=True, slots=True)
class WorkerResumeResult:
    session_id: str
    turn_id: str
    lineage: HostLineage
    state: str


@dataclass(frozen=True, slots=True)
class WorkerCancelResult:
    session_id: str
    turn_id: str | None
    lineage: HostLineage


@dataclass(frozen=True, slots=True)
class WorkerCloseResult:
    session_id: str
    lineage: HostLineage


@dataclass
class _Session:
    """Mutable per-session state private to one WorkerAdapter instance."""

    session_id: str
    lineage: HostLineage
    descriptor: WorkerDescriptor
    script: tuple[ScriptedWorkerEvent, ...]
    position: int = 0
    turn_id: str | None = None
    state: str = _STATE_OPEN
    seen: dict[str, NormalizedWorkerEvent] = field(default_factory=dict)


@dataclass
class WorkerAdapter:
    """One scripted, deterministic oracle driving worker sessions synchronously.

    A session is identified purely by its exact host lineage, so re-opening the
    same workspace/run/step/attempt tuple is refused rather than silently
    reused.  Every normalized event this adapter produces is only ever handed
    to the caller's sink; the adapter itself never writes, dispatches or
    executes anything.
    """

    _sessions: dict[str, _Session] = field(default_factory=dict, init=False, repr=False)
    _disposed: bool = field(default=False, init=False, repr=False)

    @property
    def descriptor(self) -> WorkerDescriptor:
        """Return this adapter type's immutable, provider-neutral descriptor."""
        return negotiate_descriptor(requested_contract_version=CONTRACT_VERSION)

    def open(
        self,
        *,
        lineage: HostLineage,
        script: Sequence[ScriptedWorkerEvent],
        requested_contract_version: int = CONTRACT_VERSION,
        disable: Iterable[str] = (),
    ) -> WorkerOpenResult:
        """Negotiate a descriptor and open one session for its exact lineage."""
        self._require_not_disposed()
        descriptor = negotiate_descriptor(
            requested_contract_version=requested_contract_version, disable=disable
        )
        session_id = _lineage_id(
            "worker_session",
            lineage.workspace_id,
            lineage.run_id,
            lineage.run_step_id,
            lineage.attempt_id,
        )
        if session_id in self._sessions:
            raise WorkerStateError(f"session {session_id!r} is already open")
        self._sessions[session_id] = _Session(
            session_id=session_id,
            lineage=lineage,
            descriptor=descriptor,
            script=tuple(script),
        )
        return WorkerOpenResult(
            session_id=session_id, lineage=lineage, descriptor=descriptor
        )

    def start(
        self,
        *,
        session_id: str,
        sink: WorkerEventSink,
        abort: WorkerAbortSignal | None = None,
    ) -> WorkerStartResult:
        """Start the one turn of an opened session, driving its script forward."""
        self._require_not_disposed()
        session = self._require_session(session_id)
        if session.state != _STATE_OPEN:
            raise WorkerStateError(
                f"session {session_id!r} must be 'open' to start, not {session.state!r}"
            )
        session.turn_id = _lineage_id("worker_turn", session_id, "1")
        session.state = _STATE_RUNNING
        self._drive(session, sink, abort)
        return WorkerStartResult(
            session_id=session_id,
            turn_id=session.turn_id,
            lineage=session.lineage,
            state=session.state,
        )

    def resume(
        self,
        *,
        session_id: str,
        lineage: HostLineage,
        sink: WorkerEventSink,
        abort: WorkerAbortSignal | None = None,
    ) -> WorkerResumeResult:
        """Resume a paused session, requiring its exact session id and lineage."""
        self._require_not_disposed()
        session = self._require_session(session_id)
        if session.state != _STATE_WAITING:
            raise WorkerStateError(
                f"session {session_id!r} must be 'waiting' to resume, not {session.state!r}"
            )
        if lineage != session.lineage:
            raise WorkerStateError(
                f"resume lineage does not match the exact lineage session "
                f"{session_id!r} was opened with"
            )
        assert session.turn_id is not None  # a waiting session always has a turn
        session.state = _STATE_RUNNING
        self._drive(session, sink, abort)
        return WorkerResumeResult(
            session_id=session_id,
            turn_id=session.turn_id,
            lineage=session.lineage,
            state=session.state,
        )

    def cancel(self, *, session_id: str) -> WorkerCancelResult:
        """Cancel a session before its turn starts or while it is paused/running."""
        self._require_not_disposed()
        session = self._require_session(session_id)
        if session.state not in _CANCELLABLE_STATES:
            raise WorkerStateError(
                f"session {session_id!r} cannot be cancelled from state {session.state!r}"
            )
        session.state = _STATE_CANCELLED
        return WorkerCancelResult(
            session_id=session_id, turn_id=session.turn_id, lineage=session.lineage
        )

    def close(self, *, session_id: str) -> WorkerCloseResult:
        """Close a session. Idempotent: closing an already-closed session is a no-op."""
        self._require_not_disposed()
        session = self._require_session(session_id)
        session.state = _STATE_CLOSED
        return WorkerCloseResult(session_id=session_id, lineage=session.lineage)

    @property
    def session_count(self) -> int:
        """Read-only count of sessions currently held open by this adapter."""
        return len(self._sessions)

    def dispose(self) -> None:
        """Release every owned session/script/seen state and refuse further calls."""
        if self._disposed:
            return
        self._sessions.clear()
        self._disposed = True

    def _require_not_disposed(self) -> None:
        if self._disposed:
            raise WorkerStateError(
                "worker adapter has been disposed and refuses further calls"
            )

    def _require_session(self, session_id: str) -> _Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise WorkerStateError(
                f"session {session_id!r} is not open on this adapter"
            )
        return session

    def _drive(
        self, session: _Session, sink: WorkerEventSink, abort: WorkerAbortSignal | None
    ) -> None:
        while session.position < len(session.script):
            if abort is not None and abort.is_set():
                session.state = _STATE_CANCELLED
                return
            raw = session.script[session.position]
            normalized = self._normalize(session, raw)
            session.position += 1
            if normalized is None:
                continue  # identical replay of an already-seen source event
            try:
                sink(normalized)
            except Exception as exc:
                session.state = _STATE_QUARANTINED
                raise WorkerSinkError(
                    f"sink rejected event {normalized.source_event_id!r}"
                ) from exc
            if normalized.kind == "wait_requested":
                session.state = _STATE_WAITING
                return
            if normalized.kind == "turn_completed":
                session.state = _STATE_COMPLETED
                return
            if normalized.kind == "turn_failed":
                session.state = _STATE_FAILED
                return
        if session.state == _STATE_RUNNING:
            session.state = _STATE_QUARANTINED
            raise WorkerScriptIncomplete(
                f"session {session.session_id!r} script ended without reaching "
                "turn_completed or turn_failed"
            )

    def _normalize(
        self, session: _Session, raw: ScriptedWorkerEvent
    ) -> NormalizedWorkerEvent | None:
        if raw.kind not in EVENT_KINDS:
            raise WorkerEventRejected(f"unknown event kind {raw.kind!r}")
        if raw.kind in session.descriptor.disabled_capabilities:
            raise WorkerEventRejected(
                f"capability {raw.kind!r} is disabled for this session"
            )
        _validate_event_identifier(
            "source_event_id",
            raw.source_event_id,
            max_length=_MAX_SOURCE_EVENT_ID_LENGTH,
        )
        if not raw.safe_summary or len(raw.safe_summary) > _MAX_SAFE_SUMMARY_LENGTH:
            raise WorkerEventRejected("safe_summary is empty or exceeds its bound")
        if raw.occurred_at_us <= 0:
            raise WorkerEventRejected(
                "occurred_at_us must be a positive microsecond timestamp"
            )
        for field_name, external in (
            ("external_session_id", raw.external_session_id),
            ("external_turn_id", raw.external_turn_id),
        ):
            if external is not None:
                _validate_event_identifier(
                    field_name, external, max_length=_MAX_EXTERNAL_ID_LENGTH
                )
        if raw.payload_ref is not None and (
            len(raw.payload_ref) > _MAX_PAYLOAD_REF_LENGTH
            or not _PAYLOAD_REF_PATTERN.match(raw.payload_ref)
        ):
            raise WorkerEventRejected(
                f"payload_ref {raw.payload_ref!r} is not a valid reference"
            )
        if _SECRET_PATTERN.search(raw.safe_summary) or (
            raw.payload_ref is not None and _SECRET_PATTERN.search(raw.payload_ref)
        ):
            raise WorkerEventRejected(
                "event content carries a secret-bearing key or value"
            )
        if raw.claimed_lineage is not None and raw.claimed_lineage != session.lineage:
            raise WorkerEventRejected(
                "event claims lineage that does not match the exact session lineage"
            )
        assert session.turn_id is not None  # driving only happens once a turn is open
        normalized = NormalizedWorkerEvent(
            source_event_id=raw.source_event_id,
            lineage=session.lineage,
            session_id=session.session_id,
            turn_id=session.turn_id,
            kind=raw.kind,
            occurred_at_us=raw.occurred_at_us,
            safe_summary=raw.safe_summary,
            external_session_id=raw.external_session_id,
            external_turn_id=raw.external_turn_id,
            payload_ref=raw.payload_ref,
        )
        previous = session.seen.get(raw.source_event_id)
        if previous is not None:
            if previous == normalized:
                return None
            session.state = _STATE_QUARANTINED
            raise WorkerEventConflict(
                f"source event {raw.source_event_id!r} was replayed with conflicting content"
            )
        session.seen[raw.source_event_id] = normalized
        return normalized


_ADAPTER_DESCRIPTORS: Final[dict[str, WorkerDescriptor]] = {
    ADAPTER_TYPE_DETERMINISTIC: negotiate_descriptor(
        requested_contract_version=CONTRACT_VERSION
    ),
}
_ADAPTER_FACTORIES: Final[dict[str, Callable[[], WorkerAdapter]]] = {
    ADAPTER_TYPE_DETERMINISTIC: WorkerAdapter,
}


@dataclass
class WorkerAdapterRegistry:
    """Allowlisted, disableable factory for WorkerAdapter instances.

    Only the fixed adapter types in ``_ADAPTER_FACTORIES`` can ever be
    created — there is no dynamic or plugin registration, and no arbitrary
    imports. The deterministic scripted adapter is the only default
    allowlisted factory.
    """

    _disabled: set[str] = field(default_factory=set, init=False, repr=False)

    def descriptor(self, adapter_type: str) -> WorkerDescriptor:
        """Return the type-level descriptor for an allowlisted adapter type."""
        self._require_known(adapter_type)
        return _ADAPTER_DESCRIPTORS[adapter_type]

    def disable(self, adapter_type: str) -> None:
        """Refuse further creation of an allowlisted adapter type."""
        self._require_known(adapter_type)
        self._disabled.add(adapter_type)

    def enable(self, adapter_type: str) -> None:
        """Re-allow creation of a previously disabled adapter type."""
        self._require_known(adapter_type)
        self._disabled.discard(adapter_type)

    def create(
        self, *, adapter_type: str, host_contract_version: int = CONTRACT_VERSION
    ) -> WorkerAdapter:
        """Create a new adapter instance, or refuse before any instance exists."""
        self._require_known(adapter_type)
        if adapter_type in self._disabled:
            raise WorkerContractError(f"adapter type {adapter_type!r} is disabled")
        descriptor = _ADAPTER_DESCRIPTORS[adapter_type]
        if host_contract_version != descriptor.contract_version:
            raise WorkerContractError(
                f"host contract version {host_contract_version!r} is incompatible "
                f"with adapter type {adapter_type!r} contract version "
                f"{descriptor.contract_version!r}"
            )
        return _ADAPTER_FACTORIES[adapter_type]()

    def _require_known(self, adapter_type: str) -> None:
        if adapter_type not in _ADAPTER_DESCRIPTORS:
            raise WorkerContractError(f"unknown adapter type {adapter_type!r}")


__all__ = [
    "ADAPTER_TYPE_DETERMINISTIC",
    "CAPABILITY_ACTION_PROPOSALS",
    "CAPABILITY_ARTIFACT_PROPOSALS",
    "CAPABILITY_CHILD_PROPOSALS",
    "CAPABILITY_PLAN_PROPOSALS",
    "CONTRACT_VERSION",
    "DISABLEABLE_CAPABILITIES",
    "EVENT_KINDS",
    "HostLineage",
    "NormalizedWorkerEvent",
    "ScriptedWorkerEvent",
    "WorkerAbortSignal",
    "WorkerAdapter",
    "WorkerAdapterError",
    "WorkerAdapterRegistry",
    "WorkerCancelResult",
    "WorkerCapabilities",
    "WorkerCloseResult",
    "WorkerContractError",
    "WorkerDescriptor",
    "WorkerEventConflict",
    "WorkerEventRejected",
    "WorkerEventSink",
    "WorkerOpenResult",
    "WorkerResumeResult",
    "WorkerScriptIncomplete",
    "WorkerSinkError",
    "WorkerStartResult",
    "WorkerStateError",
    "negotiate_descriptor",
]
