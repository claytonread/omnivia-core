"""Input recipes for the first nine provider-SPI vectors (V06-8, packet A9-P2).

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

#: Sentinels: `None` is a meaningful value for both, so "not overridden" needs a
#: value neither field can legitimately hold -- a negative ordinal, and a key
#: carrying a control character.
_KEEP_ORDINAL: Final = -1
_KEEP_KEY: Final = "\x00"


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
    payload_digest: str = _DEFAULT_DIGEST,
    intent: HookIntent | None = None,
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
        workspace=_label(case, "workspace"),
        purpose=purpose if purpose is not None else _label(case, "purpose"),
        provenance=SpiProvenance(
            agent=_label(case, "agent"),
            session=_label(case, "session"),
            run=_label(case, "run"),
            sequence=sequence if sequence is not None else _whole(case, "sequence", 0),
            turn_ordinal=None if chosen in RUN_LEVEL_HOOKS else ordinal,
        ),
        granted_capabilities=_capabilities(case),
        deadline_ms=_whole(case, "deadline_ms", DEFAULT_DEADLINE_MS),
        idempotency_key=key,
        payload_digest=payload_digest,
        approval_kind=(
            _approval_kind(case) if chosen is Hook.APPROVAL_REQUEST else None
        ),
        intent=intent if intent is not None else HookIntent(),
    )


def _drive(provider: MockProvider, log: list[SpiRequest], request: SpiRequest) -> None:
    """Run one precondition through the real provider and record that it ran.

    What it produced is deliberately not inspected: a recipe that read an
    outcome would be a recipe that could branch on one.
    """
    provider.handle(request)
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
    sequence = _whole(case, "sequence", 0)
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
    sequence = _whole(case, "sequence", 0)
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
    sequence = _whole(case, "sequence", 0)
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
            sequence=_whole(case, "sequence", 0) - 1,
            turn_ordinal=_OPEN_TURN_ORDINAL,
            idempotency_key=None,
        ),
    )
    return _request(case)


def _capture_after_terminal_turn(
    case: ConformanceCase, provider: MockProvider, log: list[SpiRequest]
) -> SpiRequest:
    """SPI-V-009: the case's own turn is opened and then closed by `turn.complete`."""
    sequence = _whole(case, "sequence", 0)
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
    }
)

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
    provider = MockProvider(ProviderProfile())
    log: list[SpiRequest] = []
    _drive(
        provider,
        log,
        _request(case, hook=Hook.NEGOTIATE, sequence=0, idempotency_key=None),
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
    "SUPPORTED_CASE_IDS",
    "VectorInputError",
    "VectorScenario",
    "prepare_vector",
]
