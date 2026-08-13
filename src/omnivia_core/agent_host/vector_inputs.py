"""Input recipes for provider-SPI vectors `SPI-V-001` to `SPI-V-024` (V06-8, A9-P2).

Standard library only. One function -- :func:`prepare_vector` -- turns a loaded
:class:`~omnivia_core.agent_host.conformance.ConformanceCase` into a
:class:`VectorScenario`: a fresh :class:`~omnivia_core.agent_host.mock.MockProvider`
that has already been driven through whatever preconditions the case's `when`
describes, plus the one real
:class:`~omnivia_core.agent_host.spi.SpiRequest` a caller then hands to
``scenario.provider.handle(scenario.request)``.

**Inputs only.** Nothing here reads, derives, restates or looks up what the
corpus fixes as the right answer for a case. A recipe branches on ``case.id``
to choose *setup* -- which turns are opened first, which key is already spent,
which sequence has already been served -- and never to choose a result. That
separation is the point: a recipe module that knew the answers would let a
provider be graded against a table rather than against its own behaviour.

**Deterministic.** No clock, no randomness, no iteration over an unordered
container. Every precondition is a fixed ladder of real hook calls through the
real provider, so a scenario built twice from the same case is the same
scenario.

Every scalar the wrapper carries is read off ``case.given`` (and the approval
kind off ``case.when``). A recipe supplies a value of its own only where a
precondition needs one the observed call must not share -- an earlier sequence,
a differing payload digest, a turn-control purpose.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, cast

from omnivia_core.agent_host.conformance import ConformanceCase
from omnivia_core.agent_host.mock import MockProvider, ProviderProfile
from omnivia_core.agent_host.spi import (
    RUN_LEVEL_HOOKS,
    ApprovalKind,
    Hook,
    HookIntent,
    SpiProvenance,
    SpiRequest,
)

#: The deadline a case that states none is driven with. Well inside the
#: wrapper's bound, and paired with a zero elapsed time so the deadline ladder
#: is not what a scenario runs into.
DEFAULT_DEADLINE_MS: Final = 30_000

#: The run position used by the original nine recipes when a case states none.
#: This remains zero so extending the recipe catalogue does not change their
#: previously accepted inputs.
DEFAULT_SEQUENCE: Final = 0

#: Later cases need a real precondition ladder below the observed call. The run's
#: sequence is strictly increasing, so their unstated position leaves room for
#: the deepest ladder here (two calls) beneath it.
_PRECONDITIONED_SEQUENCE: Final = 4

#: The accepted initial slice, whose unstated sequence predates the ladders.
_INITIAL_CASE_IDS: Final = tuple(f"SPI-V-{index:03d}" for index in range(1, 10))

#: The wrapper's own default digest, named so a recipe can state a *differing*
#: one against it.
_DEFAULT_DIGEST: Final = "empty"

#: A second digest for the same key: the divergent redelivery in `SPI-V-002`.
_DIVERGENT_DIGEST: Final = "capture-divergent"

#: The content handle both deliveries of `SPI-V-004` name.
_TOOL_RESULT_HANDLE: Final = "content-handle-0007"

#: The turn ordinals `SPI-V-006` opens before the observed call arrives for a
#: later one, and the single ordinal `SPI-V-008` compacts midway through.
_OPENED_TURN_ORDINALS: Final[tuple[int, ...]] = (0, 1)
_OPEN_TURN_ORDINAL: Final = 0

#: The sequence `SPI-V-007` has already served when the delayed recall arrives.
_SERVED_SEQUENCE: Final = 5

#: A turn-control call carries a turn-control purpose, not the purpose of the
#: hook the case observes.
_TURN_CONTROL_PURPOSE: Final = "turn_control"

#: Sentinels: `None` is a meaningful value for all three, so "not overridden"
#: needs a value none of the fields can legitimately hold -- a negative ordinal,
#: a key carrying a control character, and a negative deadline.
_KEEP_ORDINAL: Final = -1
_KEEP_KEY: Final = "\x00"
_KEEP_DEADLINE: Final = -1

#: The workspace `SPI-V-011` binds the run to before the rebinding call arrives.
#: It has to differ from the workspace the case names, which is the second one.
_BOUND_WORKSPACE: Final = "workspace-gamma-bound"

#: A recall purpose for the precondition of a case whose observed call carries a
#: purpose the server does not allowlist (`SPI-V-013`).
_RECALL_PURPOSE: Final = "context_recall"

#: The purpose of the capture `SPI-V-019` has already committed. The case's own
#: purpose belongs to the cancellation it observes, not to that capture.
_CAPTURE_PURPOSE: Final = "assistant_turn_capture"

#: Keys the precondition calls of the cancellation cases spend. Each differs
#: from the key its case's observed call carries, so a precondition never lands
#: on the observed call's ledger entry.
_PRIOR_CANCEL_KEY: Final = "idem-cancel-prior"
_COMMITTED_CAPTURE_KEY: Final = "idem-capture-committed"

#: The remaining time on the parent recall `SPI-V-023`'s `when` describes, in
#: milliseconds. The nested search declares its own deadline against this.
_PARENT_REMAINING_MS: Final = 2_000

#: The healthy server. Preconditions run against it, so the hostile condition a
#: case's `when` describes is met by the observed call and by nothing before it.
_HEALTHY_PROFILE: Final = ProviderProfile()


class VectorInputError(ValueError):
    """A case this module has no recipe for, or one whose input is malformed."""


# --- reading the case ------------------------------------------------------------


def _label(case: ConformanceCase, key: str) -> str:
    value = case.given.get(key)
    if not isinstance(value, str):
        raise VectorInputError(f"{case.id} given.{key} must be a string")
    return value


def _optional_label(case: ConformanceCase, key: str) -> str | None:
    if key not in case.given:
        return None
    return _label(case, key)


def _whole(case: ConformanceCase, key: str, default: int) -> int:
    value = case.given.get(key, default)
    # `bool` is an `int` in Python and is refused here on purpose.
    if isinstance(value, bool) or not isinstance(value, int):
        raise VectorInputError(f"{case.id} given.{key} must be an integer")
    return value


def _optional_whole(case: ConformanceCase, key: str) -> int | None:
    if key not in case.given:
        return None
    return _whole(case, key, 0)


def _capabilities(case: ConformanceCase) -> tuple[str, ...]:
    value = case.given.get("granted_capabilities", ())
    # A `str` is a `Sequence[str]`, so it would pass a per-entry check silently.
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise VectorInputError(f"{case.id} given.granted_capabilities must be a list")
    entries = tuple(cast("Sequence[object]", value))
    if not all(isinstance(entry, str) for entry in entries):
        raise VectorInputError(
            f"{case.id} given.granted_capabilities must hold only strings"
        )
    return cast("tuple[str, ...]", entries)


def _sequence_of(case: ConformanceCase) -> int:
    """The stated run position, preserving the initial slice's zero default."""
    default = (
        DEFAULT_SEQUENCE if case.id in _INITIAL_CASE_IDS else _PRECONDITIONED_SEQUENCE
    )
    return _whole(case, "sequence", default)


def _when_capabilities(case: ConformanceCase) -> tuple[str, ...]:
    """The capabilities `when` says the call requires; none if it says nothing."""
    value = case.when.get("required_capabilities", ())
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise VectorInputError(f"{case.id} when.required_capabilities must be a list")
    entries = tuple(cast("Sequence[object]", value))
    if not all(isinstance(entry, str) for entry in entries):
        raise VectorInputError(
            f"{case.id} when.required_capabilities must hold only strings"
        )
    return cast("tuple[str, ...]", entries)


def _when_elapsed_ms(case: ConformanceCase) -> int:
    """How long `when` says the call had already been running."""
    value = case.when.get("elapsed_ms", 0)
    if isinstance(value, bool) or not isinstance(value, int):
        raise VectorInputError(f"{case.id} when.elapsed_ms must be an integer")
    return value


def _approval_kind(case: ConformanceCase) -> ApprovalKind | None:
    value = case.when.get("approval_kind")
    if value is None:
        return None
    if not isinstance(value, str):
        raise VectorInputError(f"{case.id} when.approval_kind must be a string")
    try:
        return ApprovalKind(value)
    except ValueError as error:
        raise VectorInputError(
            f"{case.id} names unknown approval kind {value!r}"
        ) from error


# --- building one call -----------------------------------------------------------


def _request(
    case: ConformanceCase,
    *,
    hook: Hook | None = None,
    sequence: int | None = None,
    turn_ordinal: int | None = _KEEP_ORDINAL,
    idempotency_key: str | None = _KEEP_KEY,
    purpose: str | None = None,
    workspace: str | None = None,
    payload_digest: str = _DEFAULT_DIGEST,
    intent: HookIntent | None = None,
    required_capabilities: tuple[str, ...] | None = None,
    elapsed_ms: int | None = None,
    deadline_ms: int | None = _KEEP_DEADLINE,
    parent_remaining_ms: int | None = None,
) -> SpiRequest:
    """One real hook call built from `case`, with the stated fields substituted.

    Defaults come from the case; an override is what a precondition states when
    it needs a value the observed call must not share. The turn coordinate
    follows the wrapper's own partition rather than the case: a run-level hook
    never carries one, whatever the case states for the hook it observes.
    """
    chosen = hook if hook is not None else case.hook
    ordinal = (
        _optional_whole(case, "turn_ordinal")
        if turn_ordinal == _KEEP_ORDINAL
        else turn_ordinal
    )
    key = (
        _optional_label(case, "idempotency_key")
        if idempotency_key == _KEEP_KEY
        else idempotency_key
    )
    return SpiRequest(
        hook=chosen,
        caller=_label(case, "caller"),
        workspace=workspace if workspace is not None else _label(case, "workspace"),
        purpose=purpose if purpose is not None else _label(case, "purpose"),
        provenance=SpiProvenance(
            agent=_label(case, "agent"),
            session=_label(case, "session"),
            run=_label(case, "run"),
            sequence=sequence if sequence is not None else _sequence_of(case),
            turn_ordinal=None if chosen in RUN_LEVEL_HOOKS else ordinal,
        ),
        granted_capabilities=_capabilities(case),
        required_capabilities=(
            _when_capabilities(case)
            if required_capabilities is None
            else required_capabilities
        ),
        deadline_ms=(
            _whole(case, "deadline_ms", DEFAULT_DEADLINE_MS)
            if deadline_ms == _KEEP_DEADLINE
            else deadline_ms
        ),
        elapsed_ms=(_when_elapsed_ms(case) if elapsed_ms is None else elapsed_ms),
        parent_remaining_ms=parent_remaining_ms,
        idempotency_key=key,
        payload_digest=payload_digest,
        approval_kind=(
            _approval_kind(case) if chosen is Hook.APPROVAL_REQUEST else None
        ),
        intent=intent if intent is not None else HookIntent(),
    )


def _drive(
    provider: MockProvider,
    log: list[SpiRequest],
    request: SpiRequest,
    *,
    profile: ProviderProfile | None = None,
) -> None:
    """Run one precondition through the real provider and record that it ran.

    What it produced is deliberately not inspected: a recipe that read an
    outcome would be a recipe that could branch on one. ``profile`` states the
    server conditions for this precondition alone, which is how a case whose
    hostile condition is a server condition still gets a live run beneath it.
    """
    provider.handle(request, profile=profile)
    log.append(request)


# --- the scenario ----------------------------------------------------------------


@dataclass(frozen=True)
class VectorScenario:
    """A provider standing at the moment before one case's observed call.

    `provider` has already been driven through `setup`; `request` is the call a
    caller then hands to it. `setup` is the ordered trace of the precondition
    calls, for diagnostics only -- it records what was driven, never what came
    back.
    """

    case_id: str
    provider: MockProvider
    request: SpiRequest
    setup: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.case_id not in SUPPORTED_CASE_IDS:
            raise VectorInputError(f"{self.case_id!r} has no recipe")
        if not isinstance(self.provider, MockProvider):
            raise VectorInputError("provider must be a MockProvider")
        if not isinstance(self.request, SpiRequest):
            raise VectorInputError("request must be an SpiRequest")
        if isinstance(self.setup, str) or not isinstance(self.setup, Sequence):
            raise VectorInputError("setup must be a sequence")
        entries = tuple(cast("Sequence[object]", self.setup))
        if not all(isinstance(entry, str) for entry in entries):
            raise VectorInputError("setup must hold only strings")
        object.__setattr__(self, "setup", entries)


def _trace(request: SpiRequest) -> str:
    ordinal = request.provenance.turn_ordinal
    tail = "" if ordinal is None else f"#turn{ordinal}"
    return f"{request.hook.value}@{request.provenance.sequence}{tail}"


# --- the recipes -----------------------------------------------------------------


def _first_capture_delivered(
    case: ConformanceCase, provider: MockProvider, log: list[SpiRequest]
) -> None:
    """Open the turn the case names and land one capture under its key."""
    sequence = _sequence_of(case)
    _drive(
        provider,
        log,
        _request(
            case,
            hook=Hook.RECALL_BEFORE_TURN,
            sequence=sequence - 2,
            idempotency_key=None,
        ),
    )
    _drive(provider, log, _request(case, sequence=sequence - 1))


def _replayed_capture(
    case: ConformanceCase, provider: MockProvider, log: list[SpiRequest]
) -> SpiRequest:
    """SPI-V-001: the byte-identical redelivery, key and digest unchanged."""
    _first_capture_delivered(case, provider, log)
    return _request(case)


def _divergent_capture(
    case: ConformanceCase, provider: MockProvider, log: list[SpiRequest]
) -> SpiRequest:
    """SPI-V-002: the same spent key, carrying a different payload."""
    _first_capture_delivered(case, provider, log)
    return _request(case, payload_digest=_DIVERGENT_DIGEST)


def _keyless_persist(
    case: ConformanceCase, provider: MockProvider, log: list[SpiRequest]
) -> SpiRequest:
    """SPI-V-003: an effecting hook with no key at all. Negotiation is enough."""
    return _request(case)


def _replayed_tool_result(
    case: ConformanceCase, provider: MockProvider, log: list[SpiRequest]
) -> SpiRequest:
    """SPI-V-004: the same by-reference persistence, delivered a second time."""
    sequence = _sequence_of(case)
    intent = HookIntent(content_reference=_TOOL_RESULT_HANDLE)
    _drive(
        provider,
        log,
        _request(
            case,
            hook=Hook.RECALL_BEFORE_TURN,
            sequence=sequence - 2,
            idempotency_key=None,
        ),
    )
    _drive(provider, log, _request(case, sequence=sequence - 1, intent=intent))
    return _request(case, intent=intent)


def _replayed_approval(
    case: ConformanceCase, provider: MockProvider, log: list[SpiRequest]
) -> SpiRequest:
    """SPI-V-005: a run-local approval already decided under the same key."""
    sequence = _sequence_of(case)
    _drive(
        provider,
        log,
        _request(
            case,
            hook=Hook.RECALL_BEFORE_TURN,
            sequence=sequence - 2,
            idempotency_key=None,
        ),
    )
    _drive(provider, log, _request(case, sequence=sequence - 1))
    return _request(case)


def _capture_for_unopened_turn(
    case: ConformanceCase, provider: MockProvider, log: list[SpiRequest]
) -> SpiRequest:
    """SPI-V-006: only the two earlier ordinals are opened; the case's is not."""
    for ordinal in _OPENED_TURN_ORDINALS:
        _drive(
            provider,
            log,
            _request(
                case,
                hook=Hook.RECALL_BEFORE_TURN,
                sequence=ordinal + 1,
                turn_ordinal=ordinal,
                idempotency_key=None,
            ),
        )
    return _request(case)


def _stale_recall(
    case: ConformanceCase, provider: MockProvider, log: list[SpiRequest]
) -> SpiRequest:
    """SPI-V-007: a later sequence is served first, so the case's is behind it."""
    _drive(provider, log, _request(case, sequence=_SERVED_SEQUENCE))
    return _request(case)


def _compaction_notice(
    case: ConformanceCase, provider: MockProvider, log: list[SpiRequest]
) -> SpiRequest:
    """SPI-V-008: a turn is open when the host-local notice arrives."""
    _drive(
        provider,
        log,
        _request(
            case,
            hook=Hook.RECALL_BEFORE_TURN,
            sequence=_sequence_of(case) - 1,
            turn_ordinal=_OPEN_TURN_ORDINAL,
            idempotency_key=None,
        ),
    )
    return _request(case)


def _capture_after_terminal_turn(
    case: ConformanceCase, provider: MockProvider, log: list[SpiRequest]
) -> SpiRequest:
    """SPI-V-009: the case's own turn is opened and then closed by `turn.complete`."""
    sequence = _sequence_of(case)
    _drive(
        provider,
        log,
        _request(
            case,
            hook=Hook.RECALL_BEFORE_TURN,
            sequence=sequence - 2,
            idempotency_key=None,
        ),
    )
    _drive(
        provider,
        log,
        _request(
            case,
            hook=Hook.TURN_COMPLETE,
            sequence=sequence - 1,
            purpose=_TURN_CONTROL_PURPOSE,
            idempotency_key=None,
        ),
    )
    return _request(case)


# --- the recipes: denial, cancellation and deadline conditions -------------------


def _live_turn(
    case: ConformanceCase,
    provider: MockProvider,
    log: list[SpiRequest],
    *,
    sequence: int,
    turn_ordinal: int | None = _KEEP_ORDINAL,
    workspace: str | None = None,
    purpose: str | None = None,
) -> None:
    """Open the turn the observed call belongs to, on a healthy server.

    One before-turn recall, carrying no key, requiring nothing and having run
    for no time: the plain call that puts a run and a turn underneath the case.
    """
    _drive(
        provider,
        log,
        _request(
            case,
            hook=Hook.RECALL_BEFORE_TURN,
            sequence=sequence,
            turn_ordinal=turn_ordinal,
            idempotency_key=None,
            workspace=workspace,
            purpose=purpose,
            required_capabilities=(),
            elapsed_ms=0,
        ),
        profile=_HEALTHY_PROFILE,
    )


def _unauthenticated_recall(
    case: ConformanceCase, provider: MockProvider, log: list[SpiRequest]
) -> SpiRequest:
    """SPI-V-010: the same run, then a claim with no authenticated session behind it.

    The condition is the server's, so it is carried by the provider's own
    profile -- built from the caller the case names -- and the precondition ran
    before it applied.
    """
    _live_turn(case, provider, log, sequence=_sequence_of(case) - 1)
    return _request(case)


def _rebound_workspace_recall(
    case: ConformanceCase, provider: MockProvider, log: list[SpiRequest]
) -> SpiRequest:
    """SPI-V-011: the run is bound to a first workspace; the case names a second."""
    _live_turn(
        case,
        provider,
        log,
        sequence=_sequence_of(case) - 1,
        workspace=_BOUND_WORKSPACE,
    )
    return _request(case)


def _capture_without_write(
    case: ConformanceCase, provider: MockProvider, log: list[SpiRequest]
) -> SpiRequest:
    """SPI-V-012: a live turn, then a capture requiring what `when` names."""
    _live_turn(case, provider, log, sequence=_sequence_of(case) - 1)
    return _request(case)


def _search_with_unallowlisted_purpose(
    case: ConformanceCase, provider: MockProvider, log: list[SpiRequest]
) -> SpiRequest:
    """SPI-V-013: the precondition declares a purpose the server knows; the case's does not."""
    _live_turn(
        case, provider, log, sequence=_sequence_of(case) - 1, purpose=_RECALL_PURPOSE
    )
    return _request(case)


def _recall_requesting_wider_capabilities(
    case: ConformanceCase, provider: MockProvider, log: list[SpiRequest]
) -> SpiRequest:
    """SPI-V-014: the recalled text's embedded instruction, forwarded as a requirement.

    The precondition requires nothing; the observed call requires what `when`
    names, which is the widening the recalled evidence asked for.
    """
    _live_turn(case, provider, log, sequence=_sequence_of(case) - 1)
    return _request(case)


def _capture_promoting_to_governed(
    case: ConformanceCase, provider: MockProvider, log: list[SpiRequest]
) -> SpiRequest:
    """SPI-V-015: capture asked for a governed record rather than a candidate one."""
    _live_turn(case, provider, log, sequence=_sequence_of(case) - 1)
    return _request(case, intent=HookIntent(promote_to_governed_knowledge=True))


def _search_while_lease_held(
    case: ConformanceCase, provider: MockProvider, log: list[SpiRequest]
) -> SpiRequest:
    """SPI-V-016: the turn opened before another writer took the workspace lease."""
    _live_turn(case, provider, log, sequence=_sequence_of(case) - 1)
    return _request(case)


def _cancel_with_recall_in_flight(
    case: ConformanceCase, provider: MockProvider, log: list[SpiRequest]
) -> SpiRequest:
    """SPI-V-017: the before-turn recall the cancellation interrupts."""
    _live_turn(
        case, provider, log, sequence=_sequence_of(case) - 1, purpose=_RECALL_PURPOSE
    )
    return _request(case)


def _repeat_cancel(
    case: ConformanceCase, provider: MockProvider, log: list[SpiRequest]
) -> SpiRequest:
    """SPI-V-018: the turn is opened and then cancelled once before the second call."""
    sequence = _sequence_of(case)
    _live_turn(case, provider, log, sequence=sequence - 2, purpose=_RECALL_PURPOSE)
    _drive(
        provider,
        log,
        _request(
            case,
            sequence=sequence - 1,
            idempotency_key=_PRIOR_CANCEL_KEY,
            required_capabilities=(),
            elapsed_ms=0,
        ),
        profile=_HEALTHY_PROFILE,
    )
    return _request(case)


def _cancel_after_capture(
    case: ConformanceCase, provider: MockProvider, log: list[SpiRequest]
) -> SpiRequest:
    """SPI-V-019: a capture has already landed in the turn the cancellation names."""
    sequence = _sequence_of(case)
    _live_turn(case, provider, log, sequence=sequence - 2, purpose=_RECALL_PURPOSE)
    _drive(
        provider,
        log,
        _request(
            case,
            hook=Hook.CAPTURE_AFTER_TURN,
            sequence=sequence - 1,
            purpose=_CAPTURE_PURPOSE,
            idempotency_key=_COMMITTED_CAPTURE_KEY,
            required_capabilities=(),
            elapsed_ms=0,
        ),
        profile=_HEALTHY_PROFILE,
    )
    return _request(case)


def _cancel_composing_core_job_control(
    case: ConformanceCase, provider: MockProvider, log: list[SpiRequest]
) -> SpiRequest:
    """SPI-V-020: cancellation asked to take the Core durable job with it."""
    _live_turn(
        case, provider, log, sequence=_sequence_of(case) - 1, purpose=_RECALL_PURPOSE
    )
    return _request(case, intent=HookIntent(compose_core_job_control=True))


def _recall_past_its_deadline(
    case: ConformanceCase, provider: MockProvider, log: list[SpiRequest]
) -> SpiRequest:
    """SPI-V-021: the elapsed time `when` states, against the deadline `given` states."""
    _live_turn(case, provider, log, sequence=_sequence_of(case) - 1)
    return _request(case)


def _capture_past_its_deadline(
    case: ConformanceCase, provider: MockProvider, log: list[SpiRequest]
) -> SpiRequest:
    """SPI-V-022: the same, for a hook whose effect the host cannot see either way."""
    _live_turn(
        case, provider, log, sequence=_sequence_of(case) - 1, purpose=_RECALL_PURPOSE
    )
    return _request(case)


def _nested_search_beyond_parent(
    case: ConformanceCase, provider: MockProvider, log: list[SpiRequest]
) -> SpiRequest:
    """SPI-V-023: the parent recall, and the search nested inside what is left of it."""
    _live_turn(case, provider, log, sequence=_sequence_of(case) - 1)
    return _request(case, parent_remaining_ms=_PARENT_REMAINING_MS)


def _recall_without_a_deadline(
    case: ConformanceCase, provider: MockProvider, log: list[SpiRequest]
) -> SpiRequest:
    """SPI-V-024: the adapter omits the deadline and leans on a server default."""
    _live_turn(case, provider, log, sequence=_sequence_of(case) - 1)
    return _request(case, deadline_ms=None)


_Recipe = Callable[[ConformanceCase, MockProvider, list[SpiRequest]], SpiRequest]

_RECIPES: Final[MappingProxyType[str, _Recipe]] = MappingProxyType(
    {
        "SPI-V-001": _replayed_capture,
        "SPI-V-002": _divergent_capture,
        "SPI-V-003": _keyless_persist,
        "SPI-V-004": _replayed_tool_result,
        "SPI-V-005": _replayed_approval,
        "SPI-V-006": _capture_for_unopened_turn,
        "SPI-V-007": _stale_recall,
        "SPI-V-008": _compaction_notice,
        "SPI-V-009": _capture_after_terminal_turn,
        "SPI-V-010": _unauthenticated_recall,
        "SPI-V-011": _rebound_workspace_recall,
        "SPI-V-012": _capture_without_write,
        "SPI-V-013": _search_with_unallowlisted_purpose,
        "SPI-V-014": _recall_requesting_wider_capabilities,
        "SPI-V-015": _capture_promoting_to_governed,
        "SPI-V-016": _search_while_lease_held,
        "SPI-V-017": _cancel_with_recall_in_flight,
        "SPI-V-018": _repeat_cancel,
        "SPI-V-019": _cancel_after_capture,
        "SPI-V-020": _cancel_composing_core_job_control,
        "SPI-V-021": _recall_past_its_deadline,
        "SPI-V-022": _capture_past_its_deadline,
        "SPI-V-023": _nested_search_beyond_parent,
        "SPI-V-024": _recall_without_a_deadline,
    }
)


def _profile(case: ConformanceCase) -> ProviderProfile:
    """The server the observed call meets.

    Two cases state a condition that is the server's rather than the call's: a
    caller with no authenticated session behind it, and a workspace whose lease
    another writer holds. Both are read off the case's own identity fields, so
    the profile names the case's caller and workspace rather than a fixed one.
    """
    if case.id == "SPI-V-010":
        return ProviderProfile(
            unauthenticated_callers=frozenset({_label(case, "caller")})
        )
    if case.id == "SPI-V-016":
        return ProviderProfile(leased_workspaces=frozenset({_label(case, "workspace")}))
    return _HEALTHY_PROFILE


#: The cases this slice has recipes for, in corpus order.
SUPPORTED_CASE_IDS: Final[tuple[str, ...]] = tuple(_RECIPES)


def prepare_vector(case: ConformanceCase) -> VectorScenario:
    """Build the scenario for `case`, preconditions already driven.

    Every hook call the setup makes is a real call through a real
    :class:`~omnivia_core.agent_host.mock.MockProvider`, negotiation included:
    a scenario is the provider's actual state, not a description of one.
    """
    if not isinstance(case, ConformanceCase):
        raise VectorInputError("case must be a ConformanceCase")
    recipe = _RECIPES.get(case.id)
    if recipe is None:
        raise VectorInputError(
            f"{case.id} is outside {SUPPORTED_CASE_IDS[0]}..{SUPPORTED_CASE_IDS[-1]}"
        )
    provider = MockProvider(_profile(case))
    log: list[SpiRequest] = []
    _drive(
        provider,
        log,
        _request(
            case,
            hook=Hook.NEGOTIATE,
            sequence=0,
            idempotency_key=None,
            required_capabilities=(),
            elapsed_ms=0,
        ),
        profile=_HEALTHY_PROFILE,
    )
    request = recipe(case, provider, log)
    return VectorScenario(
        case_id=case.id,
        provider=provider,
        request=request,
        setup=tuple(_trace(entry) for entry in log),
    )


__all__ = [
    "DEFAULT_DEADLINE_MS",
    "DEFAULT_SEQUENCE",
    "SUPPORTED_CASE_IDS",
    "VectorInputError",
    "VectorScenario",
    "prepare_vector",
]
