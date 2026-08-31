"""Runtime Execution Planes: the governed execution oracle.

A provisional in-memory conformance oracle for T-0688 `WEFT-BL-014/015/017`, sitting beside
:mod:`loop` and :mod:`workflow` under exactly the same standing constraints: no database, no
scheduler, no recovery, no transport, no Platform handle, no table, and no canonical state.
Every decision it makes is a value returned, a refusal raised, or an :class:`Evidence` value
appended -- and Evidence is by construction the thing that changed nothing.

Three questions, three oracles, one shared Evidence shape:

- :class:`HumanInteractionSettlement` settles one interaction request. Holding a submission
  grants nothing: current Permission and eligibility are rechecked on *every* call,
  including the replay of an already-accepted answer, because the answer being settled does
  not mean the answerer is still allowed to be answering. Exactly one accepted result
  exists, and exactly one Wait continuation is ever issued from it.
- :func:`resolve_autonomy` folds seven sources into one effective bound. It intersects the
  four scope dimensions, takes the minimum of each numeric bound, unions the approval
  requirements, and freezes the exact ordered snapshot provenance it used. It never grants
  Permission -- :attr:`EffectiveAutonomy.grants_permission` is structurally ``False`` -- and
  a Permission denial refuses regardless of how wide the resolved bound would have been.
- :class:`PlaneOwnership` decides who currently owns a plane. Fencing tokens are strictly
  increasing and never reused, a takeover leaves Evidence, a stale dispatch or write refuses,
  and a late completion leaves Evidence and is never applied.

**Deployment mode is an input, not a behaviour.** ``LOCAL`` and ``DISTRIBUTED`` are carried
so a caller can say which it is running, and the oracle answers identically for both. There
is deliberately no Kubernetes, process, pod, broker, queue or partition concept here: those
are how a deployment realises a plane, not what a plane *is*, and admitting one would make
this seam a second runtime.

Timing is a caller-supplied monotonic ordinal (``at_tick``), not a clock, exactly as
:mod:`loop` orders settlement by a sequence: a pure oracle has no clock, and a replay has to
reproduce the same answer. The wall-clock instants belong to the Core records in
:mod:`omnivia_core.contracts.v1.semantics_governed_execution`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from omnivia_core_runtime.execution.profile import (
    ExecutionContractError,
    ExecutionRefused,
    canonical_hash,
    derive_id,
    require_collection,
    require_digest,
    require_identifier,
    require_vocabulary,
)

#: The refusal and Evidence codes this seam names. They are spelled exactly as the Core
#: contract spells them, uppercase, so a refusal raised here and a diagnostic recorded there
#: are the same string rather than two conventions for one code.
HUMAN_INTERACTION_OUTCOME_CONFLICT: Final = "HUMAN_INTERACTION_OUTCOME_CONFLICT"
AUTONOMY_PROFILE_REFUSED: Final = "AUTONOMY_PROFILE_REFUSED"
EXECUTION_PLANE_STALE_AUTHORITY: Final = "EXECUTION_PLANE_STALE_AUTHORITY"
EXECUTION_PLANE_LATE_RESULT: Final = "EXECUTION_PLANE_LATE_RESULT"
EXECUTION_PLANE_OWNERSHIP_TAKEOVER: Final = "EXECUTION_PLANE_OWNERSHIP_TAKEOVER"
LISTENER_SATURATED: Final = "LISTENER_SATURATED"

DEPLOYMENT_LOCAL: Final = "LOCAL"
DEPLOYMENT_DISTRIBUTED: Final = "DISTRIBUTED"

#: Two spellings of *where*, and no third. Neither changes a single answer below; the mode
#: exists so a caller may state it and a test may prove the results are identical.
DEPLOYMENT_MODES: Final[frozenset[str]] = frozenset(
    {DEPLOYMENT_LOCAL, DEPLOYMENT_DISTRIBUTED}
)

INTERACTION_INPUT: Final = "INPUT"
INTERACTION_APPROVAL: Final = "APPROVAL"
INTERACTION_REVIEW: Final = "REVIEW"
INTERACTION_HANDOFF: Final = "HANDOFF"

INTERACTION_KINDS: Final[frozenset[str]] = frozenset(
    {INTERACTION_INPUT, INTERACTION_APPROVAL, INTERACTION_REVIEW, INTERACTION_HANDOFF}
)

SOURCE_COMPONENT_IMPLEMENTATION: Final = "COMPONENT_IMPLEMENTATION"
SOURCE_WORKFLOW_SETTINGS: Final = "WORKFLOW_SETTINGS"
SOURCE_POLICY: Final = "POLICY"
SOURCE_RESOURCE_SCOPE: Final = "RESOURCE_SCOPE"
SOURCE_MODEL_SCOPE: Final = "MODEL_SCOPE"
SOURCE_KNOWLEDGE_SCOPE: Final = "KNOWLEDGE_SCOPE"
SOURCE_APPROVAL_LIMITS: Final = "APPROVAL_LIMITS"

#: All seven, in the exact order a resolution records its provenance. A tuple rather than a
#: frozenset because the order is load-bearing: the resolution digest is taken over it.
AUTONOMY_SOURCE_ORDER: Final[tuple[str, ...]] = (
    SOURCE_COMPONENT_IMPLEMENTATION,
    SOURCE_WORKFLOW_SETTINGS,
    SOURCE_POLICY,
    SOURCE_RESOURCE_SCOPE,
    SOURCE_MODEL_SCOPE,
    SOURCE_KNOWLEDGE_SCOPE,
    SOURCE_APPROVAL_LIMITS,
)
AUTONOMY_SOURCE_KINDS: Final[frozenset[str]] = frozenset(AUTONOMY_SOURCE_ORDER)

#: The four scope dimensions and the four numeric bounds, named once each.
SCOPE_DIMENSIONS: Final[tuple[str, ...]] = (
    "action_scope",
    "resource_scope",
    "model_scope",
    "knowledge_scope",
)
BOUND_DIMENSIONS: Final[tuple[str, ...]] = (
    "maximum_wall_clock_ms",
    "maximum_tokens",
    "maximum_cost_units",
    "maximum_steps",
)

#: The explicit bounded default a source kind contributes when it is absent. Absent is not
#: "unbounded" and it is not "nothing": a default declares no *narrowing* of any scope
#: dimension -- narrowing to nothing would make every resolution empty -- and it declares a
#: finite bound on all four numeric dimensions, so an unconsulted source can only ever make
#: the resolution tighter, never wider.
DEFAULT_MAXIMUM_WALL_CLOCK_MS: Final = 900_000
DEFAULT_MAXIMUM_TOKENS: Final = 100_000
DEFAULT_MAXIMUM_COST_UNITS: Final = 1_000
DEFAULT_MAXIMUM_STEPS: Final = 100

PLANE_SCHEDULER: Final = "SCHEDULER"
PLANE_LISTENER: Final = "LISTENER"
PLANE_RESOURCE_SUPERVISOR: Final = "RESOURCE_SUPERVISOR"
PLANE_WORKER: Final = "WORKER"
PLANE_CAPABILITY_GATEWAY: Final = "CAPABILITY_GATEWAY"

PLANE_ROLES: Final[frozenset[str]] = frozenset(
    {
        PLANE_SCHEDULER,
        PLANE_LISTENER,
        PLANE_RESOURCE_SUPERVISOR,
        PLANE_WORKER,
        PLANE_CAPABILITY_GATEWAY,
    }
)

EFFECT_PURE: Final = "PURE"
EFFECT_RECOMPUTABLE: Final = "RECOMPUTABLE"
EFFECT_SNAPSHOT_BOUND_READ: Final = "SNAPSHOT_BOUND_READ"
EFFECT_INTERNAL_WRITE: Final = "INTERNAL_WRITE"
EFFECT_EXTERNAL_EFFECT: Final = "EXTERNAL_EFFECT"

#: The five effect classes an approval requirement may name. Same five the IP-10 Component
#: declaration names, spelled in this seam's uppercase convention rather than respelt as a
#: different set.
EFFECT_CLASSES: Final[frozenset[str]] = frozenset(
    {
        EFFECT_PURE,
        EFFECT_RECOMPUTABLE,
        EFFECT_SNAPSHOT_BOUND_READ,
        EFFECT_INTERNAL_WRITE,
        EFFECT_EXTERNAL_EFFECT,
    }
)

DELIVERY_DELIVERED: Final = "DELIVERED"
DELIVERY_DUPLICATE: Final = "DUPLICATE"

DELIVERY_DISPOSITIONS: Final[frozenset[str]] = frozenset(
    {DELIVERY_DELIVERED, DELIVERY_DUPLICATE}
)

OUTCOME_SUCCEEDED: Final = "SUCCEEDED"
OUTCOME_FAILED: Final = "FAILED"
OUTCOME_CANCELLED: Final = "CANCELLED"

COMPLETION_OUTCOMES: Final[frozenset[str]] = frozenset(
    {OUTCOME_SUCCEEDED, OUTCOME_FAILED, OUTCOME_CANCELLED}
)


@dataclass(frozen=True, slots=True)
class Evidence:
    """One recorded observation that changed nothing.

    Three oracles, one shape, because the property that matters is the same in all three:
    something was observed, it is attributable to a subject, and :attr:`applied` is ``False``.
    It is a field rather than an implied truth so a caller reading a sequence of Evidence
    never has to know which oracle produced which entry to know none of it was applied.
    """

    code: str
    subject: str
    detail_digest: str
    applied: bool = False


@dataclass(frozen=True, slots=True)
class InteractionRequest:
    """The frozen view of one `HumanInteractionRequest` this seam settles against."""

    request_id: str
    request_version: int
    idempotency_key: str
    interaction_kind: str
    owner: str
    wait_ref: str | None = None
    expires_at_tick: int | None = None

    def __post_init__(self) -> None:
        require_identifier("request_id", self.request_id)
        require_identifier("idempotency_key", self.idempotency_key)
        require_identifier("owner", self.owner)
        if self.wait_ref is not None:
            require_identifier("wait_ref", self.wait_ref)
        require_vocabulary("interaction_kind", self.interaction_kind, INTERACTION_KINDS)
        _require_ordinal("request_version", self.request_version)
        if self.expires_at_tick is not None:
            _require_ordinal("expires_at_tick", self.expires_at_tick)


@dataclass(frozen=True, slots=True)
class OutcomeSubmission:
    """One responder's answer, pinned to the exact request version it answers."""

    request_id: str
    request_version: int
    idempotency_key: str
    actor: str
    device_session: str
    response_digest: str
    at_tick: int

    def __post_init__(self) -> None:
        require_identifier("request_id", self.request_id)
        require_identifier("idempotency_key", self.idempotency_key)
        require_identifier("actor", self.actor)
        require_identifier("device_session", self.device_session)
        require_digest("response_digest", self.response_digest)
        _require_ordinal("request_version", self.request_version)
        _require_ordinal("at_tick", self.at_tick)


@dataclass(frozen=True, slots=True)
class SettlementResult:
    """The single accepted settlement, returned again verbatim on an identical replay.

    :attr:`continuation_issued` is the exactly-once marker: the first acceptance carries it
    ``True``, and a replay of that same acceptance carries it ``False``, so one accepted
    result can only ever yield one Wait continuation.
    """

    request_id: str
    request_version: int
    accepted_actor: str
    response_digest: str
    owner: str
    replayed: bool
    continuation_issued: bool
    continuation_token: str | None
    bundle_input_digest: str | None


def _require_ordinal(field_name: str, value: int) -> int:
    """Return ``value`` if it is a non-negative monotonic ordinal, else refuse it."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ExecutionContractError(
            "invalid_ordinal", f"{field_name} is not a non-negative integer ordinal"
        )
    return value


def _require_bound(field_name: str, value: int | None) -> int | None:
    if value is None:
        return None
    return _require_ordinal(field_name, value)


def _require_flag(field_name: str, value: bool) -> bool:
    if not isinstance(value, bool):
        raise ExecutionContractError("invalid_flag", f"{field_name} is not a boolean")
    return value


def require_deployment_mode(value: str) -> str:
    """Return ``value`` if it names a deployment mode; it never changes an answer."""
    return require_vocabulary("deployment_mode", value, DEPLOYMENT_MODES)


class HumanInteractionSettlement:
    """Settle one interaction request: at most one accepted answer, ever.

    The oracle holds one request and the at-most-one result settled against it. Every call
    to :meth:`submit` rechecks the caller's *current* Permission and eligibility, then the
    request version, then expiry, and only then looks at what is already settled -- so a
    responder who was eligible when they were asked and is not eligible now cannot settle,
    and cannot replay a settlement either. Possession of a submission grants nothing.

    A second, different answer is a race, not an error in the record: it refuses with
    ``HUMAN_INTERACTION_OUTCOME_CONFLICT`` and leaves Evidence that the conflicting answer
    was observed and not applied.
    """

    __slots__ = ("_evidence", "_request", "_result")

    def __init__(self, request: InteractionRequest) -> None:
        if not isinstance(request, InteractionRequest):
            raise ExecutionContractError(
                "invalid_request", "request is not an InteractionRequest"
            )
        self._request = request
        self._result: SettlementResult | None = None
        self._evidence: list[Evidence] = []

    @property
    def request(self) -> InteractionRequest:
        return self._request

    @property
    def settled(self) -> SettlementResult | None:
        """The accepted result, or ``None`` while nothing has been accepted."""
        return self._result

    @property
    def evidence(self) -> tuple[Evidence, ...]:
        return tuple(self._evidence)

    def submit(
        self,
        submission: OutcomeSubmission,
        *,
        permission_granted: bool,
        eligible: bool,
    ) -> SettlementResult:
        """Settle, replay, or refuse ``submission``; never write canonical state."""
        if not isinstance(submission, OutcomeSubmission):
            raise ExecutionContractError(
                "invalid_submission", "submission is not an OutcomeSubmission"
            )
        _require_flag("permission_granted", permission_granted)
        _require_flag("eligible", eligible)
        if submission.request_id != self._request.request_id:
            raise ExecutionContractError(
                "request_mismatch", "submission names a different request"
            )
        if submission.idempotency_key != self._request.idempotency_key:
            raise ExecutionRefused(
                HUMAN_INTERACTION_OUTCOME_CONFLICT,
                "submission carries a different idempotency key",
            )

        if submission.request_version != self._request.request_version:
            raise ExecutionRefused(
                HUMAN_INTERACTION_OUTCOME_CONFLICT,
                "submission answers a superseded request version",
            )

        # Rechecked every time, replay included: the settlement is not a credential, and
        # neither is having settled it once.
        if not permission_granted:
            raise ExecutionRefused(
                HUMAN_INTERACTION_OUTCOME_CONFLICT,
                "current Permission does not admit this responder",
            )
        if not eligible:
            raise ExecutionRefused(
                HUMAN_INTERACTION_OUTCOME_CONFLICT,
                "the responder is not currently eligible for this request",
            )
        expires_at = self._request.expires_at_tick
        if expires_at is not None and submission.at_tick >= expires_at:
            raise ExecutionRefused(
                HUMAN_INTERACTION_OUTCOME_CONFLICT,
                "the request had expired before this submission",
            )

        settled = self._result
        if settled is None:
            return self._accept(submission)
        if submission.response_digest == settled.response_digest:
            # An identical replay is a no-op that returns what was already accepted. It
            # issues no second continuation, which is the whole of "exactly one".
            return SettlementResult(
                request_id=settled.request_id,
                request_version=settled.request_version,
                accepted_actor=settled.accepted_actor,
                response_digest=settled.response_digest,
                owner=settled.owner,
                replayed=True,
                continuation_issued=False,
                continuation_token=None,
                bundle_input_digest=None,
            )

        self._evidence.append(
            Evidence(
                code=HUMAN_INTERACTION_OUTCOME_CONFLICT,
                subject=self._request.request_id,
                detail_digest=canonical_hash(
                    {
                        "acceptedActor": settled.accepted_actor,
                        "acceptedResponseDigest": settled.response_digest,
                        "conflictingActor": submission.actor,
                        "conflictingResponseDigest": submission.response_digest,
                    }
                ),
            )
        )
        raise ExecutionRefused(
            HUMAN_INTERACTION_OUTCOME_CONFLICT,
            "this request already carries a different accepted outcome",
        )

    def _accept(self, submission: OutcomeSubmission) -> SettlementResult:
        """Record the one accepted result and the one continuation it yields."""
        owner = self._request.owner
        token = None
        bundle_input_digest = None
        if self._request.wait_ref is not None:
            token = derive_id(
                self._request.request_id,
                str(self._request.request_version),
                self._request.wait_ref,
                submission.response_digest,
            )
            bundle_input_digest = canonical_hash(
                {
                    "continuationToken": token,
                    "owner": owner,
                    "requestId": self._request.request_id,
                    "responseDigest": submission.response_digest,
                    "waitRef": self._request.wait_ref,
                }
            )
        result = SettlementResult(
            request_id=self._request.request_id,
            request_version=self._request.request_version,
            accepted_actor=submission.actor,
            response_digest=submission.response_digest,
            owner=owner,
            replayed=False,
            continuation_issued=token is not None,
            continuation_token=token,
            bundle_input_digest=bundle_input_digest,
        )
        self._result = result
        return result


@dataclass(frozen=True, slots=True)
class AutonomySource:
    """One source's contribution to a Run's autonomy.

    A scope dimension of ``None`` means *this source declares no narrowing there*, which is
    what an explicit bounded default contributes and what an intersection must not treat as
    the empty set. An empty tuple is the opposite and is a real declaration: it narrows the
    dimension to nothing, and the resolution refuses.
    """

    source_kind: str
    source_ref: str
    snapshot_ref: str
    snapshot_digest: str
    action_scope: tuple[str, ...] | None = None
    resource_scope: tuple[str, ...] | None = None
    model_scope: tuple[str, ...] | None = None
    knowledge_scope: tuple[str, ...] | None = None
    approval_required: tuple[str, ...] = ()
    maximum_wall_clock_ms: int | None = None
    maximum_tokens: int | None = None
    maximum_cost_units: int | None = None
    maximum_steps: int | None = None

    def __post_init__(self) -> None:
        require_vocabulary("source_kind", self.source_kind, AUTONOMY_SOURCE_KINDS)
        require_identifier("source_ref", self.source_ref)
        require_identifier("snapshot_ref", self.snapshot_ref)
        require_digest("snapshot_digest", self.snapshot_digest)
        for name in SCOPE_DIMENSIONS:
            values: tuple[str, ...] | None = getattr(self, name)
            if values is not None:
                require_collection(name, values, required=False)
        require_collection(
            "approval_required",
            self.approval_required,
            required=False,
            allowed=EFFECT_CLASSES,
        )
        for name in BOUND_DIMENSIONS:
            _require_bound(name, getattr(self, name))

    def provenance(self) -> tuple[str, str, str]:
        """The three members a resolution freezes for this source, in contract order."""
        return (
            self.source_kind,
            self.snapshot_ref,
            self.snapshot_digest,
        )


@dataclass(frozen=True, slots=True)
class RequestedSpend:
    """What a Run is asking to spend, checked against the resolved minima."""

    wall_clock_ms: int | None = None
    tokens: int | None = None
    cost_units: int | None = None
    steps: int | None = None

    def __post_init__(self) -> None:
        for name in ("wall_clock_ms", "tokens", "cost_units", "steps"):
            _require_bound(name, getattr(self, name))


@dataclass(frozen=True, slots=True)
class EffectiveAutonomy:
    """The frozen resolution of all seven sources.

    :attr:`grants_permission` is structurally ``False``. It is carried rather than omitted
    because "this describes a bound and confers no authority" is the property most easily
    lost by a later reader, and a field that is always ``False`` is a claim a test can hold.
    """

    action_scope: tuple[str, ...]
    resource_scope: tuple[str, ...]
    model_scope: tuple[str, ...]
    knowledge_scope: tuple[str, ...]
    approval_required: tuple[str, ...]
    maximum_wall_clock_ms: int
    maximum_tokens: int
    maximum_cost_units: int
    maximum_steps: int
    source_contributions: tuple[tuple[str, str, str], ...]
    resolution_digest: str
    grants_permission: bool = False


def default_autonomy_source(source_kind: str) -> AutonomySource:
    """The explicit bounded default profile standing in for an absent source kind.

    Explicit, so a resolution always records seven contributions and a source that was never
    consulted is visible in the provenance rather than missing from it. Bounded, so an
    absent source can only tighten the numeric result. Its refs are derived from the kind,
    which keeps the default's provenance content-addressed like every other contribution.
    """
    require_vocabulary("source_kind", source_kind, AUTONOMY_SOURCE_KINDS)
    slug = source_kind.lower()
    return AutonomySource(
        source_kind=source_kind,
        source_ref=f"default.{slug}",
        snapshot_ref=f"default.{slug}.snapshot",
        snapshot_digest=derive_id("autonomy.default", source_kind),
        maximum_wall_clock_ms=DEFAULT_MAXIMUM_WALL_CLOCK_MS,
        maximum_tokens=DEFAULT_MAXIMUM_TOKENS,
        maximum_cost_units=DEFAULT_MAXIMUM_COST_UNITS,
        maximum_steps=DEFAULT_MAXIMUM_STEPS,
    )


def _intersect(
    sources: tuple[AutonomySource, ...], name: str
) -> tuple[str, ...] | None:
    """Fold one scope dimension across the sources that actually narrow it."""
    narrowed: frozenset[str] | None = None
    for source in sources:
        declared: tuple[str, ...] | None = getattr(source, name)
        if declared is None:
            continue
        narrowed = (
            frozenset(declared) if narrowed is None else narrowed & frozenset(declared)
        )
    return None if narrowed is None else tuple(sorted(narrowed))


def resolve_autonomy(
    sources: tuple[AutonomySource, ...],
    *,
    permission_granted: bool,
    requested: RequestedSpend | None = None,
    deployment_mode: str = DEPLOYMENT_LOCAL,
) -> EffectiveAutonomy:
    """Resolve all seven autonomy sources into one effective bound, or refuse.

    Deterministic in the sources and nothing else: the seven contributions are ordered by
    :data:`AUTONOMY_SOURCE_ORDER` rather than by the order they were supplied, so two
    callers holding the same sources resolve to the same digest. A source kind supplied
    twice is refused rather than merged -- picking a winner would be a policy this seam does
    not have.

    Refuses with ``AUTONOMY_PROFILE_REFUSED`` when Permission is denied, when any of the four
    intersections is empty (or was never narrowed by any source at all, which grants nothing
    rather than everything), or when a requested spend exceeds the resolved minimum.
    Permission denial is checked first and is never traded off against a wide bound.
    """
    require_deployment_mode(deployment_mode)
    _require_flag("permission_granted", permission_granted)
    if not isinstance(sources, tuple) or not all(
        isinstance(source, AutonomySource) for source in sources
    ):
        raise ExecutionContractError(
            "invalid_sources", "sources is not a tuple of AutonomySource"
        )
    supplied = {source.source_kind: source for source in sources}
    if len(supplied) != len(sources):
        raise ExecutionContractError(
            "duplicate_source_kind", "a source kind was supplied more than once"
        )

    if not permission_granted:
        raise ExecutionRefused(
            AUTONOMY_PROFILE_REFUSED, "Permission does not admit this resolution"
        )

    ordered = tuple(
        supplied.get(kind) or default_autonomy_source(kind)
        for kind in AUTONOMY_SOURCE_ORDER
    )

    resolved_scopes: list[tuple[str, ...]] = []
    for name in SCOPE_DIMENSIONS:
        intersection = _intersect(ordered, name)
        if not intersection:
            raise ExecutionRefused(
                AUTONOMY_PROFILE_REFUSED,
                f"{name} resolves to an empty intersection",
            )
        resolved_scopes.append(intersection)

    approval = tuple(
        sorted({entry for source in ordered for entry in source.approval_required})
    )
    bounds = tuple(
        min(
            value
            for value in (getattr(source, name) for source in ordered)
            if value is not None
        )
        for name in BOUND_DIMENSIONS
    )

    if requested is not None:
        if not isinstance(requested, RequestedSpend):
            raise ExecutionContractError(
                "invalid_requested_spend", "requested is not a RequestedSpend"
            )
        asked = (
            requested.wall_clock_ms,
            requested.tokens,
            requested.cost_units,
            requested.steps,
        )
        for name, want, allowed in zip(BOUND_DIMENSIONS, asked, bounds, strict=True):
            if want is not None and want > allowed:
                raise ExecutionRefused(
                    AUTONOMY_PROFILE_REFUSED,
                    f"requested {name} exceeds the resolved bound",
                )

    contributions = tuple(source.provenance() for source in ordered)
    return EffectiveAutonomy(
        action_scope=resolved_scopes[0],
        resource_scope=resolved_scopes[1],
        model_scope=resolved_scopes[2],
        knowledge_scope=resolved_scopes[3],
        approval_required=approval,
        maximum_wall_clock_ms=bounds[0],
        maximum_tokens=bounds[1],
        maximum_cost_units=bounds[2],
        maximum_steps=bounds[3],
        source_contributions=contributions,
        resolution_digest=canonical_hash(
            {
                "approvalRequired": list(approval),
                "bounds": list(bounds),
                "scopes": [list(scope) for scope in resolved_scopes],
                "sourceContributions": [list(entry) for entry in contributions],
            }
        ),
    )


@dataclass(frozen=True, slots=True)
class LeaseGrant:
    """One issued lease: who owns what, under which never-reused fencing token."""

    lease_id: str
    plane_role: str
    scope: str
    owner: str
    fencing_token: int
    expires_at_tick: int
    superseded_lease_id: str | None = None
    superseded_fencing_token: int | None = None


@dataclass(frozen=True, slots=True)
class DispatchRecord:
    """One dispatch admitted under a current lease."""

    dispatch_id: str
    plane_role: str
    lease_id: str
    fencing_token: int
    payload_digest: str


@dataclass(frozen=True, slots=True)
class CompletionOutcome:
    """The answer to one completion report: applied, or observed and discarded."""

    dispatch_id: str
    outcome: str
    applied: bool
    evidence: Evidence | None = None


@dataclass(frozen=True, slots=True)
class DeliveryDecision:
    """One listener delivery decision. Saturation is a refusal, not a disposition."""

    delivery_id: str
    dedupe_key: str
    disposition: str
    in_flight: int
    capacity: int


@dataclass(frozen=True, slots=True)
class ReconciliationDecision:
    """One resource reconciliation admitted under a current lease and fresh observation."""

    resource: str
    lease_id: str
    fencing_token: int
    observation_digest: str
    observation_age: int


@dataclass
class _LeaseState:
    lease_id: str
    plane_role: str
    scope: str
    owner: str
    fencing_token: int
    expires_at_tick: int
    revoked: bool = False


@dataclass
class _DispatchState:
    plane_role: str
    lease_id: str
    fencing_token: int
    completed: bool = False


class PlaneOwnership:
    """Decide who currently owns each execution plane scope, and refuse the stale.

    One monotonic counter issues every fencing token in the oracle, so tokens are strictly
    increasing and never reused across acquisition, renewal and takeover alike, and across
    scopes -- a token identifies a moment of authority, not a position in one scope's
    history. Ownership is keyed by ``(plane_role, scope)``, which is what makes the five
    plane roles genuinely separate: a worker lease over a scope is not a scheduler lease
    over the same scope.

    Every answer is identical for ``LOCAL`` and ``DISTRIBUTED``. The mode is recorded and
    never branched on.
    """

    __slots__ = (
        "_deliveries",
        "_deployment_mode",
        "_dispatches",
        "_evidence",
        "_in_flight",
        "_leases",
        "_listener_capacity",
        "_next_token",
        "_owned",
    )

    def __init__(
        self,
        *,
        deployment_mode: str = DEPLOYMENT_LOCAL,
        listener_capacity: int = 8,
    ) -> None:
        self._deployment_mode = require_deployment_mode(deployment_mode)
        if isinstance(listener_capacity, bool) or not isinstance(
            listener_capacity, int
        ):
            raise ExecutionContractError(
                "invalid_capacity", "listener_capacity is not an integer"
            )
        if listener_capacity < 1:
            raise ExecutionContractError(
                "invalid_capacity", "listener_capacity must be at least one"
            )
        self._listener_capacity = listener_capacity
        self._next_token = 1
        self._leases: dict[str, _LeaseState] = {}
        self._owned: dict[tuple[str, str], str] = {}
        self._dispatches: dict[str, _DispatchState] = {}
        self._deliveries: dict[str, str] = {}
        self._in_flight: set[str] = set()
        self._evidence: list[Evidence] = []

    @property
    def deployment_mode(self) -> str:
        return self._deployment_mode

    @property
    def evidence(self) -> tuple[Evidence, ...]:
        return tuple(self._evidence)

    @property
    def listener_capacity(self) -> int:
        return self._listener_capacity

    def _issue_token(self) -> int:
        token = self._next_token
        self._next_token += 1
        return token

    def _grant(
        self,
        *,
        lease_id: str,
        plane_role: str,
        scope: str,
        owner: str,
        expires_at_tick: int,
        superseded: _LeaseState | None,
    ) -> LeaseGrant:
        token = self._issue_token()
        self._leases[lease_id] = _LeaseState(
            lease_id=lease_id,
            plane_role=plane_role,
            scope=scope,
            owner=owner,
            fencing_token=token,
            expires_at_tick=expires_at_tick,
        )
        self._owned[(plane_role, scope)] = lease_id
        return LeaseGrant(
            lease_id=lease_id,
            plane_role=plane_role,
            scope=scope,
            owner=owner,
            fencing_token=token,
            expires_at_tick=expires_at_tick,
            superseded_lease_id=None if superseded is None else superseded.lease_id,
            superseded_fencing_token=(
                None if superseded is None else superseded.fencing_token
            ),
        )

    def acquire(
        self,
        *,
        plane_role: str,
        scope: str,
        owner: str,
        acquired_at_tick: int,
        expires_at_tick: int,
    ) -> LeaseGrant:
        """Take an unheld ``(plane_role, scope)``; refuse one already owned."""
        require_vocabulary("plane_role", plane_role, PLANE_ROLES)
        require_identifier("scope", scope)
        require_identifier("owner", owner)
        _require_ordinal("acquired_at_tick", acquired_at_tick)
        _require_ordinal("expires_at_tick", expires_at_tick)
        if expires_at_tick <= acquired_at_tick:
            raise ExecutionContractError(
                "invalid_lease_window", "lease expiry must be later"
            )
        if (plane_role, scope) in self._owned:
            raise ExecutionRefused(
                "EXECUTION_PLANE_ALREADY_OWNED",
                "the scope is already owned; renew or take over instead",
            )
        lease_id = derive_id("lease", plane_role, scope, owner, str(self._next_token))
        return self._grant(
            lease_id=lease_id,
            plane_role=plane_role,
            scope=scope,
            owner=owner,
            expires_at_tick=expires_at_tick,
            superseded=None,
        )

    def renew(
        self,
        *,
        lease_id: str,
        owner: str,
        renewed_at_tick: int,
        expires_at_tick: int,
    ) -> LeaseGrant:
        """Advance the fence for the current holder; refuse a superseded holder.

        A renewal issues a *new* token rather than extending the old one. Two writers
        holding two tokens is the situation fencing exists to resolve, and an unchanged
        token after a renewal would make a pre-renewal write indistinguishable from a
        post-renewal one.
        """
        require_digest("lease_id", lease_id)
        require_identifier("owner", owner)
        _require_ordinal("renewed_at_tick", renewed_at_tick)
        _require_ordinal("expires_at_tick", expires_at_tick)
        if expires_at_tick <= renewed_at_tick:
            raise ExecutionContractError(
                "invalid_lease_window", "lease expiry must be later"
            )
        held = self._leases.get(lease_id)
        if held is None or held.owner != owner:
            raise ExecutionRefused(
                EXECUTION_PLANE_STALE_AUTHORITY, "no such lease is held by this owner"
            )
        if self._owned.get((held.plane_role, held.scope)) != lease_id:
            raise ExecutionRefused(
                EXECUTION_PLANE_STALE_AUTHORITY, "the lease has already been superseded"
            )
        if held.revoked or renewed_at_tick >= held.expires_at_tick:
            raise ExecutionRefused(
                EXECUTION_PLANE_STALE_AUTHORITY, "the lease is revoked or expired"
            )
        token = self._issue_token()
        held.fencing_token = token
        held.expires_at_tick = expires_at_tick
        return LeaseGrant(
            lease_id=lease_id,
            plane_role=held.plane_role,
            scope=held.scope,
            owner=owner,
            fencing_token=token,
            expires_at_tick=expires_at_tick,
        )

    def take_over(
        self,
        *,
        plane_role: str,
        scope: str,
        owner: str,
        at_tick: int,
        expires_at_tick: int,
    ) -> LeaseGrant:
        """Supersede the current holder of a scope, leaving Evidence that it happened."""
        require_vocabulary("plane_role", plane_role, PLANE_ROLES)
        require_identifier("scope", scope)
        require_identifier("owner", owner)
        _require_ordinal("at_tick", at_tick)
        _require_ordinal("expires_at_tick", expires_at_tick)
        if expires_at_tick <= at_tick:
            raise ExecutionContractError(
                "invalid_lease_window", "lease expiry must be later"
            )
        current_id = self._owned.get((plane_role, scope))
        if current_id is None:
            raise ExecutionRefused(
                "EXECUTION_PLANE_NOT_OWNED", "there is no lease to take over"
            )
        superseded = self._leases[current_id]
        if not superseded.revoked and at_tick < superseded.expires_at_tick:
            raise ExecutionRefused(
                EXECUTION_PLANE_STALE_AUTHORITY,
                "the current lease has neither expired nor been revoked",
            )
        lease_id = derive_id("lease", plane_role, scope, owner, str(self._next_token))
        grant = self._grant(
            lease_id=lease_id,
            plane_role=plane_role,
            scope=scope,
            owner=owner,
            expires_at_tick=expires_at_tick,
            superseded=superseded,
        )
        self._evidence.append(
            Evidence(
                code=EXECUTION_PLANE_OWNERSHIP_TAKEOVER,
                subject=lease_id,
                detail_digest=canonical_hash(
                    {
                        "planeRole": plane_role,
                        "scope": scope,
                        "supersededFencingToken": superseded.fencing_token,
                        "supersededLeaseId": superseded.lease_id,
                        "supersededOwner": superseded.owner,
                        "takingOwner": owner,
                    }
                ),
            )
        )
        return grant

    def revoke(self, *, lease_id: str, fencing_token: int, at_tick: int) -> None:
        """Revoke the current lease so a governed takeover may proceed."""
        held = self._require_current(lease_id, fencing_token, at_tick=at_tick)
        held.revoked = True

    def _require_current(
        self, lease_id: str, fencing_token: int, *, at_tick: int
    ) -> _LeaseState:
        """The one staleness check every authority-bearing call routes through."""
        require_digest("lease_id", lease_id)
        _require_ordinal("fencing_token", fencing_token)
        _require_ordinal("at_tick", at_tick)
        held = self._leases.get(lease_id)
        if held is None:
            raise ExecutionRefused(EXECUTION_PLANE_STALE_AUTHORITY, "no such lease")
        if self._owned.get((held.plane_role, held.scope)) != lease_id:
            raise ExecutionRefused(
                EXECUTION_PLANE_STALE_AUTHORITY, "the lease has been superseded"
            )
        if held.fencing_token != fencing_token:
            raise ExecutionRefused(
                EXECUTION_PLANE_STALE_AUTHORITY,
                "the fencing token is not the lease's current token",
            )
        if held.revoked or at_tick >= held.expires_at_tick:
            raise ExecutionRefused(
                EXECUTION_PLANE_STALE_AUTHORITY, "the lease is revoked or expired"
            )
        return held

    def dispatch(
        self,
        *,
        lease_id: str,
        fencing_token: int,
        dispatch_id: str,
        payload_digest: str,
        at_tick: int,
    ) -> DispatchRecord:
        """Admit one dispatch under a current lease; refuse a stale one."""
        require_identifier("dispatch_id", dispatch_id)
        require_digest("payload_digest", payload_digest)
        held = self._require_current(lease_id, fencing_token, at_tick=at_tick)
        if dispatch_id in self._dispatches:
            raise ExecutionContractError(
                "duplicate_dispatch", "dispatch_id has already been dispatched"
            )
        self._dispatches[dispatch_id] = _DispatchState(
            plane_role=held.plane_role, lease_id=lease_id, fencing_token=fencing_token
        )
        return DispatchRecord(
            dispatch_id=dispatch_id,
            plane_role=held.plane_role,
            lease_id=lease_id,
            fencing_token=fencing_token,
            payload_digest=payload_digest,
        )

    def apply_write(
        self, *, lease_id: str, fencing_token: int, payload_digest: str, at_tick: int
    ) -> str:
        """Admit one write under a current lease; refuse a stale one.

        Shares :meth:`_require_current` with :meth:`dispatch` rather than restating the
        check, so "stale" means exactly one thing on both paths.
        """
        require_digest("payload_digest", payload_digest)
        held = self._require_current(lease_id, fencing_token, at_tick=at_tick)
        return derive_id("write", held.lease_id, str(fencing_token), payload_digest)

    def complete(
        self, *, dispatch_id: str, fencing_token: int, outcome: str, at_tick: int
    ) -> CompletionOutcome:
        """Apply a completion, or record a late one as Evidence and discard it.

        A late completion does not raise. It is a real observation of real work, and the
        honest answer is that it was seen and not applied -- so it returns
        ``applied=False`` carrying ``EXECUTION_PLANE_LATE_RESULT`` Evidence rather than
        vanishing into an exception the caller might swallow.
        """
        require_identifier("dispatch_id", dispatch_id)
        _require_ordinal("fencing_token", fencing_token)
        _require_ordinal("at_tick", at_tick)
        require_vocabulary("outcome", outcome, COMPLETION_OUTCOMES)
        state = self._dispatches.get(dispatch_id)
        if state is None:
            raise ExecutionContractError("unknown_dispatch", "no such dispatch")
        if state.completed:
            raise ExecutionContractError(
                "duplicate_completion", "the dispatch has already been completed"
            )

        held = self._leases.get(state.lease_id)
        current = (
            held is not None
            and self._owned.get((held.plane_role, held.scope)) == state.lease_id
            and held.fencing_token == fencing_token
            and state.fencing_token == fencing_token
            and not held.revoked
            and at_tick < held.expires_at_tick
        )
        if not current:
            evidence = Evidence(
                code=EXECUTION_PLANE_LATE_RESULT,
                subject=dispatch_id,
                detail_digest=canonical_hash(
                    {
                        "dispatchFencingToken": state.fencing_token,
                        "outcome": outcome,
                        "reportedFencingToken": fencing_token,
                    }
                ),
            )
            self._evidence.append(evidence)
            return CompletionOutcome(
                dispatch_id=dispatch_id,
                outcome=outcome,
                applied=False,
                evidence=evidence,
            )

        state.completed = True
        return CompletionOutcome(dispatch_id=dispatch_id, outcome=outcome, applied=True)

    def deliver(
        self,
        *,
        lease_id: str,
        fencing_token: int,
        delivery_id: str,
        dedupe_key: str,
        at_tick: int,
    ) -> DeliveryDecision:
        """Deduplicate one listener delivery, or shed it at bounded capacity.

        A duplicate is answered before capacity is consulted: rejecting a redelivery of work
        already in flight because the listener is full would turn a harmless retry into a
        loss. Only genuinely new work can saturate.
        """
        require_identifier("delivery_id", delivery_id)
        require_identifier("dedupe_key", dedupe_key)
        held = self._require_current(lease_id, fencing_token, at_tick=at_tick)
        if held.plane_role != PLANE_LISTENER:
            raise ExecutionContractError(
                "wrong_plane_role", "delivery requires a LISTENER lease"
            )
        if dedupe_key in self._deliveries:
            return DeliveryDecision(
                delivery_id=self._deliveries[dedupe_key],
                dedupe_key=dedupe_key,
                disposition=DELIVERY_DUPLICATE,
                in_flight=len(self._in_flight),
                capacity=self._listener_capacity,
            )
        if len(self._in_flight) >= self._listener_capacity:
            self._evidence.append(
                Evidence(
                    code=LISTENER_SATURATED,
                    subject=delivery_id,
                    detail_digest=canonical_hash(
                        {
                            "capacity": self._listener_capacity,
                            "dedupeKey": dedupe_key,
                            "inFlight": len(self._in_flight),
                        }
                    ),
                )
            )
            raise ExecutionRefused(
                LISTENER_SATURATED, "the listener is at its bounded delivery capacity"
            )
        self._deliveries[dedupe_key] = delivery_id
        self._in_flight.add(dedupe_key)
        return DeliveryDecision(
            delivery_id=delivery_id,
            dedupe_key=dedupe_key,
            disposition=DELIVERY_DELIVERED,
            in_flight=len(self._in_flight),
            capacity=self._listener_capacity,
        )

    def settle_delivery(self, *, dedupe_key: str) -> int:
        """Release one in-flight delivery and return the remaining in-flight count.

        The dedupe record itself is kept: a delivery that has been settled is still a
        delivery that has been seen, and forgetting it would let the same source event be
        delivered twice.
        """
        require_identifier("dedupe_key", dedupe_key)
        if dedupe_key not in self._deliveries:
            raise ExecutionContractError("unknown_delivery", "no such delivery")
        self._in_flight.discard(dedupe_key)
        return len(self._in_flight)

    def reconcile(
        self,
        *,
        lease_id: str,
        fencing_token: int,
        resource: str,
        observation_digest: str,
        observation_age: int,
        maximum_observation_age: int,
        at_tick: int,
    ) -> ReconciliationDecision:
        """Admit one reconciliation; refuse a stale lease or a stale observation."""
        require_identifier("resource", resource)
        require_digest("observation_digest", observation_digest)
        _require_ordinal("observation_age", observation_age)
        if maximum_observation_age < 1:
            raise ExecutionContractError(
                "invalid_freshness_bound",
                "maximum_observation_age must be at least one",
            )
        held = self._require_current(lease_id, fencing_token, at_tick=at_tick)
        if held.plane_role != PLANE_RESOURCE_SUPERVISOR:
            raise ExecutionContractError(
                "wrong_plane_role",
                "reconciliation requires a RESOURCE_SUPERVISOR lease",
            )
        if observation_age > maximum_observation_age:
            raise ExecutionRefused(
                "EXECUTION_PLANE_STALE_OBSERVATION",
                "the observation is older than the declared freshness bound",
            )
        return ReconciliationDecision(
            resource=resource,
            lease_id=lease_id,
            fencing_token=fencing_token,
            observation_digest=observation_digest,
            observation_age=observation_age,
        )


__all__ = [
    "AUTONOMY_PROFILE_REFUSED",
    "AUTONOMY_SOURCE_KINDS",
    "AUTONOMY_SOURCE_ORDER",
    "BOUND_DIMENSIONS",
    "COMPLETION_OUTCOMES",
    "DEFAULT_MAXIMUM_COST_UNITS",
    "DEFAULT_MAXIMUM_STEPS",
    "DEFAULT_MAXIMUM_TOKENS",
    "DEFAULT_MAXIMUM_WALL_CLOCK_MS",
    "DELIVERY_DELIVERED",
    "DELIVERY_DISPOSITIONS",
    "DELIVERY_DUPLICATE",
    "DEPLOYMENT_DISTRIBUTED",
    "DEPLOYMENT_LOCAL",
    "DEPLOYMENT_MODES",
    "EFFECT_CLASSES",
    "EFFECT_EXTERNAL_EFFECT",
    "EFFECT_INTERNAL_WRITE",
    "EFFECT_PURE",
    "EFFECT_RECOMPUTABLE",
    "EFFECT_SNAPSHOT_BOUND_READ",
    "EXECUTION_PLANE_LATE_RESULT",
    "EXECUTION_PLANE_OWNERSHIP_TAKEOVER",
    "EXECUTION_PLANE_STALE_AUTHORITY",
    "HUMAN_INTERACTION_OUTCOME_CONFLICT",
    "INTERACTION_APPROVAL",
    "INTERACTION_HANDOFF",
    "INTERACTION_INPUT",
    "INTERACTION_KINDS",
    "INTERACTION_REVIEW",
    "LISTENER_SATURATED",
    "OUTCOME_CANCELLED",
    "OUTCOME_FAILED",
    "OUTCOME_SUCCEEDED",
    "PLANE_CAPABILITY_GATEWAY",
    "PLANE_LISTENER",
    "PLANE_RESOURCE_SUPERVISOR",
    "PLANE_ROLES",
    "PLANE_SCHEDULER",
    "PLANE_WORKER",
    "SCOPE_DIMENSIONS",
    "SOURCE_APPROVAL_LIMITS",
    "SOURCE_COMPONENT_IMPLEMENTATION",
    "SOURCE_KNOWLEDGE_SCOPE",
    "SOURCE_MODEL_SCOPE",
    "SOURCE_POLICY",
    "SOURCE_RESOURCE_SCOPE",
    "SOURCE_WORKFLOW_SETTINGS",
    "AutonomySource",
    "CompletionOutcome",
    "DeliveryDecision",
    "DispatchRecord",
    "EffectiveAutonomy",
    "Evidence",
    "HumanInteractionSettlement",
    "InteractionRequest",
    "LeaseGrant",
    "OutcomeSubmission",
    "PlaneOwnership",
    "ReconciliationDecision",
    "RequestedSpend",
    "SettlementResult",
    "default_autonomy_source",
    "require_deployment_mode",
    "resolve_autonomy",
]
