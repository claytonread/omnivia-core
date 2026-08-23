"""Runtime Execution Planes: the three plane behaviours, as deterministic locals.

One module per execution class would be three files describing one seam. These
three planes share a descriptor, a lineage, a refusal vocabulary and a single
notion of a fence, so they live together and the shared parts are written once.

None of them holds a database, scheduler, recovery, transport, filesystem or
Platform handle, and none writes canonical state. A "plane" here decides
*whether* something may run and records what it decided; the running itself is
a caller-supplied callable that this module never persists the result of.

- :class:`ComponentPlane` backs the ``DETERMINISTIC`` execution class. It
  separates four outcomes that are routinely conflated: a fresh completion, a
  *replay* of an earlier completion under the same idempotency key, a
  *cancellation* that discards whatever the handler produced, and an *invalid
  output* that the plane refuses rather than passes on. Capability proposals are
  the fifth separation and the load-bearing one: a deterministic component
  *proposes* capability use and this plane treats every proposal as data,
  applying none of them, ever.
- :class:`GovernedDispatchPlane` backs the ``EFFECT`` execution class and is a
  **provisional pre-FND-F3** admission gate. FND-F3 owns real governance; until
  it lands, this gate exists so nothing dispatches ungoverned in the meantime.
  It only ever refuses -- passing admission is not a grant, it is the absence of
  the six refusals below -- and every refusal happens strictly *before* the
  callback is invoked, which is the property that makes a provisional gate worth
  having at all.
- :class:`StatefulSessionPlane` backs the ``AGENT`` execution class with a
  deterministic session lease: exclusive while held, fenced across transfer of
  control, expiring against an injected clock, resumable from a snapshot, and
  leaving evidence for every acquire, snapshot, transfer, resume, expiry,
  release and reconcile. The clock is a parameter because a lease that expires
  against the wall clock cannot be tested deterministically. Snapshot, resume
  and transfer of control are gated on the provider build declaring it supports
  them, because a lifecycle operation the provider cannot perform is a refusal
  and not an attempt.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Final, TypeVar

from omnivia_core_runtime.execution.profile import (
    BUILD_TRUST_APPROVED,
    CONTRACT_VERSION,
    EXECUTION_CLASS_AGENT,
    EXECUTION_CLASS_DETERMINISTIC,
    EXECUTION_CLASS_EFFECT,
    PROFILE_TRUST_APPROVED,
    ExecutionContractError,
    ExecutionLineage,
    ExecutionRefused,
    RuntimeProfileDescriptor,
    SessionProviderDescriptor,
    canonical_hash,
    derive_id,
    require_collection,
    require_digest,
    require_identifier,
    require_version,
)

_T = TypeVar("_T")

#: The profile trust posture a governed dispatch plane admits at. Deliberately a
#: constant and not a constructor argument: a caller able to lower the bar for
#: its own plane is the failure this gate exists to prevent.
REQUIRED_DISPATCH_TRUST: Final = PROFILE_TRUST_APPROVED

STATUS_COMPLETED: Final = "COMPLETED"
STATUS_CANCELLED: Final = "CANCELLED"

LEASE_HELD: Final = "HELD"
LEASE_RELEASED: Final = "RELEASED"
LEASE_EXPIRED: Final = "EXPIRED"

LEASE_STATES: Final[frozenset[str]] = frozenset(
    {LEASE_HELD, LEASE_RELEASED, LEASE_EXPIRED}
)

#: The four stable answers :meth:`StatefulSessionPlane.reconcile` gives about an
#: external reference. ``ACTIVE`` is live and held, ``ORPHANED`` is live with no
#: holder, ``RELEASED`` is a reference this plane knows and is no longer live,
#: and ``UNKNOWN`` is a reference this plane has never seen and cannot see now.
SESSION_ACTIVE: Final = "ACTIVE"
SESSION_RELEASED: Final = "RELEASED"
SESSION_ORPHANED: Final = "ORPHANED"
SESSION_UNKNOWN: Final = "UNKNOWN"

SESSION_STATES: Final[frozenset[str]] = frozenset(
    {SESSION_ACTIVE, SESSION_RELEASED, SESSION_ORPHANED, SESSION_UNKNOWN}
)

_MAX_FIELDS: Final = 32
_MAX_PROPOSALS: Final = 16
_MAX_TTL_US: Final = 86_400_000_000


def _admitted_descriptor(
    descriptor: RuntimeProfileDescriptor, execution_class: str
) -> RuntimeProfileDescriptor:
    """Return ``descriptor`` if it may back a plane of ``execution_class``.

    Sealed and verified first: an unsealed or edited descriptor is refused
    before any of its own claims are read, because those claims are only worth
    reading once the hash says they are the ones that were sealed.
    """
    descriptor.verify_content_hash()
    if execution_class not in descriptor.execution_classes:
        raise ExecutionRefused(
            "unsupported_execution_class",
            f"descriptor does not declare the {execution_class} execution class",
        )
    return descriptor


def _validated_fields(label: str, pairs: tuple[tuple[str, str], ...]) -> None:
    """Bound-check one name/value collection, both halves as identifiers.

    Both halves, so the seam-wide "no free-text field, therefore no secret
    channel" property is structural here too rather than stopping at the keys.
    """
    if len(pairs) > _MAX_FIELDS:
        raise ExecutionContractError("collection_too_large", f"{label} exceeds its bound")
    names: list[str] = []
    for entry in pairs:
        if len(entry) != 2:
            raise ExecutionContractError(
                "invalid_field", f"{label} entry is not a name/value pair"
            )
        name, value = entry
        require_identifier(f"{label} name", name)
        require_identifier(f"{label} value", value)
        names.append(name)
    if len(set(names)) != len(names):
        raise ExecutionContractError(
            "duplicate_entry", f"{label} names the same field twice"
        )


# --------------------------------------------------------------------------
# Component plane (DETERMINISTIC execution class)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CapabilityProposal:
    """A *proposal* to use one capability against one target. Data, never an act.

    Deliberately inert. The component plane returns proposals to its caller and
    performs none of them, so a component that proposes a deletion has, from
    this seam's point of view, deleted nothing.
    """

    capability: str
    target_ref: str

    @property
    def key(self) -> str:
        return derive_id("capability_proposal", self.capability, self.target_ref)


@dataclass(frozen=True, slots=True)
class ComponentRequest:
    """One idempotent deterministic component invocation."""

    lineage: ExecutionLineage
    capability: str
    idempotency_key: str
    arguments: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        require_identifier("capability", self.capability)
        require_identifier("idempotency_key", self.idempotency_key)
        _validated_fields("arguments", self.arguments)

    @property
    def request_hash(self) -> str:
        """The content address of this request, used to detect a reused key."""
        return canonical_hash(
            {
                "lineage": self.lineage.key,
                "capability": self.capability,
                "idempotency_key": self.idempotency_key,
                "arguments": [[name, value] for name, value in self.arguments],
            }
        )


@dataclass(frozen=True, slots=True)
class ComponentResult:
    """What a component handler hands back, before the plane has validated it.

    An unvalidated carrier on purpose: validating in ``__post_init__`` would
    make an invalid output unconstructible, and the plane's refusal of one
    untestable. The plane is the single validation boundary.
    """

    output: tuple[tuple[str, str], ...] = ()
    proposals: tuple[CapabilityProposal, ...] = ()


@dataclass(frozen=True, slots=True)
class ComponentOutcome:
    """The result of one execution: completed or cancelled, fresh or replayed."""

    status: str
    request_hash: str
    output: tuple[tuple[str, str], ...] = ()
    proposals: tuple[CapabilityProposal, ...] = ()
    replayed: bool = False


#: A handler returns ``object`` rather than ``ComponentResult`` because the
#: plane validates what actually arrives. Annotating the return type as the
#: thing being checked for would make the check read as dead code.
ComponentHandler = Callable[[ComponentRequest], object]


class ComponentPlane:
    """A deterministic component execution fake with a replay ledger."""

    def __init__(
        self, descriptor: RuntimeProfileDescriptor, handler: ComponentHandler
    ) -> None:
        self.descriptor = _admitted_descriptor(
            descriptor, EXECUTION_CLASS_DETERMINISTIC
        )
        self._handler = handler
        self._ledger: dict[str, tuple[str, ComponentOutcome]] = {}

    def execute(
        self,
        request: ComponentRequest,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> ComponentOutcome:
        """Run ``request`` once, replaying an earlier completion under the same key."""
        if request.capability not in self.descriptor.capability_sets:
            raise ExecutionRefused(
                "unsupported_capability",
                f"profile does not declare capability {request.capability!r}",
            )
        recorded = self._ledger.get(request.idempotency_key)
        if recorded is not None:
            recorded_hash, outcome = recorded
            if recorded_hash != request.request_hash:
                raise ExecutionRefused(
                    "idempotency_conflict",
                    "idempotency key was already used by a different request",
                )
            return replace(outcome, replayed=True)
        if cancelled is not None and cancelled():
            return ComponentOutcome(STATUS_CANCELLED, request.request_hash)
        output, proposals = self._validated(self._handler(request))
        if cancelled is not None and cancelled():
            # Cancelled mid-flight. The proposals are dropped and nothing is
            # recorded, so a later attempt under the same key runs the handler
            # again rather than replaying a completion that never happened.
            return ComponentOutcome(STATUS_CANCELLED, request.request_hash)
        outcome = ComponentOutcome(
            STATUS_COMPLETED, request.request_hash, output, proposals
        )
        self._ledger[request.idempotency_key] = (request.request_hash, outcome)
        return outcome

    def _validated(
        self, result: object
    ) -> tuple[tuple[tuple[str, str], ...], tuple[CapabilityProposal, ...]]:
        if not isinstance(result, ComponentResult):
            raise ExecutionContractError(
                "invalid_component_output", "handler did not return a ComponentResult"
            )
        _validated_fields("output", result.output)
        if len(result.proposals) > _MAX_PROPOSALS:
            raise ExecutionContractError(
                "collection_too_large", "proposals exceeds its bound"
            )
        keys: list[str] = []
        for proposal in result.proposals:
            require_identifier("capability", proposal.capability)
            require_digest("target_ref", proposal.target_ref)
            keys.append(proposal.key)
        if len(set(keys)) != len(keys):
            raise ExecutionContractError(
                "duplicate_entry", "proposals names the same proposal twice"
            )
        return result.output, result.proposals


# --------------------------------------------------------------------------
# Governed dispatch plane (EFFECT execution class)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DispatchIntent:
    """The declared intent a governed dispatch is admitted against."""

    intent_id: str
    capability: str
    contract_version: str
    fence: int
    lineage: ExecutionLineage

    def __post_init__(self) -> None:
        require_identifier("intent_id", self.intent_id)
        require_identifier("capability", self.capability)
        require_version("contract_version", self.contract_version)
        if self.fence < 1:
            raise ExecutionContractError(
                "invalid_fence", "fence must be a positive integer"
            )


class GovernedDispatchPlane:
    """Provisional pre-FND-F3 admission in front of a governed dispatch callback.

    The accepted intent ids are fixed at construction rather than added later:
    an intent is admissible because governance minted it, and a plane that could
    grow its own allowlist mid-run would be admitting intents nobody accepted.
    """

    def __init__(
        self,
        descriptor: RuntimeProfileDescriptor,
        accepted_intent_ids: tuple[str, ...],
    ) -> None:
        self.descriptor = _admitted_descriptor(descriptor, EXECUTION_CLASS_EFFECT)
        self.accepted_intent_ids = require_collection(
            "accepted_intent_ids", accepted_intent_ids, required=True
        )
        self._fence = 1

    @property
    def fence(self) -> int:
        return self._fence

    def rotate_fence(self) -> int:
        """Advance the fence, invalidating every intent minted against the old one."""
        self._fence += 1
        return self._fence

    def admit(self, intent: DispatchIntent | None) -> DispatchIntent:
        """Refuse ``intent``, or return it unchanged. Returning it is not a grant.

        The order is deliberate and is the contract: an absent intent is refused
        before anything is read off it, the contract version before any claim in
        it is interpreted, the plane's own trust posture before the intent's id
        is matched, and the id before the capability is matched against a profile
        that may not be trusted to declare it.
        """
        if intent is None:
            raise ExecutionRefused(
                "missing_intent", "governed dispatch requires a declared intent"
            )
        if intent.contract_version != CONTRACT_VERSION:
            raise ExecutionRefused(
                "unsupported_contract_version",
                f"this seam speaks contract version {CONTRACT_VERSION} only",
            )
        if self.descriptor.trust_state != REQUIRED_DISPATCH_TRUST:
            raise ExecutionRefused(
                "unsupported_trust",
                f"governed dispatch requires an {REQUIRED_DISPATCH_TRUST} profile",
            )
        if intent.intent_id not in self.accepted_intent_ids:
            raise ExecutionRefused(
                "unaccepted_intent",
                f"intent {intent.intent_id!r} was not accepted by this plane",
            )
        if intent.capability not in self.descriptor.capability_sets:
            raise ExecutionRefused(
                "unsupported_capability",
                f"profile does not declare capability {intent.capability!r}",
            )
        if intent.fence < self._fence:
            raise ExecutionRefused(
                "stale_fence", "intent was minted against a superseded fence"
            )
        if intent.fence > self._fence:
            raise ExecutionRefused(
                "unknown_fence", "intent claims a fence this plane has not reached"
            )
        return intent

    def dispatch(
        self,
        intent: DispatchIntent | None,
        callback: Callable[[DispatchIntent], _T],
    ) -> _T:
        """Admit ``intent``, then invoke ``callback``. Every refusal precedes it."""
        return callback(self.admit(intent))


# --------------------------------------------------------------------------
# Stateful session plane (AGENT execution class)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionLease:
    """An exclusive, fenced, expiring claim on one session."""

    session_id: str
    holder: str
    fence: int
    expires_at_us: int
    lineage: ExecutionLineage
    external_reference: str
    state: str = LEASE_HELD

    def __post_init__(self) -> None:
        require_identifier("session_id", self.session_id)
        require_identifier("holder", self.holder)
        require_identifier("external_reference", self.external_reference)
        if self.fence < 1:
            raise ExecutionContractError(
                "invalid_fence", "fence must be a positive integer"
            )
        if self.expires_at_us < 0:
            raise ExecutionContractError(
                "invalid_deadline", "expires_at_us must not be negative"
            )
        if self.state not in LEASE_STATES:
            raise ExecutionContractError(
                "unknown_vocabulary_member", f"state {self.state!r} is not a lease state"
            )

    @property
    def key(self) -> str:
        return derive_id("lease", self.session_id, self.holder, str(self.fence))


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """A frozen, content-addressed view of one held session at one instant.

    Carries no session *contents* -- only the identity, the fence it was taken
    at and the external reference -- because contents are the provider's, and a
    seam that copied them would be holding state it promises not to hold.
    """

    session_id: str
    holder: str
    fence: int
    external_reference: str
    taken_at_us: int

    @property
    def key(self) -> str:
        return derive_id(
            "snapshot",
            self.session_id,
            self.holder,
            str(self.fence),
            self.external_reference,
            str(self.taken_at_us),
        )


@dataclass(frozen=True, slots=True)
class SessionEvent:
    """One piece of session evidence. Held in memory; never written anywhere."""

    kind: str
    session_id: str
    external_reference: str
    holder: str
    fence: int
    at_us: int
    #: The reconcile outcome for a ``RECONCILED`` event; empty for the rest.
    state: str = ""

    @property
    def key(self) -> str:
        return derive_id(
            "session_event",
            self.kind,
            self.session_id or "-",
            self.external_reference,
            self.holder or "-",
            str(self.fence),
            self.state or "-",
        )


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """What one reconcile decided, and the evidence it produced deciding it."""

    external_reference: str
    state: str
    session_id: str
    fence: int
    evidence: tuple[SessionEvent, ...]


class StatefulSessionPlane:
    """A deterministic session lease with transfer, snapshot, resume and reconcile."""

    def __init__(
        self,
        descriptor: RuntimeProfileDescriptor,
        clock: Callable[[], int],
        *,
        provider: SessionProviderDescriptor,
    ) -> None:
        self.descriptor = _admitted_descriptor(descriptor, EXECUTION_CLASS_AGENT)
        provider.verify_content_hash()
        if provider.provider_id not in self.descriptor.session_providers:
            raise ExecutionRefused(
                "unsupported_session_provider",
                f"profile does not declare session provider {provider.provider_id!r}",
            )
        if provider.trust_state != BUILD_TRUST_APPROVED:
            raise ExecutionRefused(
                "unsupported_trust",
                f"a session provider must be {BUILD_TRUST_APPROVED}",
            )
        if provider.required_isolation < self.descriptor.minimum_isolation:
            raise ExecutionRefused(
                "insufficient_isolation",
                "provider runs below the isolation the profile requires",
            )
        self.provider = provider
        self._clock = clock
        self._leases: dict[str, SessionLease] = {}
        self._events: list[SessionEvent] = []

    @property
    def evidence(self) -> tuple[SessionEvent, ...]:
        """Every session event so far, in the order it happened."""
        return tuple(self._events)

    def acquire(
        self,
        session_id: str,
        holder: str,
        lineage: ExecutionLineage,
        *,
        ttl_us: int,
        external_reference: str,
    ) -> SessionLease:
        """Take the session exclusively, refusing while another holder still has it."""
        require_identifier("session_id", session_id)
        require_identifier("holder", holder)
        require_identifier("external_reference", external_reference)
        if not 0 < ttl_us <= _MAX_TTL_US:
            raise ExecutionContractError("invalid_ttl", "ttl_us is outside its bound")
        now = self._now()
        current = self._leases.get(session_id)
        if current is not None and current.state == LEASE_HELD:
            if now < current.expires_at_us:
                raise ExecutionRefused(
                    "session_held", f"session {session_id!r} is held by another holder"
                )
            current = self._expire(current, now)
        fence = 1 if current is None else current.fence + 1
        lease = SessionLease(
            session_id, holder, fence, now + ttl_us, lineage, external_reference
        )
        self._leases[session_id] = lease
        self._record("ACQUIRED", lease, now)
        return lease

    def inspect(self, session_id: str) -> SessionLease | None:
        """Return the current lease for ``session_id``, expiring it if it is overdue.

        Expiry on inspection rather than only in :meth:`reconcile`, so a reader
        cannot see a timed-out lease as held by never reconciling.
        """
        require_identifier("session_id", session_id)
        lease = self._leases.get(session_id)
        if lease is None:
            return None
        now = self._now()
        if lease.state == LEASE_HELD and now >= lease.expires_at_us:
            return self._expire(lease, now)
        return lease

    def snapshot(self, lease: SessionLease) -> SessionSnapshot:
        """Take a fenced snapshot of a held session. Only the current holder may."""
        self._require_suspend_resume()
        now = self._now()
        current = self._active(lease, now)
        self._record("SNAPSHOTTED", current, now)
        return SessionSnapshot(
            current.session_id,
            current.holder,
            current.fence,
            current.external_reference,
            now,
        )

    def transfer_control(self, lease: SessionLease, new_holder: str) -> SessionLease:
        """Hand the session to ``new_holder``, advancing the fence past ``lease``."""
        if not self.provider.supports_control_transfer:
            raise ExecutionRefused(
                "unsupported_session_feature",
                "session provider does not support control transfer",
            )
        require_identifier("holder", new_holder)
        now = self._now()
        current = self._active(lease, now)
        transferred = replace(current, holder=new_holder, fence=current.fence + 1)
        self._leases[transferred.session_id] = transferred
        self._record("TRANSFERRED", transferred, now)
        return transferred

    def resume(
        self,
        snapshot: SessionSnapshot,
        holder: str,
        lineage: ExecutionLineage,
        *,
        ttl_us: int,
    ) -> SessionLease:
        """Re-take a session this plane knows, from a snapshot of its last fence.

        A snapshot older than the session's current fence is refused rather than
        resumed: the point of the fence is that whoever moved it last wins, and a
        resume from behind it would hand the session back to a superseded holder.
        """
        self._require_suspend_resume()
        require_identifier("holder", holder)
        if not 0 < ttl_us <= _MAX_TTL_US:
            raise ExecutionContractError("invalid_ttl", "ttl_us is outside its bound")
        now = self._now()
        current = self._leases.get(snapshot.session_id)
        if current is None:
            raise ExecutionRefused(
                "unknown_session", f"session {snapshot.session_id!r} is not known here"
            )
        if current.state == LEASE_HELD:
            if now < current.expires_at_us:
                raise ExecutionRefused(
                    "session_held", f"session {snapshot.session_id!r} is still held"
                )
            current = self._expire(current, now)
        if snapshot.fence != current.fence:
            raise ExecutionRefused(
                "stale_snapshot", "snapshot was taken against a superseded fence"
            )
        resumed = SessionLease(
            snapshot.session_id,
            holder,
            current.fence + 1,
            now + ttl_us,
            lineage,
            snapshot.external_reference,
        )
        self._leases[resumed.session_id] = resumed
        self._record("RESUMED", resumed, now)
        return resumed

    def release(self, lease: SessionLease) -> SessionLease:
        """Give the session up. Only the current fenced holder may."""
        now = self._now()
        released = replace(self._active(lease, now), state=LEASE_RELEASED)
        self._leases[released.session_id] = released
        self._record("RELEASED", released, now)
        return released

    def reconcile(
        self, external_reference: str, live_external_references: Iterable[str]
    ) -> ReconcileResult:
        """Say what ``external_reference`` is, against what is observably live.

        The four answers are a fixed function of two facts -- whether this plane
        holds an unexpired lease for the reference, and whether the outside world
        still lists it as live -- so the same pair always reconciles the same way:

        =========  ==========  ==========
        held       live        state
        =========  ==========  ==========
        yes        yes         ACTIVE
        no         yes         ORPHANED
        any        no          RELEASED (known) / UNKNOWN (never seen)
        =========  ==========  ==========
        """
        require_identifier("external_reference", external_reference)
        live = tuple(live_external_references)
        require_collection("live_external_references", live, required=False)
        now = self._now()
        before = len(self._events)
        # Expire first, so "held" means held *now* rather than held on paper.
        for session_id in sorted(self._leases):
            lease = self._leases[session_id]
            if lease.state == LEASE_HELD and now >= lease.expires_at_us:
                self._expire(lease, now)
        known = self._by_external_reference(external_reference)
        is_live = external_reference in live
        if known is None:
            state = SESSION_ORPHANED if is_live else SESSION_UNKNOWN
            self._events.append(
                SessionEvent("RECONCILED", "", external_reference, "", 0, now, state)
            )
        else:
            if not is_live:
                state = SESSION_RELEASED
            elif known.state == LEASE_HELD:
                state = SESSION_ACTIVE
            else:
                state = SESSION_ORPHANED
            self._record("RECONCILED", known, now, state=state)
        return ReconcileResult(
            external_reference,
            state,
            "" if known is None else known.session_id,
            0 if known is None else known.fence,
            tuple(self._events[before:]),
        )

    def _require_suspend_resume(self) -> None:
        if not self.provider.supports_suspend_resume:
            raise ExecutionRefused(
                "unsupported_session_feature",
                "session provider does not support suspend and resume",
            )

    def _by_external_reference(self, external_reference: str) -> SessionLease | None:
        """Return the lease carrying ``external_reference``, in deterministic order."""
        for session_id in sorted(self._leases):
            lease = self._leases[session_id]
            if lease.external_reference == external_reference:
                return lease
        return None

    def _active(self, lease: SessionLease, now: int) -> SessionLease:
        """Return the live lease ``lease`` is a current token for, or refuse."""
        current = self._leases.get(lease.session_id)
        if current is None or current.state != LEASE_HELD:
            raise ExecutionRefused(
                "no_active_lease", f"session {lease.session_id!r} is not held"
            )
        if lease.fence != current.fence or lease.holder != current.holder:
            raise ExecutionRefused(
                "stale_lease_fence", "lease token was superseded by a later holder"
            )
        if now >= current.expires_at_us:
            # Expire on contact rather than only in `reconcile`, so a caller
            # holding a timed-out token cannot act on it by never reconciling.
            self._expire(current, now)
            raise ExecutionRefused("lease_expired", "lease deadline has passed")
        return current

    def _expire(self, lease: SessionLease, now: int) -> SessionLease:
        expired = replace(lease, state=LEASE_EXPIRED)
        self._leases[expired.session_id] = expired
        self._record("EXPIRED", expired, now)
        return expired

    def _record(
        self, kind: str, lease: SessionLease, at_us: int, *, state: str = ""
    ) -> None:
        self._events.append(
            SessionEvent(
                kind,
                lease.session_id,
                lease.external_reference,
                lease.holder,
                lease.fence,
                at_us,
                state,
            )
        )

    def _now(self) -> int:
        now = self._clock()
        if now < 0:
            raise ExecutionContractError(
                "invalid_clock", "clock returned a negative timestamp"
            )
        return now


__all__ = [
    "LEASE_EXPIRED",
    "LEASE_HELD",
    "LEASE_RELEASED",
    "LEASE_STATES",
    "REQUIRED_DISPATCH_TRUST",
    "SESSION_ACTIVE",
    "SESSION_ORPHANED",
    "SESSION_RELEASED",
    "SESSION_STATES",
    "SESSION_UNKNOWN",
    "STATUS_CANCELLED",
    "STATUS_COMPLETED",
    "CapabilityProposal",
    "ComponentHandler",
    "ComponentOutcome",
    "ComponentPlane",
    "ComponentRequest",
    "ComponentResult",
    "DispatchIntent",
    "GovernedDispatchPlane",
    "ReconcileResult",
    "SessionEvent",
    "SessionLease",
    "SessionSnapshot",
    "StatefulSessionPlane",
]
