"""V06-8 packet A9-P4: the generated lifecycle state-machine sequences.

These tests are about the *generator* as much as the provider. A layer that
listed its sequences by hand would pass a test that only ran them, so each
family is checked against the closed hook and state set it is generated from:
the count and the covered hooks or hook pairs are derived here from
`TURN_SCOPED_HOOKS`, `READ_ONLY_HOOKS`, `TERMINAL_HOOKS` and `TURN_EFFECT_HOOKS`
and compared, rather than restated as numbers.

The rest is what the accepted candidate's section 3.2 fixes and what this layer
must not become: one `handle` call per step, immutable and deterministic
results, no Core job control anywhere in any outcome, `context.compact`
composing and persisting and auditing nothing, and no corpus case identifier
anywhere in the source.
"""

from __future__ import annotations

import re
from dataclasses import FrozenInstanceError, replace
from itertools import product
from pathlib import Path

import pytest

from omnivia_core.agent_host import lifecycle_conformance
from omnivia_core.agent_host.lifecycle_conformance import (
    COMPACTION_IS_RUN_LEVEL,
    HOOK_PURPOSES,
    INTERLEAVED_CANCEL,
    LATER_TURN_OPENS,
    LIFECYCLE_SEQUENCES,
    MONOTONIC_SEQUENCE,
    MULTI_TURN_RUN,
    NEGOTIATION_FIRST,
    RETRY_REOPENS,
    RUN_ISOLATION,
    TERMINAL_BLOCKS_EFFECT,
    TERMINAL_CONTROL_PAIR,
    TERMINAL_HOOKS,
    TURN_EFFECT_HOOKS,
    TURN_ISOLATION,
    TURN_MUST_BE_OPEN,
    LifecycleSequence,
    LifecycleSequenceError,
    LifecycleStep,
    SequenceResult,
    StepResult,
    run_lifecycle_sequences,
    run_sequence,
)
from omnivia_core.agent_host.spi import (
    CORE_JOB_CONTROL_OPERATIONS,
    READ_ONLY_HOOKS,
    RUN_LEVEL_HOOKS,
    TURN_CONTROL_HOOKS,
    TURN_SCOPED_HOOKS,
    Disposition,
    Hook,
    Reason,
    SpiContractError,
    SpiProvenance,
    SpiRequest,
)
from omnivia_core.contracts.v1.generated import ERROR_CODE_CONFLICT

RESULTS = run_lifecycle_sequences()
STEP_RESULTS = tuple(step for result in RESULTS for step in result.steps)


def _family(name: str) -> tuple[LifecycleSequence, ...]:
    return tuple(item for item in LIFECYCLE_SEQUENCES if item.family == name)


# --- the sequences run clean -----------------------------------------------------


@pytest.mark.parametrize("result", RESULTS, ids=lambda item: item.sequence.name)
def test_every_generated_sequence_matches_the_lifecycle_rules(
    result: SequenceResult,
) -> None:
    assert result.passed, f"{result.sequence.name}: {result.detail}"


def test_every_sequence_is_named_once() -> None:
    names = [sequence.name for sequence in LIFECYCLE_SEQUENCES]
    assert len(set(names)) == len(names)


# --- the families are generated from the closed sets -----------------------------


def test_negotiation_precedes_every_other_hook() -> None:
    """One sequence per hook but the handshake, plus the handshake itself."""
    refused = {
        sequence.hooks[0]: sequence
        for sequence in _family(NEGOTIATION_FIRST)
        if len(sequence.hooks) == 1
    }
    assert set(refused) == set(Hook) - {Hook.NEGOTIATE}
    for sequence in refused.values():
        step = sequence.steps[0]
        assert step.disposition is Disposition.REFUSED
        assert step.reason is Reason.PRE_NEGOTIATION
    handshake = next(
        sequence for sequence in _family(NEGOTIATION_FIRST) if len(sequence.hooks) > 1
    )
    assert handshake.hooks[0] is Hook.NEGOTIATE


def test_the_monotonic_family_covers_every_turn_scoped_hook() -> None:
    """Read-only redeliveries are dropped, every other one refused. No third case."""
    observed = {
        sequence.hooks[-1]: sequence.steps[-1]
        for sequence in _family(MONOTONIC_SEQUENCE)
    }
    assert set(observed) == set(TURN_SCOPED_HOOKS)
    for hook, step in observed.items():
        expected = (
            Disposition.IGNORED if hook in READ_ONLY_HOOKS else Disposition.REFUSED
        )
        assert step.disposition is expected
        assert step.reason is Reason.STALE_SEQUENCE
    # The redelivered position is one the run has already served.
    for sequence in _family(MONOTONIC_SEQUENCE):
        assert (
            sequence.steps[-1].request.provenance.sequence
            == sequence.steps[-2].request.provenance.sequence
        )


def test_recall_opens_a_turn_and_every_other_turn_hook_needs_one_open() -> None:
    unopened = {
        sequence.hooks[-1]: sequence.steps[-1]
        for sequence in _family(TURN_MUST_BE_OPEN)
        if sequence.steps[-1].reason is Reason.TURN_NOT_OPEN
    }
    assert set(unopened) == set(TURN_SCOPED_HOOKS) - {Hook.RECALL_BEFORE_TURN}
    opener = next(
        sequence
        for sequence in _family(TURN_MUST_BE_OPEN)
        if sequence.steps[-1].disposition is Disposition.ACCEPTED
    )
    assert opener.hooks == (
        Hook.NEGOTIATE,
        Hook.RECALL_BEFORE_TURN,
        Hook.CAPTURE_AFTER_TURN,
    )


def test_both_terminals_block_every_turn_effect() -> None:
    """The full product of the two closed sets, and `conflict` on every pair."""
    pairs = {
        (sequence.hooks[-2], sequence.hooks[-1]): sequence.steps[-1]
        for sequence in _family(TERMINAL_BLOCKS_EFFECT)
    }
    assert set(pairs) == set(product(TERMINAL_HOOKS, TURN_EFFECT_HOOKS))
    for step in pairs.values():
        assert step.disposition is Disposition.REFUSED
        assert step.reason is Reason.TURN_TERMINAL
        assert step.error_code == ERROR_CODE_CONFLICT
    assert TURN_EFFECT_HOOKS == (
        TURN_SCOPED_HOOKS - TURN_CONTROL_HOOKS - {Hook.RECALL_BEFORE_TURN}
    )


def test_a_repeated_terminal_is_idempotent_and_the_opposite_one_conflicts() -> None:
    pairs = {
        (sequence.hooks[-2], sequence.hooks[-1]): sequence.steps[-1]
        for sequence in _family(TERMINAL_CONTROL_PAIR)
    }
    assert set(pairs) == set(product(TERMINAL_HOOKS, TERMINAL_HOOKS))
    for (first, second), step in pairs.items():
        if first is second:
            assert step.disposition is Disposition.IDEMPOTENT_REPLAY
            assert step.reason is Reason.STATE_REPLAY
            assert step.error_code is None
        else:
            assert step.disposition is Disposition.REFUSED
            assert step.error_code == ERROR_CODE_CONFLICT
    assert TERMINAL_HOOKS == TURN_CONTROL_HOOKS - {Hook.TURN_RETRY}


@pytest.mark.parametrize(
    "family", [RETRY_REOPENS, LATER_TURN_OPENS, TURN_ISOLATION, RUN_ISOLATION]
)
def test_the_terminal_driven_families_cover_both_terminal_states(family: str) -> None:
    sequences = _family(family)
    covered = {sequence.hooks[2] for sequence in sequences}
    assert covered == set(TERMINAL_HOOKS)
    assert len(sequences) == len(TERMINAL_HOOKS)


def test_retry_reopens_a_terminal_turn() -> None:
    for sequence in _family(RETRY_REOPENS):
        assert sequence.hooks[-2:] == (Hook.TURN_RETRY, Hook.CAPTURE_AFTER_TURN)
        assert all(
            step.disposition is Disposition.ACCEPTED for step in sequence.steps[-2:]
        )


def test_a_later_turn_opens_after_either_terminal() -> None:
    for sequence in _family(LATER_TURN_OPENS):
        ordinals = [step.request.turn_ordinal for step in sequence.steps]
        assert ordinals[-1] is not None
        assert ordinals[-1] > ordinals[2]
        assert sequence.steps[-1].disposition is Disposition.ACCEPTED


def test_two_turns_and_two_runs_stay_isolated() -> None:
    for sequence in _family(TURN_ISOLATION):
        # The later turn accepts while the terminal turn still refuses.
        assert sequence.steps[-2].disposition is Disposition.ACCEPTED
        assert sequence.steps[-1].disposition is Disposition.REFUSED
        assert (
            sequence.steps[-2].request.turn_ordinal
            != sequence.steps[-1].request.turn_ordinal
        )
    for sequence in _family(RUN_ISOLATION):
        runs = {step.request.provenance.run for step in sequence.steps}
        assert len(runs) == 2
        assert sequence.steps[-2].disposition is Disposition.ACCEPTED
        assert sequence.steps[-1].disposition is Disposition.REFUSED
        # The second run reopens the *same* turn ordinal the first one closed.
        assert (
            sequence.steps[-2].request.turn_ordinal
            == sequence.steps[-1].request.turn_ordinal
        )


def test_a_cancellation_lands_on_its_own_turn_while_a_later_one_is_open() -> None:
    """Both turns open before either ends, one sequence per turn effect."""
    covered = {}
    for sequence in _family(INTERLEAVED_CANCEL):
        # Two turns are opened, and only then is the first one cancelled.
        assert sequence.hooks[1:4] == (
            Hook.RECALL_BEFORE_TURN,
            Hook.RECALL_BEFORE_TURN,
            Hook.TURN_CANCEL,
        )
        assert (
            sequence.steps[1].request.turn_ordinal
            != sequence.steps[2].request.turn_ordinal
        )
        assert sequence.steps[3].request.turn_ordinal == (
            sequence.steps[1].request.turn_ordinal
        )
        # The same effect: accepted on the open turn, refused on the cancelled one.
        effect = sequence.hooks[-1]
        assert sequence.hooks[-2] is effect
        covered[effect] = sequence
        assert sequence.steps[-2].disposition is Disposition.ACCEPTED
        assert sequence.steps[-1].disposition is Disposition.REFUSED
        assert sequence.steps[-1].reason is Reason.TURN_TERMINAL
        assert sequence.steps[-1].error_code == ERROR_CODE_CONFLICT
        assert (
            sequence.steps[-2].request.turn_ordinal
            != sequence.steps[-1].request.turn_ordinal
        )
    assert set(covered) == set(TURN_EFFECT_HOOKS)


def test_one_run_ends_turn_after_turn_and_opens_a_further_one_each_time() -> None:
    """Every control hook, in one run, and a later turn after every terminal."""
    (sequence,) = _family(MULTI_TURN_RUN)
    assert {step.request.provenance.run for step in sequence.steps} == {
        sequence.steps[0].request.provenance.run
    }
    assert set(sequence.hooks) >= TURN_CONTROL_HOOKS
    controls = tuple(hook for hook in sequence.hooks if hook in TURN_CONTROL_HOOKS)
    assert controls[:3] == (
        Hook.TURN_COMPLETE,
        Hook.TURN_CANCEL,
        Hook.TURN_RETRY,
    )
    assert all(step.disposition is not Disposition.REFUSED for step in sequence.steps)
    for index, step in enumerate(sequence.steps):
        if step.request.hook not in TERMINAL_HOOKS:
            continue
        ended = step.request.turn_ordinal
        assert ended is not None
        opened = [
            later
            for later in sequence.steps[index + 1 :]
            if later.request.hook is Hook.RECALL_BEFORE_TURN
            and (later.request.turn_ordinal or 0) > ended
        ]
        assert opened, f"no further turn opens after {step.label}"
        assert opened[0].disposition is Disposition.ACCEPTED
    # The retried turn is the one that was just ended, and it takes an effect again.
    retry = next(
        index
        for index, step in enumerate(sequence.steps)
        if step.request.hook is Hook.TURN_RETRY
    )
    assert sequence.steps[retry - 1].request.hook is Hook.RECALL_BEFORE_TURN
    assert sequence.steps[retry].request.turn_ordinal == (
        sequence.steps[retry - 2].request.turn_ordinal
    )
    reopened = sequence.steps[retry + 1]
    assert reopened.request.hook is Hook.CAPTURE_AFTER_TURN
    assert reopened.request.turn_ordinal == sequence.steps[retry].request.turn_ordinal
    assert reopened.disposition is Disposition.ACCEPTED


def test_every_hook_appears_in_some_generated_sequence() -> None:
    covered = {hook for sequence in LIFECYCLE_SEQUENCES for hook in sequence.hooks}
    assert covered == set(Hook)


# --- the turn partition ----------------------------------------------------------


def _provenance(*, turn_ordinal: int | None) -> SpiProvenance:
    return SpiProvenance(
        agent="agent-partition",
        session="session-partition",
        run="run-partition",
        sequence=1,
        turn_ordinal=turn_ordinal,
    )


@pytest.mark.parametrize("hook", sorted(TURN_SCOPED_HOOKS, key=lambda item: item.value))
def test_every_turn_scoped_hook_requires_a_turn(hook: Hook) -> None:
    with pytest.raises(SpiContractError):
        SpiRequest(
            hook=hook,
            caller="principal-partition",
            workspace="workspace-partition",
            purpose=HOOK_PURPOSES[hook],
            provenance=_provenance(turn_ordinal=None),
            deadline_ms=30_000,
            idempotency_key="idem-partition",
        )


@pytest.mark.parametrize("hook", sorted(RUN_LEVEL_HOOKS, key=lambda item: item.value))
def test_every_run_level_hook_forbids_a_turn(hook: Hook) -> None:
    with pytest.raises(SpiContractError):
        SpiRequest(
            hook=hook,
            caller="principal-partition",
            workspace="workspace-partition",
            purpose=HOOK_PURPOSES[hook],
            provenance=_provenance(turn_ordinal=0),
            deadline_ms=30_000,
        )


def test_the_purposes_cover_the_closed_hook_set() -> None:
    assert set(HOOK_PURPOSES) == set(Hook)


# --- what the executor does, and does not, do ------------------------------------


@pytest.mark.parametrize("result", RESULTS, ids=lambda item: item.sequence.name)
def test_each_step_calls_handle_exactly_once(result: SequenceResult) -> None:
    """`handled_calls` is the provider's own journal length, not a count kept here."""
    assert result.handled_calls == len(result.sequence.steps)
    assert len(result.steps) == len(result.sequence.steps)


def test_a_result_that_lost_or_gained_a_call_does_not_pass() -> None:
    """Matching every step is not enough: the run must be one call per step."""
    result = RESULTS[0]
    assert result.passed
    for handled in (result.handled_calls - 1, result.handled_calls + 1):
        miscounted = replace(result, handled_calls=handled)
        assert all(step.passed for step in miscounted.steps)
        assert not miscounted.passed
        assert "handled_calls" in miscounted.detail


def test_results_are_deterministic() -> None:
    for sequence in LIFECYCLE_SEQUENCES:
        assert run_sequence(sequence) == run_sequence(sequence)


def test_results_are_immutable() -> None:
    result = RESULTS[0]
    with pytest.raises(FrozenInstanceError):
        result.handled_calls = 0  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.steps[0].outcome = result.steps[0].outcome  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        LIFECYCLE_SEQUENCES[0].steps[0].disposition = Disposition.ACCEPTED  # type: ignore[misc]


def test_a_wrong_expectation_is_reported_rather_than_passed() -> None:
    """The comparison is not vacuous: mutate a step and the sequence must fail."""
    sequence = _family(RETRY_REOPENS)[0]
    mutated = replace(
        sequence,
        steps=(
            *sequence.steps[:-1],
            replace(
                sequence.steps[-1],
                disposition=Disposition.REFUSED,
                reason=Reason.TURN_TERMINAL,
                error_code=ERROR_CODE_CONFLICT,
            ),
        ),
    )
    result = run_sequence(mutated)
    assert not result.passed
    assert "disposition" in result.detail
    assert "reason" in result.detail
    assert "error_code" in result.detail


def test_no_outcome_anywhere_reaches_core_job_control() -> None:
    for step in STEP_RESULTS:
        composed = set(step.outcome.composed_operations)
        assert not composed & CORE_JOB_CONTROL_OPERATIONS
        assert not step.outcome.implies_core_job_cancel
        assert not step.outcome.implies_core_job_retry


def test_no_outcome_anywhere_leaks_host_identity_or_widens_capability() -> None:
    for step in STEP_RESULTS:
        assert not step.outcome.host_identity_in_request_metadata
        assert not step.outcome.capability_expanded


def test_compaction_composes_persists_and_audits_nothing() -> None:
    compactions = [
        step
        for result in RESULTS
        if result.sequence.family == COMPACTION_IS_RUN_LEVEL
        for step in result.steps
        if step.outcome.hook is Hook.CONTEXT_COMPACT
    ]
    assert compactions
    for step in compactions:
        assert step.outcome.disposition is Disposition.IGNORED
        assert step.outcome.reason is Reason.COMPACTION_NOTICE
        assert step.outcome.composed_operations == ()
        assert step.outcome.nested_envelopes == ()
        assert step.outcome.audit_record_required is False
        assert step.outcome.response.metadata.audit_reference is None
        assert step.step.request.turn_ordinal is None


def test_the_malformed_is_refused_rather_than_reported() -> None:
    with pytest.raises(LifecycleSequenceError):
        run_sequence("negotiation-first:handshake")  # type: ignore[arg-type]
    with pytest.raises(LifecycleSequenceError):
        LifecycleSequence(family="f", name="n", rule="r", steps=())
    step = LIFECYCLE_SEQUENCES[0].steps[0]
    with pytest.raises(LifecycleSequenceError):
        # Only a refusal carries an error code.
        LifecycleStep(
            request=step.request,
            disposition=Disposition.ACCEPTED,
            reason=Reason.ACCEPTED,
            error_code=ERROR_CODE_CONFLICT,
        )
    with pytest.raises(LifecycleSequenceError):
        StepResult(step=step, outcome=None)  # type: ignore[arg-type]


# --- the architectural properties, asserted against the source -------------------

_SOURCE = Path(lifecycle_conformance.__file__).read_text(encoding="utf-8")


def test_the_layer_names_no_corpus_case_identifier() -> None:
    """No vector identifier, and no corpus answer to copy one from."""
    assert not re.search(r"SPI-V-\d", _SOURCE)
    for forbidden in (
        "conformance_runner",
        "vector_inputs",
        "load_corpus",
        "ConformanceCase",
        "ExpectedOutcome",
        "prepare_vector",
    ):
        assert forbidden not in _SOURCE


def test_the_layer_reaches_no_host_storage_or_network() -> None:
    for forbidden in (
        "import socket",
        "import urllib",
        "import sqlite3",
        "import pathlib",
        ".read_text",
        ".write_text",
    ):
        assert forbidden not in _SOURCE
