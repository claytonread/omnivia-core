"""Generated lifecycle state-machine sequences for the provider SPI (V06-8, A9-P4).

Standard library only, over :mod:`omnivia_core.agent_host.spi` and
:mod:`omnivia_core.agent_host.mock` and nothing else. This is the second
conformance layer: the corpus lane grades one call per accepted vector, and this
lane drives *ordered runs of calls* through the turn state machine of section
3.2 of the accepted candidate.

**Generated, not enumerated.** Every sequence here is produced from the closed
hook and state sets the SPI already owns -- :data:`~omnivia_core.agent_host.spi.TURN_SCOPED_HOOKS`,
:data:`~omnivia_core.agent_host.spi.RUN_LEVEL_HOOKS`,
:data:`~omnivia_core.agent_host.spi.READ_ONLY_HOOKS`,
:data:`~omnivia_core.agent_host.spi.TURN_CONTROL_HOOKS`, and the two terminal
hooks and four turn effect hooks derived from them below. No sequence is keyed
on a corpus case identifier, and nothing here reads the corpus, its loader or
its expected answers: a family is a loop over a set, so a hook added to a set
without a rule for it produces a sequence that fails rather than a gap nobody
notices.

**The expectation is the rule, not an answer read off a fixture.** A step states
the disposition, reason and error code section 3.2 requires of it -- negotiation
precedes every hook (SPI-R-003), positions are monotonic per run (SPI-R-005),
`turn.complete` and `turn.cancel` are terminal for the identified turn only
while the run stays open (SPI-R-006), and the turn coordinate is a lifecycle
coordinate and never an authority (SPI-R-038). The generator writes those
expectations from the rule it is exercising, which is why a provider that
answered from a lookup table would fail here.

**Deterministic and immutable.** No clock, no randomness, no iteration over an
unordered set: hooks are ordered by their wire value everywhere. Sequences and
results are frozen values, so the same sequence run twice produces equal
results.

Nothing in this module reaches a host adapter, a store, a migration, a run-state
API, Core job control or a network. :func:`run_sequence` builds a fresh
:class:`~omnivia_core.agent_host.mock.MockProvider` and calls
:meth:`~omnivia_core.agent_host.mock.MockProvider.handle` once per step, which
is the whole of its execution surface.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from omnivia_core.agent_host.mock import MockProvider
from omnivia_core.agent_host.spi import (
    EFFECTING_HOOKS,
    READ_ONLY_HOOKS,
    RUN_LEVEL_HOOKS,
    TURN_CONTROL_HOOKS,
    TURN_SCOPED_HOOKS,
    ApprovalKind,
    Disposition,
    Hook,
    HookOutcome,
    Reason,
    SpiProvenance,
    SpiRequest,
)
from omnivia_core.contracts.v1.generated import (
    ERROR_CODE_CONFLICT,
    ERROR_CODE_INVALID_REQUEST,
)

# --- the state sets the families are generated from --------------------------

#: The two hooks that end a turn. Derived from the three control hooks rather
#: than listed, so a fourth control hook cannot quietly become a third terminal.
TERMINAL_HOOKS: Final[frozenset[Hook]] = TURN_CONTROL_HOOKS - {Hook.TURN_RETRY}

#: The turn-scoped hooks that are neither the opener nor turn control: what a
#: terminal turn must refuse `conflict`.
TURN_EFFECT_HOOKS: Final[frozenset[Hook]] = (
    TURN_SCOPED_HOOKS - TURN_CONTROL_HOOKS - {Hook.RECALL_BEFORE_TURN}
)

#: The allowlisted purpose each hook calls with. A purpose outside the server's
#: allowlist is refused before the turn machine is reached, so the lifecycle
#: families would be testing the purpose check instead of the state machine.
HOOK_PURPOSES: Final[MappingProxyType[Hook, str]] = MappingProxyType(
    {
        Hook.NEGOTIATE: "spi_negotiation",
        Hook.RECALL_BEFORE_TURN: "context_recall",
        Hook.MEMORY_SEARCH: "context_recall",
        Hook.CAPTURE_AFTER_TURN: "assistant_turn_capture",
        Hook.TOOL_RESULT_PERSIST: "tool_result_persistence",
        Hook.CONTEXT_COMPACT: "context_compaction",
        Hook.APPROVAL_REQUEST: "runtime_tool_approval",
        Hook.TURN_COMPLETE: "turn_control",
        Hook.TURN_CANCEL: "turn_control",
        Hook.TURN_RETRY: "turn_recovery",
    }
)

_AGENT: Final = "agent-lifecycle"
_SESSION: Final = "session-lifecycle"
_CALLER: Final = "principal-lifecycle"
_WORKSPACE: Final = "workspace-lifecycle"
_DEADLINE_MS: Final = 30_000

#: Two runs and two turns, because run and turn are separate levels: a family
#: that only ever drove one of each would pass against a provider that fused
#: them.
_RUN: Final = "run-lifecycle-first"
_OTHER_RUN: Final = "run-lifecycle-second"
_TURN: Final = 0
_LATER_TURN: Final = 1


class LifecycleSequenceError(ValueError):
    """A sequence, step or result outside the shape this layer admits.

    Raised at construction. A provider that answers a step wrongly produces a
    mismatch, which is reported rather than raised: a failing step is a result,
    a malformed sequence is a caller defect.
    """


def _ordered(hooks: frozenset[Hook]) -> tuple[Hook, ...]:
    """Hooks in wire-value order: no family iterates an unordered set."""
    return tuple(sorted(hooks, key=lambda hook: hook.value))


# --- the value objects -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LifecycleStep:
    """One call in a sequence, and the outcome the lifecycle rules require of it."""

    request: SpiRequest
    disposition: Disposition
    reason: Reason
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request, SpiRequest):
            raise LifecycleSequenceError("request must be an SpiRequest")
        if not isinstance(self.disposition, Disposition):
            raise LifecycleSequenceError("disposition must be a Disposition member")
        if not isinstance(self.reason, Reason):
            raise LifecycleSequenceError("reason must be a Reason member")
        if (self.error_code is not None) != (self.disposition is Disposition.REFUSED):
            raise LifecycleSequenceError("only a refusal carries an error code")

    @property
    def label(self) -> str:
        """Where this call sits: hook, run position and turn, for diagnostics."""
        provenance = self.request.provenance
        turn = provenance.turn_ordinal
        tail = "" if turn is None else f"#turn{turn}"
        return f"{self.request.hook.value}@{provenance.run}:{provenance.sequence}{tail}"


@dataclass(frozen=True, slots=True)
class LifecycleSequence:
    """An ordered run of calls exercising one lifecycle rule.

    ``family`` names the generator that produced it and ``rule`` the requirement
    it exercises, so a caller can select or report by either without any of them
    being addressed by an identifier of its own.
    """

    family: str
    name: str
    rule: str
    steps: tuple[LifecycleStep, ...]

    def __post_init__(self) -> None:
        for name in ("family", "name", "rule"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise LifecycleSequenceError(f"{name} must be a non-empty string")
        if isinstance(self.steps, str) or not isinstance(self.steps, Sequence):
            raise LifecycleSequenceError("steps must be a sequence")
        steps = tuple(self.steps)
        if not steps:
            raise LifecycleSequenceError("a sequence has at least one step")
        if not all(isinstance(step, LifecycleStep) for step in steps):
            raise LifecycleSequenceError("steps must hold only LifecycleStep entries")
        object.__setattr__(self, "steps", steps)

    @property
    def hooks(self) -> tuple[Hook, ...]:
        return tuple(step.request.hook for step in self.steps)


@dataclass(frozen=True, slots=True)
class StepResult:
    """What one step produced, against what its rule required."""

    step: LifecycleStep
    outcome: HookOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.step, LifecycleStep):
            raise LifecycleSequenceError("step must be a LifecycleStep")
        if not isinstance(self.outcome, HookOutcome):
            raise LifecycleSequenceError("outcome must be a HookOutcome")

    @property
    def mismatches(self) -> tuple[str, ...]:
        """Every field on which the outcome differed, in a fixed order."""
        step = self.step
        outcome = self.outcome
        compared = (
            ("hook", step.request.hook, outcome.hook),
            ("disposition", step.disposition, outcome.disposition),
            ("reason", step.reason, outcome.reason),
            ("error_code", step.error_code, outcome.error_code),
        )
        return tuple(
            f"{field}: expected {_render(expected)}, observed {_render(observed)}"
            for field, expected, observed in compared
            if expected != observed
        )

    @property
    def passed(self) -> bool:
        return not self.mismatches


@dataclass(frozen=True, slots=True)
class SequenceResult:
    """Every step of one sequence, run in order against one provider.

    ``handled_calls`` is read off the provider's journal after the run, so a
    caller can see that the sequence made exactly one call per step rather than
    take it on trust. A run that handled a different number of calls than the
    sequence has steps did not run this sequence, so it does not pass however
    the steps compared.
    """

    sequence: LifecycleSequence
    steps: tuple[StepResult, ...]
    handled_calls: int

    def __post_init__(self) -> None:
        if not isinstance(self.sequence, LifecycleSequence):
            raise LifecycleSequenceError("sequence must be a LifecycleSequence")
        if isinstance(self.steps, str) or not isinstance(self.steps, Sequence):
            raise LifecycleSequenceError("steps must be a sequence")
        steps = tuple(self.steps)
        if not all(isinstance(step, StepResult) for step in steps):
            raise LifecycleSequenceError("steps must hold only StepResult entries")
        if len(steps) != len(self.sequence.steps):
            raise LifecycleSequenceError("a result covers every step of its sequence")
        if type(self.handled_calls) is not int or self.handled_calls < 0:
            raise LifecycleSequenceError("handled_calls must be a whole number")
        object.__setattr__(self, "steps", steps)

    @property
    def call_count_matches(self) -> bool:
        return self.handled_calls == len(self.sequence.steps)

    @property
    def passed(self) -> bool:
        return self.call_count_matches and all(step.passed for step in self.steps)

    @property
    def detail(self) -> str:
        """Every differing step, named by its position in the run."""
        parts = [
            f"{step.step.label} {mismatch}"
            for step in self.steps
            for mismatch in step.mismatches
        ]
        if not self.call_count_matches:
            parts.append(
                f"handled_calls: expected {len(self.sequence.steps)}, "
                f"observed {self.handled_calls}"
            )
        return "; ".join(parts)


def _render(value: object) -> str:
    return (
        repr(value.value)
        if isinstance(value, Disposition | Reason | Hook)
        else repr(value)
    )


# --- building one call -------------------------------------------------------


def _request(
    hook: Hook,
    *,
    run: str,
    sequence: int,
    turn: int,
) -> SpiRequest:
    """One hook call at a stated position. The turn coordinate follows the partition.

    A run-level hook never carries one whatever the caller asks for, which is
    the partition of section 3.2 applied at the point the call is built rather
    than remembered by every generator.
    """
    return SpiRequest(
        hook=hook,
        caller=_CALLER,
        workspace=_WORKSPACE,
        purpose=HOOK_PURPOSES[hook],
        provenance=SpiProvenance(
            agent=_AGENT,
            session=_SESSION,
            run=run,
            sequence=sequence,
            turn_ordinal=None if hook in RUN_LEVEL_HOOKS else turn,
        ),
        deadline_ms=_DEADLINE_MS,
        idempotency_key=(
            f"idem-{run}-{hook.value}-{sequence}" if hook in EFFECTING_HOOKS else None
        ),
        approval_kind=(
            ApprovalKind.RUNTIME_TOOL_APPROVAL
            if hook is Hook.APPROVAL_REQUEST
            else None
        ),
    )


class _Draft:
    """A sequence under construction, handing out monotonic positions per run.

    Positions are per run because the SPI's `sequence` is per run: two runs in
    one sequence each start at zero, which is what makes run isolation
    observable rather than asserted.
    """

    def __init__(self, family: str, name: str, rule: str) -> None:
        self._family = family
        self._name = name
        self._rule = rule
        self._steps: list[LifecycleStep] = []
        self._next: dict[str, int] = {}

    def add(
        self,
        hook: Hook,
        *,
        disposition: Disposition,
        reason: Reason,
        error_code: str | None = None,
        run: str = _RUN,
        turn: int = _TURN,
        sequence: int | None = None,
    ) -> LifecycleStep:
        """Append one call. An explicit `sequence` replays a position already served."""
        position = self._next.get(run, 0) if sequence is None else sequence
        if sequence is None:
            self._next[run] = position + 1
        step = LifecycleStep(
            request=_request(hook, run=run, sequence=position, turn=turn),
            disposition=disposition,
            reason=reason,
            error_code=error_code,
        )
        self._steps.append(step)
        return step

    def negotiate(self, *, run: str = _RUN) -> LifecycleStep:
        """The handshake every other hook must follow (SPI-R-003)."""
        return self.add(
            Hook.NEGOTIATE,
            disposition=Disposition.NEGOTIATED,
            reason=Reason.NEGOTIATED,
            run=run,
        )

    def open_turn(self, *, run: str = _RUN, turn: int = _TURN) -> LifecycleStep:
        """`recall.before_turn` opens the turn it names."""
        return self.accept(Hook.RECALL_BEFORE_TURN, run=run, turn=turn)

    def accept(
        self, hook: Hook, *, run: str = _RUN, turn: int = _TURN
    ) -> LifecycleStep:
        return self.add(
            hook,
            disposition=Disposition.ACCEPTED,
            reason=Reason.ACCEPTED,
            run=run,
            turn=turn,
        )

    def refuse(
        self,
        hook: Hook,
        *,
        reason: Reason,
        error_code: str,
        run: str = _RUN,
        turn: int = _TURN,
        sequence: int | None = None,
    ) -> LifecycleStep:
        return self.add(
            hook,
            disposition=Disposition.REFUSED,
            reason=reason,
            error_code=error_code,
            run=run,
            turn=turn,
            sequence=sequence,
        )

    def done(self) -> LifecycleSequence:
        return LifecycleSequence(
            family=self._family,
            name=self._name,
            rule=self._rule,
            steps=tuple(self._steps),
        )


# --- the families ------------------------------------------------------------

NEGOTIATION_FIRST: Final = "negotiation-first"
MONOTONIC_SEQUENCE: Final = "monotonic-sequence"
TURN_MUST_BE_OPEN: Final = "turn-must-be-open"
TERMINAL_BLOCKS_EFFECT: Final = "terminal-blocks-effect"
TERMINAL_CONTROL_PAIR: Final = "terminal-control-pair"
RETRY_REOPENS: Final = "retry-reopens"
LATER_TURN_OPENS: Final = "later-turn-opens"
TURN_ISOLATION: Final = "turn-isolation"
RUN_ISOLATION: Final = "run-isolation"
INTERLEAVED_CANCEL: Final = "interleaved-cancel"
MULTI_TURN_RUN: Final = "multi-turn-run"
COMPACTION_IS_RUN_LEVEL: Final = "compaction-is-run-level"


def _negotiation_first() -> tuple[LifecycleSequence, ...]:
    """Every hook but the handshake, called before it (SPI-R-003)."""
    sequences = []
    for hook in _ordered(frozenset(Hook) - {Hook.NEGOTIATE}):
        draft = _Draft(
            NEGOTIATION_FIRST, f"{NEGOTIATION_FIRST}:{hook.value}", "SPI-R-003"
        )
        draft.refuse(
            hook, reason=Reason.PRE_NEGOTIATION, error_code=ERROR_CODE_INVALID_REQUEST
        )
        sequences.append(draft.done())
    # The handshake itself, so the family is not "the provider refuses everything".
    draft = _Draft(NEGOTIATION_FIRST, f"{NEGOTIATION_FIRST}:handshake", "SPI-R-003")
    draft.negotiate()
    draft.open_turn()
    sequences.append(draft.done())
    return tuple(sequences)


def _monotonic_sequence() -> tuple[LifecycleSequence, ...]:
    """A position already served, redelivered (SPI-R-005).

    A read-only redelivery is dropped and every other one is refused, which is
    the read-only partition doing the deciding rather than a per-hook rule.
    """
    sequences = []
    for hook in _ordered(TURN_SCOPED_HOOKS):
        draft = _Draft(
            MONOTONIC_SEQUENCE, f"{MONOTONIC_SEQUENCE}:{hook.value}", "SPI-R-005"
        )
        draft.negotiate()
        served = draft.open_turn().request.provenance.sequence
        if hook in READ_ONLY_HOOKS:
            draft.add(
                hook,
                disposition=Disposition.IGNORED,
                reason=Reason.STALE_SEQUENCE,
                sequence=served,
            )
        else:
            draft.refuse(
                hook,
                reason=Reason.STALE_SEQUENCE,
                error_code=ERROR_CODE_INVALID_REQUEST,
                sequence=served,
            )
        sequences.append(draft.done())
    return tuple(sequences)


def _turn_must_be_open() -> tuple[LifecycleSequence, ...]:
    """Recall opens a turn; every other turn-scoped hook needs one already open."""
    draft = _Draft(TURN_MUST_BE_OPEN, f"{TURN_MUST_BE_OPEN}:recall-opens", "SPI-R-005")
    draft.negotiate()
    draft.open_turn()
    draft.accept(Hook.CAPTURE_AFTER_TURN)
    sequences = [draft.done()]
    for hook in _ordered(TURN_SCOPED_HOOKS - {Hook.RECALL_BEFORE_TURN}):
        draft = _Draft(
            TURN_MUST_BE_OPEN, f"{TURN_MUST_BE_OPEN}:{hook.value}", "SPI-R-005"
        )
        draft.negotiate()
        draft.refuse(
            hook, reason=Reason.TURN_NOT_OPEN, error_code=ERROR_CODE_INVALID_REQUEST
        )
        sequences.append(draft.done())
    return tuple(sequences)


def _terminal_blocks_effect() -> tuple[LifecycleSequence, ...]:
    """Both terminals against every turn effect: `conflict`, every pair (SPI-R-006)."""
    sequences = []
    for terminal in _ordered(TERMINAL_HOOKS):
        for effect in _ordered(TURN_EFFECT_HOOKS):
            draft = _Draft(
                TERMINAL_BLOCKS_EFFECT,
                f"{TERMINAL_BLOCKS_EFFECT}:{terminal.value}:{effect.value}",
                "SPI-R-006",
            )
            draft.negotiate()
            draft.open_turn()
            draft.accept(terminal)
            draft.refuse(
                effect, reason=Reason.TURN_TERMINAL, error_code=ERROR_CODE_CONFLICT
            )
            sequences.append(draft.done())
    return tuple(sequences)


def _terminal_control_pair() -> tuple[LifecycleSequence, ...]:
    """Both terminals against both terminals.

    The same terminal repeated is a successful idempotent result carrying the
    unchanged state (SPI-R-032); the opposite one is a `conflict`, because a
    terminal turn is not re-decidable (SPI-R-006).
    """
    sequences = []
    for first in _ordered(TERMINAL_HOOKS):
        for second in _ordered(TERMINAL_HOOKS):
            draft = _Draft(
                TERMINAL_CONTROL_PAIR,
                f"{TERMINAL_CONTROL_PAIR}:{first.value}:{second.value}",
                "SPI-R-006",
            )
            draft.negotiate()
            draft.open_turn()
            draft.accept(first)
            if second is first:
                draft.add(
                    second,
                    disposition=Disposition.IDEMPOTENT_REPLAY,
                    reason=Reason.STATE_REPLAY,
                )
            else:
                draft.refuse(
                    second, reason=Reason.TURN_TERMINAL, error_code=ERROR_CODE_CONFLICT
                )
            sequences.append(draft.done())
    return tuple(sequences)


def _retry_reopens() -> tuple[LifecycleSequence, ...]:
    """`turn.retry` re-enters the identified turn's open state from either terminal."""
    sequences = []
    for terminal in _ordered(TERMINAL_HOOKS):
        draft = _Draft(RETRY_REOPENS, f"{RETRY_REOPENS}:{terminal.value}", "SPI-R-041")
        draft.negotiate()
        draft.open_turn()
        draft.accept(terminal)
        draft.accept(Hook.TURN_RETRY)
        draft.accept(Hook.CAPTURE_AFTER_TURN)
        sequences.append(draft.done())
    return tuple(sequences)


def _later_turn_opens() -> tuple[LifecycleSequence, ...]:
    """A turn terminal is not a run terminal: the next turn opens normally."""
    sequences = []
    for terminal in _ordered(TERMINAL_HOOKS):
        draft = _Draft(
            LATER_TURN_OPENS, f"{LATER_TURN_OPENS}:{terminal.value}", "SPI-R-006"
        )
        draft.negotiate()
        draft.open_turn()
        draft.accept(terminal)
        draft.open_turn(turn=_LATER_TURN)
        draft.accept(Hook.CAPTURE_AFTER_TURN, turn=_LATER_TURN)
        sequences.append(draft.done())
    return tuple(sequences)


def _isolation() -> tuple[LifecycleSequence, ...]:
    """Two turns in one run, and two runs, stay separate under either terminal."""
    sequences = []
    for terminal in _ordered(TERMINAL_HOOKS):
        turns = _Draft(
            TURN_ISOLATION, f"{TURN_ISOLATION}:{terminal.value}", "SPI-R-006"
        )
        turns.negotiate()
        turns.open_turn()
        turns.accept(terminal)
        turns.open_turn(turn=_LATER_TURN)
        turns.accept(Hook.CAPTURE_AFTER_TURN, turn=_LATER_TURN)
        turns.refuse(
            Hook.CAPTURE_AFTER_TURN,
            reason=Reason.TURN_TERMINAL,
            error_code=ERROR_CODE_CONFLICT,
        )
        sequences.append(turns.done())

        runs = _Draft(RUN_ISOLATION, f"{RUN_ISOLATION}:{terminal.value}", "SPI-R-038")
        runs.negotiate()
        runs.open_turn()
        runs.accept(terminal)
        runs.open_turn(run=_OTHER_RUN)
        runs.accept(Hook.CAPTURE_AFTER_TURN, run=_OTHER_RUN)
        runs.refuse(
            Hook.CAPTURE_AFTER_TURN,
            reason=Reason.TURN_TERMINAL,
            error_code=ERROR_CODE_CONFLICT,
        )
        sequences.append(runs.done())
    return tuple(sequences)


def _interleaved_cancel() -> tuple[LifecycleSequence, ...]:
    """A turn cancelled while a later turn of the same run is still open.

    Both turns are opened before either ends, so the cancellation has to land on
    the turn it names and on no other: the open turn takes the effect and the
    cancelled one refuses the same effect `conflict` (SPI-R-006). One sequence
    per turn effect, because a provider that fused the two turns would fail on
    whichever effect it fused them for.
    """
    sequences = []
    for effect in _ordered(TURN_EFFECT_HOOKS):
        draft = _Draft(
            INTERLEAVED_CANCEL, f"{INTERLEAVED_CANCEL}:{effect.value}", "SPI-R-006"
        )
        draft.negotiate()
        draft.open_turn()
        draft.open_turn(turn=_LATER_TURN)
        draft.accept(Hook.TURN_CANCEL)
        draft.accept(effect, turn=_LATER_TURN)
        draft.refuse(
            effect, reason=Reason.TURN_TERMINAL, error_code=ERROR_CODE_CONFLICT
        )
        sequences.append(draft.done())
    return tuple(sequences)


def _multi_turn_run() -> tuple[LifecycleSequence, ...]:
    """One run whose turns end one after another, by every control hook there is.

    The first turn completes, the next one cancels, and that cancelled turn is
    retried after a further turn has opened. This is the accepted control order,
    not wire-value order. Every terminal is followed by another turn of the
    *same* run opening, which makes "terminal for the identified turn only"
    observable rather than stated (SPI-R-006). The retried turn then takes an
    effect and completes, so `turn.retry` is shown inside the run rather than
    only in a sequence of its own (SPI-R-041).
    """
    draft = _Draft(MULTI_TURN_RUN, f"{MULTI_TURN_RUN}:every-control-hook", "SPI-R-006")
    draft.negotiate()
    turn = _TURN
    draft.open_turn(turn=turn)
    terminals = (Hook.TURN_COMPLETE, Hook.TURN_CANCEL)
    for terminal in terminals:
        draft.accept(terminal, turn=turn)
        turn += 1
        draft.open_turn(turn=turn)
    recovered_turn = turn - 1
    draft.accept(Hook.TURN_RETRY, turn=recovered_turn)
    draft.accept(Hook.CAPTURE_AFTER_TURN, turn=recovered_turn)
    draft.accept(Hook.TURN_COMPLETE, turn=recovered_turn)
    turn += 1
    draft.open_turn(turn=turn)
    draft.accept(Hook.CAPTURE_AFTER_TURN, turn=turn)
    return (draft.done(),)


def _compaction_is_run_level() -> tuple[LifecycleSequence, ...]:
    """`context.compact` is a run-level notice with no position in the turn machine.

    It is acknowledged and dropped, and the turn it interleaves with carries on
    as though it had not been delivered, because Core holds nothing from it.
    """
    notice = _Draft(
        COMPACTION_IS_RUN_LEVEL, f"{COMPACTION_IS_RUN_LEVEL}:notice", "SPI-R-005"
    )
    notice.negotiate()
    notice.add(
        Hook.CONTEXT_COMPACT,
        disposition=Disposition.IGNORED,
        reason=Reason.COMPACTION_NOTICE,
    )
    inside = _Draft(
        COMPACTION_IS_RUN_LEVEL, f"{COMPACTION_IS_RUN_LEVEL}:inside-a-turn", "SPI-R-005"
    )
    inside.negotiate()
    inside.open_turn()
    inside.add(
        Hook.CONTEXT_COMPACT,
        disposition=Disposition.IGNORED,
        reason=Reason.COMPACTION_NOTICE,
    )
    inside.accept(Hook.CAPTURE_AFTER_TURN)
    inside.accept(Hook.TURN_COMPLETE)
    return (notice.done(), inside.done())


def _generate() -> tuple[LifecycleSequence, ...]:
    return (
        *_negotiation_first(),
        *_monotonic_sequence(),
        *_turn_must_be_open(),
        *_terminal_blocks_effect(),
        *_terminal_control_pair(),
        *_retry_reopens(),
        *_later_turn_opens(),
        *_isolation(),
        *_interleaved_cancel(),
        *_multi_turn_run(),
        *_compaction_is_run_level(),
    )


#: Every generated sequence, in family order. Built once at import because the
#: values are immutable and the generation takes no input.
LIFECYCLE_SEQUENCES: Final[tuple[LifecycleSequence, ...]] = _generate()


# --- execution ---------------------------------------------------------------


def run_sequence(sequence: LifecycleSequence) -> SequenceResult:
    """Drive one sequence through a fresh mock provider, one call per step.

    The provider is built here rather than passed in: a sequence states its own
    preconditions as steps, so a provider carrying state from an earlier
    sequence would be running a different sequence than the one named.
    """
    if not isinstance(sequence, LifecycleSequence):
        raise LifecycleSequenceError("sequence must be a LifecycleSequence")
    provider = MockProvider()
    results = []
    for step in sequence.steps:
        results.append(StepResult(step=step, outcome=provider.handle(step.request)))
    return SequenceResult(
        sequence=sequence,
        steps=tuple(results),
        handled_calls=len(provider.journal),
    )


def run_lifecycle_sequences() -> tuple[SequenceResult, ...]:
    """Every generated sequence, each against its own provider, in order."""
    return tuple(run_sequence(sequence) for sequence in LIFECYCLE_SEQUENCES)


__all__ = [
    "COMPACTION_IS_RUN_LEVEL",
    "HOOK_PURPOSES",
    "INTERLEAVED_CANCEL",
    "LATER_TURN_OPENS",
    "LIFECYCLE_SEQUENCES",
    "MONOTONIC_SEQUENCE",
    "MULTI_TURN_RUN",
    "NEGOTIATION_FIRST",
    "RETRY_REOPENS",
    "RUN_ISOLATION",
    "TERMINAL_BLOCKS_EFFECT",
    "TERMINAL_CONTROL_PAIR",
    "TERMINAL_HOOKS",
    "TURN_EFFECT_HOOKS",
    "TURN_ISOLATION",
    "TURN_MUST_BE_OPEN",
    "LifecycleSequence",
    "LifecycleSequenceError",
    "LifecycleStep",
    "SequenceResult",
    "StepResult",
    "run_lifecycle_sequences",
    "run_sequence",
]
