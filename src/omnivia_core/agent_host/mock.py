"""The deterministic in-process mock provider for the agent-host SPI (A9-P1).

Standard library only. This exists so an adapter author can develop against
the SPI without a workspace, a lease or a Core Service, and so an adapter that
expects a composition the SPI forbids fails here rather than in production.

The candidate's section 5 fixes five properties, and each one is visible in the
code rather than asserted in prose:

1. **Deterministic.** No clock, no randomness, no iteration over an unordered
   set. Elapsed time is a request field, not a reading.
2. **It refuses.** :meth:`MockProvider.handle` is a fixed ladder of checks,
   every rung of which produces a frozen catalogue error code and the
   catalogue's retry class for it. The mock never mints a code.
3. **It holds no run state.** The ephemeral session state is a bounded turn
   frontier and an idempotency ledger, both dropped by :meth:`reset`. There is
   no persist, load, checkpoint or replay entry point, and nothing here
   survives the object.
4. **Turn and run are separate levels.** :class:`_RunState` tracks the run and
   its turns independently, so closing a turn demonstrably leaves the run open
   and a later turn opens normally.
5. **No Core job-control surface.** :data:`HOOK_COMPOSITIONS` names no job
   operation, :func:`build_nested_envelope` refuses one by name, and a request
   that asks a turn hook to compose one is refused `invalid_request` whether or
   not the caller holds `job.control`.

**Audit policy.** ``audit_record_required`` is decided by hook class and
refusal reason, never by case identifier. A run-level hook reaches no Core
operation and so is never audited; a run-local `runtime_tool_approval` is never
audited for the same reason; a turn-scoped outcome is audited unless the fault
was in the shape of the request rather than in its authority or its effect. The
corpus is what fixes which reasons fall on which side, and
:data:`UNAUDITED_REASONS` is that list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

from omnivia_core.agent_host.spi import (
    EFFECTING_HOOKS,
    READ_ONLY_HOOKS,
    RUN_LEVEL_HOOKS,
    TURN_CONTROL_HOOKS,
    ApprovalKind,
    Disposition,
    Hook,
    HookOutcome,
    Reason,
    SpiContractError,
    SpiRequest,
    VersionAxes,
    build_nested_envelope,
    build_response,
    build_version_envelope,
    frozen_retry_class,
    parse_capability_token,
    resolve_effective_capabilities,
)
from omnivia_core.agent_host.spi import (
    HOOK_COMPOSITIONS as _HOOK_COMPOSITIONS,
)
from omnivia_core.agent_host.spi import (
    SPI_VERSION_MAXIMUM as _SPI_MAX,
)
from omnivia_core.agent_host.spi import (
    SPI_VERSION_MINIMUM as _SPI_MIN,
)
from omnivia_core.contracts.v1.generated import (
    COMPATIBILITY_STATUS_COMPATIBLE,
    COMPATIBILITY_STATUS_COMPATIBLE_WITH_DEPRECATIONS,
    COMPATIBILITY_STATUS_INCOMPATIBLE,
    COMPATIBILITY_STATUS_UPGRADE_REQUIRED,
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
    RETRYABLE_RETRY_CLASSES,
    UPGRADE_STATE_NONE,
    UPGRADE_STATE_REQUIRED,
    ApiError,
    CapabilityRef,
    CapabilityRequirement,
    CapabilitySet,
    ClientIdentity,
    Deprecation,
    GrantedAuthority,
    JobReference,
    PrincipalClaim,
    RequestEnvelope,
    ResponseEnvelope,
    VersionWindow,
)

# --- audit policy ------------------------------------------------------------

#: Faults in the shape of a request rather than in its authority or its effect.
#: A hook refused for one of these reached no authorization decision and left
#: nothing behind, so there is nothing for Core to audit.
UNAUDITED_REASONS: Final[frozenset[Reason]] = frozenset(
    {
        Reason.PRE_NEGOTIATION,
        Reason.HOST_SOURCE_PATCH,
        Reason.IDENTITY_INJECTION,
        Reason.RUN_STATE_REQUESTED,
        Reason.MISSING_DEADLINE,
        Reason.NESTED_DEADLINE,
        Reason.SIZE_LIMIT,
        Reason.LEASE_UNAVAILABLE,
        Reason.COMPACTION_NOTICE,
        Reason.NEGOTIATED,
    }
)

#: A read changes nothing, so a read that simply succeeded or simply ran out of
#: time has no effect to account for. Every other read outcome -- a denial, an
#: ordering fault -- is an authority or lifecycle decision and is audited.
READ_ONLY_UNAUDITED_REASONS: Final[frozenset[Reason]] = frozenset(
    {Reason.ACCEPTED, Reason.DEADLINE_EXCEEDED}
)

#: Bounds on the ephemeral conformance state. Past either bound the mock
#: refuses to keep going rather than growing into a store.
MAX_TRACKED_RUNS: Final = 64
MAX_TRACKED_TURNS_PER_RUN: Final = 256
MAX_JOURNAL_ENTRIES: Final = 4096
MAX_IDEMPOTENCY_ENTRIES: Final = 1024


# --- server profile ----------------------------------------------------------


@dataclass(frozen=True)
class ProviderProfile:
    """The server-side conditions a hook call meets.

    Every restriction is an *exception set* so the default profile is a healthy
    server: a vector states the condition it needs by naming the caller,
    workspace or format that is not healthy, and states nothing otherwise.

    ``allowlisted_purposes`` is the one positive list, because purpose
    allowlisting is a positive check in the frozen contract: an unrecognised
    purpose is refused rather than admitted (SPI-R-021).
    """

    supported_capabilities: frozenset[str] = frozenset(
        {
            "memory.read@1.0",
            "memory.write@1.0",
            "knowledge.read@1.0",
            "knowledge.govern@1.0",
            "evidence.read@1.0",
            "context_pack.build@1.0",
            "workspace.read@1.0",
            "job.control@1.0",
        }
    )
    deprecated_capabilities: frozenset[str] = frozenset({"memory.read@1.0"})
    allowlisted_purposes: frozenset[str] = frozenset(
        {
            "assistant_turn_capture",
            "context_compaction",
            "context_recall",
            "governed_mutation",
            "runtime_tool_approval",
            "spi_negotiation",
            "tool_result_persistence",
            "turn_control",
            "turn_recovery",
        }
    )
    unauthenticated_callers: frozenset[str] = frozenset()
    ungranted_workspaces: frozenset[str] = frozenset()
    leased_workspaces: frozenset[str] = frozenset()
    workspaces_below_minimum_format: frozenset[str] = frozenset()
    max_inline_result_bytes: int = 65_536
    axes: VersionAxes = field(
        default_factory=lambda: VersionAxes(
            spi=_SPI_MAX,
            api="1.0",
            server="0.6.8",
            workspace_format="1.0",
            client="0.6.8",
        )
    )

    def __post_init__(self) -> None:
        for token in (*self.supported_capabilities, *self.deprecated_capabilities):
            parse_capability_token(token)
        if not isinstance(self.axes, VersionAxes):
            raise SpiContractError("axes must be a VersionAxes")
        if self.max_inline_result_bytes <= 0:
            raise SpiContractError("max_inline_result_bytes must be positive")


#: The negotiation profile used when a caller does not supply one.
DEFAULT_PROFILE: Final = ProviderProfile()


# --- ephemeral conformance state --------------------------------------------

_OPEN: Final = "open"
_COMPLETED: Final = "completed"
_CANCELLED: Final = "cancelled"


@dataclass
class _RunState:
    """The run level and the turn level, tracked separately on purpose.

    A single terminal state would let an adapter author ship the exact defect
    section 3.2 rule 4 forbids: a turn terminal that behaves as a run terminal.
    """

    workspace: str
    last_sequence: int = -1
    frontier: int = -1
    turns: dict[int, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _LedgerEntry:
    digest: str
    disposition: Disposition
    reason: Reason
    audit_record_required: bool


class MockProvider:
    """An in-process provider implementing all ten hooks.

    It has no store, no lease, no transport and no job-control surface. What it
    keeps between calls is named in :meth:`reset` and is dropped there in full.
    """

    def __init__(self, profile: ProviderProfile | None = None) -> None:
        self._profile = profile or DEFAULT_PROFILE
        self._negotiated = False
        self._selected_spi_version: str | None = None
        self._runs: dict[str, _RunState] = {}
        self._ledger: dict[str, _LedgerEntry] = {}
        self._journal: list[HookOutcome] = []

    # --- observation ---------------------------------------------------------

    @property
    def negotiated(self) -> bool:
        return self._negotiated

    @property
    def selected_spi_version(self) -> str | None:
        return self._selected_spi_version

    @property
    def journal(self) -> tuple[HookOutcome, ...]:
        """Every outcome this session produced, oldest first."""
        return tuple(self._journal)

    @property
    def composed_operations(self) -> tuple[str, ...]:
        """Every frozen catalogue operation any hook composed, in order."""
        return tuple(
            operation for outcome in self._journal for operation in outcome.composed_operations
        )

    @property
    def nested_envelopes(self) -> tuple[RequestEnvelope, ...]:
        return tuple(
            envelope for outcome in self._journal for envelope in outcome.nested_envelopes
        )

    @property
    def open_turns(self) -> MappingProxyType[str, tuple[int, ...]]:
        """The turns still open per run. Ephemeral; nothing here is persisted."""
        return MappingProxyType(
            {
                run: tuple(
                    sorted(turn for turn, state in item.turns.items() if state == _OPEN)
                )
                for run, item in self._runs.items()
            }
        )

    def reset(self) -> None:
        """Drop every scrap of session state. Nothing survives this call."""
        self._negotiated = False
        self._selected_spi_version = None
        self._runs.clear()
        self._ledger.clear()
        self._journal.clear()

    # --- the hook entry point ------------------------------------------------

    def handle(
        self, request: SpiRequest, *, profile: ProviderProfile | None = None
    ) -> HookOutcome:
        """Drive one hook and return its outcome.

        ``profile`` states the server conditions *for this call*. A lease is
        held at one moment and not another, and a purpose allowlist can be
        tightened between two calls in one session, so the condition belongs to
        the call rather than to the object.
        """
        outcome = self._dispatch(request, profile or self._profile)
        if len(self._journal) >= MAX_JOURNAL_ENTRIES:
            raise SpiContractError("conformance journal bound exceeded")
        self._journal.append(outcome)
        return outcome

    # --- dispatch ------------------------------------------------------------

    def _dispatch(self, request: SpiRequest, profile: ProviderProfile) -> HookOutcome:
        if request.hook is Hook.NEGOTIATE:
            return self._negotiate(request, profile)

        if not self._negotiated:
            return self._refuse(
                request, profile, Reason.PRE_NEGOTIATION, ERROR_CODE_INVALID_REQUEST
            )

        blocked = self._boundary_refusal(request, profile)
        if blocked is not None:
            return blocked

        shape = self._shape_refusal(request, profile)
        if shape is not None:
            return shape

        authority = self._authority_refusal(request, profile)
        if authority is not None:
            return authority

        replay = self._ledger_outcome(request, profile)
        if replay is not None:
            return replay

        if request.intent.inline_payload_bytes > profile.max_inline_result_bytes:
            return self._refuse(
                request, profile, Reason.SIZE_LIMIT, ERROR_CODE_SIZE_LIMIT_EXCEEDED
            )

        # `_shape_refusal` has already refused a missing deadline.
        deadline = request.deadline_ms if request.deadline_ms is not None else 0
        if request.elapsed_ms >= deadline:
            return self._record(
                request,
                self._refuse(
                    request, profile, Reason.DEADLINE_EXCEEDED, ERROR_CODE_DEADLINE_EXCEEDED
                ),
            )

        lifecycle = self._lifecycle_outcome(request, profile)
        if lifecycle is not None:
            return self._record(request, lifecycle)

        return self._record(request, self._effect_outcome(request, profile))

    # --- the check ladder ----------------------------------------------------

    def _boundary_refusal(
        self, request: SpiRequest, profile: ProviderProfile
    ) -> HookOutcome | None:
        """The compositions the SPI may never make, refused before anything else.

        `job.control` in the granted set does not make the composition legal:
        what is refused is the composition, not the capability.
        """
        intent = request.intent
        if intent.inject_host_identity:
            return self._refuse(
                request, profile, Reason.IDENTITY_INJECTION, ERROR_CODE_INVALID_REQUEST
            )
        if intent.direct_storage_access:
            return self._refuse(
                request, profile, Reason.DIRECT_STORAGE_ACCESS, ERROR_CODE_AUTHORIZATION_DENIED
            )
        if intent.request_core_run_state:
            return self._refuse(
                request, profile, Reason.RUN_STATE_REQUESTED, ERROR_CODE_INVALID_REQUEST
            )
        if intent.compose_core_job_control:
            return self._refuse(
                request, profile, Reason.JOB_CONTROL_COMPOSITION, ERROR_CODE_INVALID_REQUEST
            )
        if intent.requires_host_source_patch:
            return self._refuse(
                request, profile, Reason.HOST_SOURCE_PATCH, ERROR_CODE_INVALID_REQUEST
            )
        return None

    def _shape_refusal(
        self, request: SpiRequest, profile: ProviderProfile
    ) -> HookOutcome | None:
        if request.deadline_ms is None:
            return self._refuse(
                request, profile, Reason.MISSING_DEADLINE, ERROR_CODE_INVALID_REQUEST
            )
        if (
            request.parent_remaining_ms is not None
            and request.deadline_ms > request.parent_remaining_ms
        ):
            return self._refuse(
                request, profile, Reason.NESTED_DEADLINE, ERROR_CODE_INVALID_REQUEST
            )
        if request.hook in EFFECTING_HOOKS and request.idempotency_key is None:
            return self._refuse(
                request, profile, Reason.MISSING_IDEMPOTENCY_KEY, ERROR_CODE_INVALID_REQUEST
            )
        return None

    def _authority_refusal(
        self, request: SpiRequest, profile: ProviderProfile
    ) -> HookOutcome | None:
        """Host-supplied identity, workspace and purpose are claims until here."""
        if request.caller in profile.unauthenticated_callers:
            return self._refuse(
                request, profile, Reason.AUTHENTICATION, ERROR_CODE_AUTHENTICATION_REQUIRED
            )
        bound = self._runs.get(request.provenance.run)
        rebinding = bound is not None and bound.workspace != request.workspace
        if rebinding or request.workspace in profile.ungranted_workspaces:
            return self._refuse(
                request, profile, Reason.WORKSPACE_BINDING, ERROR_CODE_WORKSPACE_NOT_GRANTED
            )
        if request.purpose not in profile.allowlisted_purposes:
            return self._refuse(request, profile, Reason.PURPOSE, ERROR_CODE_INVALID_PURPOSE)
        effective = set(self._effective(request, profile))
        if not set(request.required_capabilities) <= effective:
            return self._refuse(
                request, profile, Reason.CAPABILITY, ERROR_CODE_CAPABILITY_NOT_GRANTED
            )
        if request.workspace in profile.leased_workspaces:
            return self._refuse(
                request,
                profile,
                Reason.LEASE_UNAVAILABLE,
                ERROR_CODE_WORKSPACE_LEASE_UNAVAILABLE,
            )
        return None

    def _ledger_outcome(
        self, request: SpiRequest, profile: ProviderProfile
    ) -> HookOutcome | None:
        key = request.idempotency_key
        if key is None:
            return None
        entry = self._ledger.get(key)
        if entry is None:
            return None
        if entry.digest != request.payload_digest:
            return self._refuse(
                request, profile, Reason.IDEMPOTENCY_CONFLICT, ERROR_CODE_IDEMPOTENCY_CONFLICT
            )
        # The recorded result, and no second effect: a replay composes nothing.
        return self._succeed(
            request,
            profile,
            Disposition.IDEMPOTENT_REPLAY,
            Reason.IDEMPOTENT_REPLAY,
            result={"replayed": True, "of": entry.reason.value},
            compose=False,
        )

    def _lifecycle_outcome(
        self, request: SpiRequest, profile: ProviderProfile
    ) -> HookOutcome | None:
        """Sequence monotonicity and the turn state machine (section 3.2)."""
        run = self._run_state(request)
        if request.provenance.sequence <= run.last_sequence:
            if request.hook in READ_ONLY_HOOKS:
                return self._succeed(
                    request,
                    profile,
                    Disposition.IGNORED,
                    Reason.STALE_SEQUENCE,
                    result={"dropped": True},
                    compose=False,
                )
            return self._refuse(
                request, profile, Reason.STALE_SEQUENCE, ERROR_CODE_INVALID_REQUEST
            )
        run.last_sequence = request.provenance.sequence

        if request.hook is Hook.CONTEXT_COMPACT:
            # Host-local notice. No Core operation, no Core record, no audit.
            return self._succeed(
                request,
                profile,
                Disposition.IGNORED,
                Reason.COMPACTION_NOTICE,
                result={"acknowledged": True},
                compose=False,
            )

        # Every hook still here is turn-scoped, so the coordinate is present:
        # `SpiRequest` refuses a turn-scoped hook without one at construction.
        turn = request.provenance.turn_ordinal
        if turn is None:
            raise SpiContractError(f"{request.hook.value} reached the turn machine untyped")
        state = run.turns.get(turn)

        if request.hook is Hook.RECALL_BEFORE_TURN:
            if state is None:
                if turn <= run.frontier:
                    return self._refuse(
                        request, profile, Reason.TURN_NOT_OPEN, ERROR_CODE_INVALID_REQUEST
                    )
                self._open_turn(run, turn)
                return None
            if state != _OPEN:
                return self._refuse(
                    request, profile, Reason.TURN_TERMINAL, ERROR_CODE_CONFLICT
                )
            return None

        if state is None:
            return self._refuse(
                request, profile, Reason.TURN_NOT_OPEN, ERROR_CODE_INVALID_REQUEST
            )

        if request.hook in TURN_CONTROL_HOOKS:
            return self._turn_control(request, profile, run, turn, state)

        if state != _OPEN:
            # Turn completion is terminal for the identified turn. The run
            # stays open, which is what lets a later turn still be recalled.
            return self._refuse(request, profile, Reason.TURN_TERMINAL, ERROR_CODE_CONFLICT)
        return None

    def _turn_control(
        self,
        request: SpiRequest,
        profile: ProviderProfile,
        run: _RunState,
        turn: int,
        state: str,
    ) -> HookOutcome | None:
        if request.hook is Hook.TURN_RETRY:
            # The single recovery entry point: it re-enters the identified
            # turn's open state and never closes or reopens the run.
            run.turns[turn] = _OPEN
            return None
        target = _COMPLETED if request.hook is Hook.TURN_COMPLETE else _CANCELLED
        if state == target:
            # A state-based refusal of a control call is a successful
            # idempotent result carrying the unchanged handle, not a conflict.
            return self._succeed(
                request,
                profile,
                Disposition.IDEMPOTENT_REPLAY,
                Reason.STATE_REPLAY,
                result={"turn_state": state},
                compose=False,
                job_handles=self._outstanding_jobs(request),
            )
        if state != _OPEN:
            return self._refuse(request, profile, Reason.TURN_TERMINAL, ERROR_CODE_CONFLICT)
        run.turns[turn] = target
        return self._succeed(
            request,
            profile,
            Disposition.ACCEPTED,
            Reason.ACCEPTED,
            result={"turn_state": target},
            compose=False,
            job_handles=self._outstanding_jobs(request),
        )

    def _effect_outcome(self, request: SpiRequest, profile: ProviderProfile) -> HookOutcome:
        intent = request.intent
        if intent.unrecognised_retry_class is not None:
            # Preserved, never mapped onto a known class and never inferred to
            # be retryable.
            return self._refuse(
                request,
                profile,
                Reason.UNRECOGNISED_RETRY_CLASS,
                ERROR_CODE_INTERNAL_NON_RECOVERABLE,
            )
        if intent.prior_failure_code is not None:
            code = intent.prior_failure_code
            if frozen_retry_class(code) not in RETRYABLE_RETRY_CLASSES:
                return self._refuse(request, profile, Reason.NON_RETRYABLE_FAILURE, code)
        if intent.promote_to_governed_knowledge or (
            request.approval_kind is ApprovalKind.CORE_GOVERNANCE_ESCALATION
        ):
            # The SPI reports that the governance path is required. It never
            # takes it, and a turn ending never takes it either.
            return self._succeed(
                request,
                profile,
                Disposition.ESCALATION_REQUIRED,
                Reason.GOVERNANCE_ESCALATION,
                result={"required_path": "knowledge.govern"},
                compose=False,
            )
        return self._succeed(
            request, profile, Disposition.ACCEPTED, Reason.ACCEPTED, result={"accepted": True}
        )

    # --- negotiation (section 7 matrix) --------------------------------------

    def _negotiate(self, request: SpiRequest, profile: ProviderProfile) -> HookOutcome:
        if request.intent.requires_host_source_patch:
            return self._refuse(
                request, profile, Reason.HOST_SOURCE_PATCH, ERROR_CODE_INVALID_REQUEST
            )
        declared = request.declared_spi_version or _SPI_MIN
        declared_parts = _version_parts(declared)
        if declared_parts[0] != _version_parts(_SPI_MAX)[0]:
            return self._refuse(
                request,
                profile,
                Reason.NEGOTIATED,
                ERROR_CODE_INCOMPATIBLE_VERSION,
                status=COMPATIBILITY_STATUS_INCOMPATIBLE,
            )
        if request.workspace in profile.workspaces_below_minimum_format:
            return self._refuse(
                request,
                profile,
                Reason.NEGOTIATED,
                ERROR_CODE_WORKSPACE_MIGRATION_REQUIRED,
                status=COMPATIBILITY_STATUS_UPGRADE_REQUIRED,
                upgrade_state=UPGRADE_STATE_REQUIRED,
            )
        unsupported = set(request.required_capabilities) - profile.supported_capabilities
        if unsupported:
            return self._refuse(
                request,
                profile,
                Reason.NEGOTIATED,
                ERROR_CODE_CAPABILITY_NOT_GRANTED,
                status=COMPATIBILITY_STATUS_INCOMPATIBLE,
            )
        selected = declared if declared_parts <= _version_parts(_SPI_MAX) else _SPI_MAX
        deprecated = set(request.required_capabilities) & profile.deprecated_capabilities
        status = (
            COMPATIBILITY_STATUS_COMPATIBLE_WITH_DEPRECATIONS
            if deprecated
            else COMPATIBILITY_STATUS_COMPATIBLE
        )
        self._negotiated = True
        self._selected_spi_version = selected
        return self._succeed(
            request,
            profile,
            Disposition.NEGOTIATED,
            Reason.NEGOTIATED,
            result={"selected_spi_version": selected},
            status=status,
            selected_spi=selected,
        )

    # --- outcome construction ------------------------------------------------

    def _refuse(
        self,
        request: SpiRequest,
        profile: ProviderProfile,
        reason: Reason,
        code: str,
        *,
        status: str | None = None,
        upgrade_state: str = UPGRADE_STATE_NONE,
    ) -> HookOutcome:
        retry_class = frozen_retry_class(code)
        audit = self._audits(request, reason)
        error = ApiError(code=code, message=reason.value, retry_class=retry_class)
        return HookOutcome(
            hook=request.hook,
            disposition=Disposition.REFUSED,
            reason=reason,
            audit_record_required=audit,
            response=self._envelope(
                request,
                profile,
                audit=audit,
                error=error,
                status=status,
                upgrade_state=upgrade_state,
            ),
            error_code=code,
            retry_class=retry_class,
            compatibility_status=status,
            version_axes=profile.axes if request.hook is Hook.NEGOTIATE else None,
            granted_capabilities=request.granted_capabilities,
            effective_capabilities=self._effective(request, profile),
        )

    def _succeed(
        self,
        request: SpiRequest,
        profile: ProviderProfile,
        disposition: Disposition,
        reason: Reason,
        *,
        result: dict[str, object],
        compose: bool = True,
        status: str | None = None,
        selected_spi: str | None = None,
        job_handles: tuple[JobReference, ...] = (),
    ) -> HookOutcome:
        audit = self._audits(request, reason)
        operations = _HOOK_COMPOSITIONS[request.hook] if compose else ()
        envelopes = tuple(
            self._compose(request, profile, operation) for operation in operations
        )
        axes = profile.axes
        if selected_spi is not None:
            axes = VersionAxes(
                spi=selected_spi,
                api=axes.api,
                server=axes.server,
                workspace_format=axes.workspace_format,
                client=axes.client,
            )
        return HookOutcome(
            hook=request.hook,
            disposition=disposition,
            reason=reason,
            audit_record_required=audit,
            response=self._envelope(
                request, profile, audit=audit, result=result, status=status, job=_first(job_handles)
            ),
            compatibility_status=status,
            version_axes=axes if request.hook is Hook.NEGOTIATE else None,
            composed_operations=operations,
            nested_envelopes=envelopes,
            job_handles=job_handles,
            granted_capabilities=request.granted_capabilities,
            effective_capabilities=self._effective(request, profile),
        )

    def _audits(self, request: SpiRequest, reason: Reason) -> bool:
        if request.hook in RUN_LEVEL_HOOKS:
            return False
        if (
            request.hook is Hook.APPROVAL_REQUEST
            and request.approval_kind is ApprovalKind.RUNTIME_TOOL_APPROVAL
        ):
            return False
        if reason in UNAUDITED_REASONS:
            return False
        return not (request.hook in READ_ONLY_HOOKS and reason in READ_ONLY_UNAUDITED_REASONS)

    def _compose(
        self, request: SpiRequest, profile: ProviderProfile, operation: str
    ) -> RequestEnvelope:
        """Build the nested frozen envelope, carrying no SPI-local provenance."""
        effective = self._effective(request, profile)
        deadline = request.deadline_ms if request.deadline_ms is not None else 0
        return build_nested_envelope(
            operation,
            request_id=_request_id(request, operation),
            api_version=profile.axes.api,
            client=ClientIdentity(id="omnivia-core-agent-host-mock", version="0.6.8"),
            workspace_id=request.workspace,
            scopes=tuple(sorted({token.split("@", 1)[0] for token in effective})),
            purpose=request.purpose,
            required_capabilities=tuple(
                CapabilityRequirement(
                    id=ref.id, minimum_version=ref.version, required=True
                )
                for ref in sorted(
                    (parse_capability_token(token) for token in request.required_capabilities),
                    key=lambda item: (item.id, item.version),
                )
            ),
            principal_claim=PrincipalClaim(claimed_principal_id=request.caller),
            deadline_ms=deadline,
            idempotency_key=request.idempotency_key
            if request.hook in EFFECTING_HOOKS
            else None,
        )

    def _envelope(
        self,
        request: SpiRequest,
        profile: ProviderProfile,
        *,
        audit: bool,
        result: dict[str, object] | None = None,
        error: ApiError | None = None,
        status: str | None = None,
        upgrade_state: str = UPGRADE_STATE_NONE,
        job: JobReference | None = None,
    ) -> ResponseEnvelope:
        effective = self._effective(request, profile)
        request_id = _request_id(request, request.hook.value)
        capabilities = CapabilitySet(
            supported=_refs(sorted(profile.supported_capabilities)),
            granted=_refs(request.granted_capabilities),
            effective=_refs(effective),
        )
        deprecations = tuple(
            Deprecation(id=token.split("@", 1)[0], since=profile.axes.api, removal="2.0")
            for token in sorted(profile.deprecated_capabilities)
            if token in set(request.required_capabilities)
        )
        version = build_version_envelope(
            profile.axes,
            status=status or COMPATIBILITY_STATUS_COMPATIBLE,
            upgrade_state=upgrade_state,
            supported_api=VersionWindow(minimum=profile.axes.api, maximum=profile.axes.api),
            supported_workspace=VersionWindow(
                minimum=profile.axes.workspace_format, maximum=profile.axes.workspace_format
            ),
            deprecations=deprecations,
            capabilities=capabilities,
        )
        return build_response(
            request_id=request_id,
            version=version,
            authority=GrantedAuthority(
                principal_id=request.caller, roles=(), capabilities=_refs(effective)
            ),
            audit_reference=f"audit-{request_id}" if audit else None,
            result=result,
            error=error,
            job=job,
        )

    # --- ephemeral state -----------------------------------------------------

    def _run_state(self, request: SpiRequest) -> _RunState:
        run = self._runs.get(request.provenance.run)
        if run is None:
            if len(self._runs) >= MAX_TRACKED_RUNS:
                raise SpiContractError("tracked-run bound exceeded")
            run = _RunState(workspace=request.workspace)
            self._runs[request.provenance.run] = run
        return run

    def _open_turn(self, run: _RunState, turn: int) -> None:
        if len(run.turns) >= MAX_TRACKED_TURNS_PER_RUN:
            raise SpiContractError("tracked-turn bound exceeded")
        run.turns[turn] = _OPEN
        run.frontier = max(run.frontier, turn)

    def _record(self, request: SpiRequest, outcome: HookOutcome) -> HookOutcome:
        """Reserve the idempotency key when the failure is one a retry may repeat.

        A retryable failure leaves the effect uncertain, so the key is what
        makes the retry safe. A caller defect leaves nothing uncertain and must
        not burn the key.
        """
        key = request.idempotency_key
        if key is None or key in self._ledger:
            return outcome
        if outcome.disposition is Disposition.REFUSED and (
            outcome.retry_class not in RETRYABLE_RETRY_CLASSES
        ):
            return outcome
        if len(self._ledger) >= MAX_IDEMPOTENCY_ENTRIES:
            raise SpiContractError("idempotency ledger bound exceeded")
        self._ledger[key] = _LedgerEntry(
            digest=request.payload_digest,
            disposition=outcome.disposition,
            reason=outcome.reason,
            audit_record_required=outcome.audit_record_required,
        )
        return outcome

    def _effective(self, request: SpiRequest, profile: ProviderProfile) -> tuple[str, ...]:
        return resolve_effective_capabilities(
            tuple(sorted(profile.supported_capabilities)), request.granted_capabilities
        )

    def _outstanding_jobs(self, request: SpiRequest) -> tuple[JobReference, ...]:
        """Handles the host may act on, returned unchanged and never cancelled.

        The mock has no job store, so this is the identity of the turn's own
        work and nothing more. What matters is that it is *returned* rather than
        acted on: the host makes its own explicitly authorized `job.cancel`.
        """
        turn = request.provenance.turn_ordinal
        return (JobReference(job_id=f"job-{request.provenance.run}-{turn}"),)


# --- helpers -----------------------------------------------------------------


def _version_parts(version: str) -> tuple[int, int, int]:
    major, minor, patch = version.split(".")
    return int(major), int(minor), int(patch)


def _refs(tokens: tuple[str, ...] | list[str]) -> tuple[CapabilityRef, ...]:
    return tuple(parse_capability_token(token) for token in tokens)


def _first(handles: tuple[JobReference, ...]) -> JobReference | None:
    return handles[0] if handles else None


def _request_id(request: SpiRequest, operation: str) -> str:
    """A request identifier the frozen pattern accepts: no underscore, no space."""
    slug = operation.replace("_", "-").replace(".", "-")
    return f"spi-{slug}-{request.provenance.sequence}"


__all__ = [
    "DEFAULT_PROFILE",
    "MAX_IDEMPOTENCY_ENTRIES",
    "MAX_JOURNAL_ENTRIES",
    "MAX_TRACKED_RUNS",
    "MAX_TRACKED_TURNS_PER_RUN",
    "READ_ONLY_UNAUDITED_REASONS",
    "UNAUDITED_REASONS",
    "MockProvider",
    "ProviderProfile",
]
