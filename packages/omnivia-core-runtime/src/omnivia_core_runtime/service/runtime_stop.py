"""Stopping a run, and stopping admission for a whole workspace (RT-207).

Three ways a run stops without finishing its work -- an operator cancels it, its deadline
passes, a newer run supersedes it -- and one way a workspace stops taking new work at all.
All four are commands here, and none of them is an outcome. The central invariant is the
one RT-205 states for effects, one level up: **stopping never fabricates a result.** A
cancelled run is `cancelled`, never `succeeded`; a timed-out run is `failed`, never
`cancelled`; a run holding an effect nobody can account for is `uncertain` and stays that
way until the effect is reconciled. There is no branch here that closes a run over work
this database cannot account for, and no argument by which a caller could ask for one.

**A stop is two steps, and they are two steps on purpose.**
:func:`request_run_stop` records *what was asked and why* -- durably, once, keyed on the
run. :func:`settle_run_stop` derives *what the run therefore becomes* from that command
plus the run's own open work, and records it as one more entry on the append-only event
stream 0018 owns. Splitting them is what makes the second step deterministic: it takes no
outcome from its caller, reads everything it decides from the database, and can be
repeated after a crash without changing an answer. It is also what lets a run be stopped
while work is still in flight and terminalize later, when that work has actually settled,
rather than being declared finished over the top of it.

**Terminal transitions are monotonic, and contradictions fail closed.** Nothing here owns
a second copy of the status machine. The terminal statuses are sinks in
`RUN_STATUS_TRANSITIONS`, 0018's event guard refuses any entry after a terminal one, and
:func:`decide_stop_settlement` asks the contract's own
:func:`~omnivia_core.contracts.v1.validate_run_status_transition` whether the move it
derived is legal. A timeout of a run that never started is refused rather than quietly
recorded as a cancellation -- `admitted` may only become `running` or `cancelled`, a run
that never ran cannot have failed, and rewriting the operator's reason to make the move fit
would be this seam answering a question nobody asked it.

**Evidence is preserved, never collapsed.** Stopping deletes nothing and rewrites nothing:
every prior event, artifact, evidence item, cleanup receipt, approval, grant, intent,
receipt, settlement and reconciliation stays exactly as written, and the stop is one more
fact on top. That is why the terminal status is appended to the stream rather than stamped
onto a column -- 0018 makes the stream append-only, so a stopped run's history is
structurally unrewritable rather than merely left alone by this module.

**Unknown effects are never auto-retried, and never assumed away.** An effect settled
`unknown` and not yet reconciled puts the run in `uncertain` -- the one non-terminal status
a stopped run can rest in, which the contract added for exactly this. Nothing here
dispatches, redispatches or schedules; a stop does not produce work. An effect with no
settlement at all is refused rather than guessed at, because settling it is RT-205's job
and stopping a run is not a licence to decide what became of its effects.

**The running-work policy is explicit.** `await` means the attempts and waits already open
settle on their own and the run terminalizes only once they have -- a settle attempted
before then is refused, with nothing written. `release` means this stop closes them:
pending waits `cancelled`, running attempts `cancelled`, unfinished steps `cancelled`,
through the same append-only relations any other closure uses. There is no default,
because "and what about the work in flight?" is the question an operator has to answer.

**The emergency admission stop is structural, not advisory.** 0025 puts the denial on
`omnivia_runtime_runs` and `omnivia_runtime_effect_intents` themselves, so while a stop is
engaged no run is admitted and no effect is intended in this database by any writer at all.
Everything that lets already-running work *reach an outcome* is deliberately still open --
a dispatch of an intent already declared, a receipt, a settlement, a reconciliation, a wait
resolution, an attempt outcome, a run event -- because an emergency stop that also froze
the work in flight would leave every uncertain effect uncertain forever.
:func:`require_admission_open` is the readable refusal in front of that guard, not a second
authority: the guard is what makes the rule true.

**Stale owners cannot mutate.** Every durable step here runs inside
:func:`~storage.agent_runtime.runtime_writer`, so a superseded fencing generation or a
foreign service instance is refused on entry and again immediately before commit, and a
rollback leaves the run exactly as it was.

This module adds no adapter, no transport, no dispatcher, no scheduler and no public wire
operation, for the reasons :mod:`.effect_transaction` and :mod:`.effect_reconciliation` add
none. The frozen application catalogue is untouched; there is no `run.cancel`.
"""

from __future__ import annotations

import sqlite3
from typing import Final

from omnivia_core.contracts.v1 import (
    ATTEMPT_STATUS_CANCELLED,
    ATTEMPT_STATUS_RUNNING,
    EFFECT_OUTCOME_UNKNOWN,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_FAILED,
    RUN_STATUS_UNCERTAIN,
    RUN_STEP_TERMINAL_STATUSES,
    RUN_TERMINAL_STATUSES,
    WAIT_RESOLUTION_CANCELLED,
    WAIT_STATUS_PENDING,
    ContractSemanticError,
    is_known_run_status,
    validate_run_status_transition,
)
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.storage.agent_runtime import (
    AdmissionStop,
    RunSnapshot,
    RunStop,
    read_admission_stop,
    read_run,
    read_run_stop,
    runtime_timestamp,
    runtime_writer,
)
from omnivia_core_runtime.storage.connection import StorageError

#: Why a run is being stopped. Three private reasons rather than a widened `RunStatus`:
#: the status a run ends in is the accepted contract's and is not extended here, and these
#: say which of the three the operator asked for. `timed_out` and `superseded` both need
#: to be distinguishable from a plain cancellation afterwards -- a deadline that passed and
#: a newer run taking over are different facts about why work stopped, and collapsing them
#: into `cancelled` would lose the difference the moment it is written down.
STOP_REASON_CANCELLED: Final = "cancelled"
STOP_REASON_TIMED_OUT: Final = "timed_out"
STOP_REASON_SUPERSEDED: Final = "superseded"
STOP_REASONS: Final[tuple[str, ...]] = (
    STOP_REASON_CANCELLED,
    STOP_REASON_TIMED_OUT,
    STOP_REASON_SUPERSEDED,
)

#: What is to happen to attempts and waits that are already open. No default: an implicit
#: answer to "and the work in flight?" is exactly the answer an operator has to give.
RUNNING_WORK_AWAIT: Final = "await"
RUNNING_WORK_RELEASE: Final = "release"
RUNNING_WORK_POLICIES: Final[tuple[str, ...]] = (
    RUNNING_WORK_AWAIT,
    RUNNING_WORK_RELEASE,
)

ADMISSION_STOP_ENGAGED: Final = "engaged"
ADMISSION_STOP_RELEASED: Final = "released"

#: The terminal `RunStatus` each stop reason settles a run in. Fixed here rather than
#: chosen at the call site, for RT-205's reason: a stopped run's status states which stop
#: it was given and a caller cannot state a different one. A timeout is `failed` because a
#: deadline that passed is a run that did not do what it was asked; a supersession is
#: `cancelled` because the superseded run's own work was called off, whatever its successor
#: goes on to do.
_TERMINAL_STATUS_FOR_REASON: Final[dict[str, str]] = {
    STOP_REASON_CANCELLED: RUN_STATUS_CANCELLED,
    STOP_REASON_TIMED_OUT: RUN_STATUS_FAILED,
    STOP_REASON_SUPERSEDED: RUN_STATUS_CANCELLED,
}

#: The reason code each settled stop is recorded under, in the same open, dot-namespaced
#: `OpenCode` shape RT-205's and RT-206's reasons use. One per branch of
#: :func:`decide_stop_settlement`, so a stopped run's event states which branch produced it.
REASON_STOPPED_CANCELLED: Final = "run.stopped_cancelled"
REASON_STOPPED_TIMED_OUT: Final = "run.stopped_timed_out"
REASON_STOPPED_SUPERSEDED: Final = "run.stopped_superseded"
REASON_STOP_DEFERRED_UNCERTAIN: Final = "run.stop_deferred_uncertain_effect"

_REASON_FOR_STOP: Final[dict[str, str]] = {
    STOP_REASON_CANCELLED: REASON_STOPPED_CANCELLED,
    STOP_REASON_TIMED_OUT: REASON_STOPPED_TIMED_OUT,
    STOP_REASON_SUPERSEDED: REASON_STOPPED_SUPERSEDED,
}

#: The one event kind a settled stop appends. One kind rather than two, because the entry's
#: `run_status` column already says whether the run closed or came to rest `uncertain`, and
#: a second kind saying the same thing could disagree with it.
EVENT_KIND_RUN_STOPPED: Final = "run_stopped"

_STEP_STATUS_CANCELLED: Final = "cancelled"


class RunStopError(StorageError):
    """One refusal from this seam, in the storage vocabulary the repository already uses.

    A `StorageError` subclass for :class:`~.effect_transaction.EffectTransactionError`'s
    reason: every caller of the runtime repository already handles that type, and a refusal
    here means the same thing those do -- the durable record does not permit what was
    asked, and nothing was written.
    """


def decide_stop_settlement(
    *,
    stop: RunStop,
    run_status: str,
    open_waits: int,
    running_attempts: int,
    unsettled_effects: int,
    uncertain_effects: int,
) -> tuple[str, str]:
    """The status and reason one stopped run settles in. Pure, and fail-closed.

    The same shape and the same discipline as
    :func:`~.effect_transaction.decide_settlement` and
    :func:`~.effect_reconciliation.decide_reconciliation`: no database, no clock, no
    adapter, no default branch, and a status derived from the record rather than supplied.

    The refusals come first, because each of them is a state in which *no* honest answer
    exists yet:

    * an unrecognized or already-terminal status. A finished run has nothing left to stop,
      and a status this build cannot read implies neither terminality nor any successor;
    * an effect this run declared that holds no settlement. Stopping settles no effect and
      may not assume one did not land -- what that effect is owed is a settlement, which is
      :mod:`.effect_transaction`'s to give;
    * an `await` stop with an attempt still running or a wait still pending. That policy
      says the work in flight settles on its own, so terminalizing over the top of it would
      be exactly the fabricated outcome this seam exists to prevent. The refusal is not a
      failure: the same command settles cleanly once the work has closed.

    Then the two answers:

    * an effect settled `unknown` and not yet reconciled makes the run `uncertain`. Not
      terminal, not retried, and not a stall -- it is the status the contract added for a
      run holding a question nobody can yet answer, and reconciling the effect is what
      clears it;
    * otherwise the terminal status this stop's reason settles in.

    Whether the run may actually move there is asked of the contract's own transition rule
    rather than restated here, so a timeout of a run that never started is refused instead
    of being rewritten into a cancellation that happens to be legal.
    """
    if min(open_waits, running_attempts, unsettled_effects, uncertain_effects) < 0:
        raise RunStopError(
            "a count of open work is never negative; a run cannot be stopped from a "
            "count that is not one"
        )
    if stop.stop_reason not in _TERMINAL_STATUS_FOR_REASON:
        raise RunStopError(
            f"stop reason {stop.stop_reason!r} is not one this build settles a run on"
        )
    if not is_known_run_status(run_status):
        raise RunStopError(
            f"run {stop.run_id!r} is in status {run_status!r}, which this build does not "
            "recognize; an unreadable status is never stopped into a readable one"
        )
    if run_status in RUN_TERMINAL_STATUSES:
        raise RunStopError(
            f"run {stop.run_id!r} is already {run_status!r} and has finished; a terminal "
            "run has nothing left to stop"
        )
    if unsettled_effects:
        raise RunStopError(
            f"run {stop.run_id!r} declared {unsettled_effects} effect(s) holding no "
            "settlement; stopping settles no effect and never assumes one did not land"
        )
    if stop.running_work == RUNNING_WORK_AWAIT and (open_waits or running_attempts):
        raise RunStopError(
            f"run {stop.run_id!r} holds {running_attempts} running attempt(s) and "
            f"{open_waits} pending wait(s), and its stop awaits them; a run is never "
            "terminalized over work that is still open"
        )
    if stop.running_work not in RUNNING_WORK_POLICIES:
        raise RunStopError(
            f"running-work policy {stop.running_work!r} is not one this build stops a "
            "run under"
        )
    if uncertain_effects:
        settled = RUN_STATUS_UNCERTAIN, REASON_STOP_DEFERRED_UNCERTAIN
    else:
        settled = (
            _TERMINAL_STATUS_FOR_REASON[stop.stop_reason],
            _REASON_FOR_STOP[stop.stop_reason],
        )
    try:
        validate_run_status_transition(run_status, settled[0])
    except ContractSemanticError as error:
        raise RunStopError(
            f"run {stop.run_id!r} is {run_status!r} and a {stop.stop_reason!r} stop "
            f"settles it {settled[0]!r}, which it may not move to: {error}"
        ) from error
    return settled


def request_run_stop(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    run_id: str,
    run_stop_id: str,
    stop_reason: str,
    running_work: str,
    requested_at_us: int,
    audit_ref: str,
    superseded_by_run_id: str | None = None,
) -> RunStop:
    """Record the one stop command a run is given, exactly once.

    Durable before anything acts on it, and idempotent rather than repeatable. A stop
    already stored for this run is compared field by field against the one this call would
    write: an identical request is answered from the store with nothing written -- which is
    what a caller replaying its own command after a crash issues -- and a request differing
    in any field is refused, because 0025 keys the row on the run and a second, *different*
    stop of one run has nowhere to live. A caller asking for one is asking this seam to
    change a command rather than repeat it.

    Recording the command settles nothing. What the run becomes is
    :func:`settle_run_stop`'s answer, derived from this row and the run's own open work.
    """
    with runtime_writer(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as writer:
        if read_run(connection, workspace_id=workspace_id, run_id=run_id) is None:
            raise RunStopError(
                f"run {run_id!r} is not admitted in this workspace; there is no stop "
                "command without a run to give it to"
            )
        stop = RunStop(
            workspace_id=workspace_id,
            run_stop_id=run_stop_id,
            run_id=run_id,
            stop_reason=stop_reason,
            running_work=running_work,
            requested_at=runtime_timestamp(requested_at_us),
            audit_reference=audit_ref,
            superseded_by_run_id=superseded_by_run_id,
        )
        stored = read_run_stop(connection, workspace_id=workspace_id, run_id=run_id)
        if stored is not None:
            if stored != stop:
                raise RunStopError(
                    f"run {run_id!r} is already stopped {stored.stop_reason!r}; a second, "
                    "different stop of one run is a contradiction"
                )
            return stored
        writer.request_run_stop(stop)
        return stop


def settle_run_stop(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    run_id: str,
    runtime_event_id: str,
    settled_at_us: int,
) -> str:
    """Settle one stopped run into the status its command and its open work imply.

    The composition :func:`decide_stop_settlement` is the decision half of. The run's
    snapshot, its stop command, its open waits, its running attempts and every effect it
    declared are read inside one fenced transaction, the pure rule answers from those, the
    release (if the command asked for one) and the run event are written in that same
    transaction, and the settled status is returned. So nothing can start, resolve or settle
    between the reading and the answering, and a rollback leaves the run exactly as it was
    -- stopped, and not yet settled.

    Repeating it is idempotent rather than a second decision. A run already resting in the
    status this stop settles it in is answered from the stream with nothing written, which
    is what a caller replaying its own command after a crash issues. A run resting in a
    *different* terminal status is refused: 0018 makes every terminal status a sink, so
    there is nowhere to write a second ending and asking for one is asking this seam to
    reopen a closed run.

    An `uncertain` run may be settled again, and that is the point of `uncertain`: the first
    call parked the run there because an effect could not be accounted for, and the second
    -- after :func:`~.effect_reconciliation.reconcile_effect_transaction` closed it -- moves
    the run to its terminal status. Nothing is retried in between; a reconciliation is
    evidence arriving, not work being repeated.

    The status is never an argument, for RT-205's reason: a caller that could state it could
    state `succeeded` for a run that was cancelled, which is the one thing this seam exists
    to make impossible.
    """
    with runtime_writer(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as writer:
        run = read_run(connection, workspace_id=workspace_id, run_id=run_id)
        if run is None:
            raise RunStopError(
                f"run {run_id!r} is not admitted in this workspace; there is no stop "
                "settlement without a run"
            )
        if run.stop is None:
            raise RunStopError(
                f"run {run_id!r} was given no stop command; a run is terminalized by a "
                "stop it was actually given, never by one inferred here"
            )
        open_waits = tuple(
            wait for wait in run.waits if wait.status == WAIT_STATUS_PENDING
        )
        running = tuple(
            attempt
            for step in run.steps
            for attempt in step.attempts
            if attempt.status == ATTEMPT_STATUS_RUNNING
        )
        settled, uncertain = _effect_account(run)
        replayed = _replayed_status(run, stop=run.stop)
        if replayed is not None:
            return replayed
        status, reason = decide_stop_settlement(
            stop=run.stop,
            run_status=run.status,
            open_waits=len(open_waits),
            running_attempts=len(running),
            unsettled_effects=settled,
            uncertain_effects=uncertain,
        )
        if status == run.status:
            # Both are `uncertain`: `decide_stop_settlement` returns nothing else a
            # non-terminal run could already be in, and a terminal one never reaches
            # here. The run is already resting where this stop leaves it, so a second
            # call while the effect is still uncertain restates a fact rather than
            # settling anything, and writes nothing.
            return status
        if run.stop.running_work == RUNNING_WORK_RELEASE:
            for wait in open_waits:
                writer.close_wait(
                    wait_id=wait.wait_id,
                    status=WAIT_RESOLUTION_CANCELLED,
                    resolved_at_us=settled_at_us,
                    resolution_reason=WAIT_RESOLUTION_CANCELLED,
                )
            for attempt in running:
                writer.finish_attempt(
                    attempt_id=attempt.attempt_id,
                    status=ATTEMPT_STATUS_CANCELLED,
                    finished_at_us=settled_at_us,
                )
            for step in run.steps:
                if step.status not in RUN_STEP_TERMINAL_STATUSES:
                    writer.record_step_status(
                        run_step_id=step.run_step_id,
                        status=_STEP_STATUS_CANCELLED,
                        observed_at_us=settled_at_us,
                    )
        writer.append_run_event(
            run_id=run_id,
            runtime_event_id=runtime_event_id,
            occurred_at_us=settled_at_us,
            event_kind=EVENT_KIND_RUN_STOPPED,
            run_status=status,
            details={
                "run_stop_id": run.stop.run_stop_id,
                "stop_reason": run.stop.stop_reason,
                "running_work": run.stop.running_work,
                "reason": reason,
            },
        )
        return status


def _effect_account(run: RunSnapshot) -> tuple[int, int]:
    """How many of this run's effects are unsettled, and how many are still uncertain.

    Counted from the three relations rather than asked of a column, because an effect's
    state *is* the relation it appears in: an intent with no settlement is unsettled, and
    one settled `unknown` is uncertain until a reconciliation names its settlement. A
    reconciled effect is neither -- the `unknown` row stays exactly as written and the
    reconciliation beside it is the final answer, which is the whole shape RT-206 chose.
    """
    settlements = {
        settlement.effect_intent_id: settlement for settlement in run.effect_settlements
    }
    reconciled = {
        reconciliation.effect_intent_id
        for reconciliation in run.effect_reconciliations
    }
    unsettled = 0
    uncertain = 0
    for intent in run.effect_intents:
        settlement = settlements.get(intent.effect_intent_id)
        if settlement is None:
            unsettled += 1
        elif (
            settlement.outcome == EFFECT_OUTCOME_UNKNOWN
            and intent.effect_intent_id not in reconciled
        ):
            uncertain += 1
    return unsettled, uncertain


def _replayed_status(run: RunSnapshot, *, stop: RunStop) -> str | None:
    """The status a repeated settlement is answered from, or `None` to settle now.

    Only a run already at rest is a replay. A terminal run that matches what this stop
    settles it in is one; a terminal run that does not is a contradiction and is refused
    here rather than left to 0018's sink rule, so the caller is told which two endings
    disagreed. An `uncertain` run is deliberately *not* a replay: it is the state a first
    settlement parks a run in, and settling it again after a reconciliation is the second
    half of the same stop rather than a repeat of the first.
    """
    if run.status not in RUN_TERMINAL_STATUSES:
        return None
    expected = _TERMINAL_STATUS_FOR_REASON.get(stop.stop_reason)
    if run.status == expected:
        return run.status
    raise RunStopError(
        f"run {run.run_id!r} has already finished {run.status!r} and its "
        f"{stop.stop_reason!r} stop settles {expected!r}; a run has one ending"
    )


def engage_admission_stop(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    admission_stop_id: str,
    running_work: str,
    effective_at_us: int,
    reason: str,
    audit_ref: str,
) -> AdmissionStop:
    """Engage this workspace's emergency admission stop, exactly once.

    From the instant this commits, 0025's guards refuse every new run admission and every
    new effect intent in this database, whatever code path they arrive through. Nothing
    that lets already-running work reach an outcome is blocked, because work in flight has
    to be able to finish: a stop that also froze it would leave every uncertain effect
    uncertain forever.

    `running_work` is the policy the whole workspace stops under, and it is a rule rather
    than a note -- 0025 refuses a per-run stop that disagrees with it, so an operator who
    declared that running work settles cannot be overruled one run at a time.

    Idempotent by identifier. Re-engaging under the same `admission_stop_id` with the same
    facts is answered from the ledger with nothing written; a stop already engaged under a
    *different* identifier is refused, because two live emergency stops would be two
    authorities over one workspace.
    """
    return _append_admission_stop(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
        admission_stop_id=admission_stop_id,
        state=ADMISSION_STOP_ENGAGED,
        running_work=running_work,
        effective_at_us=effective_at_us,
        reason=reason,
        audit_ref=audit_ref,
    )


def release_admission_stop(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    admission_stop_id: str,
    effective_at_us: int,
    reason: str,
    audit_ref: str,
) -> AdmissionStop:
    """Release this workspace's emergency admission stop, exactly once.

    A second immutable ledger entry, never an edit of the first: the fact that admission
    was once stopped, when, and why is part of the record, and lifting the stop does not
    unmake it. A release carries no running-work policy, because reopening admission
    declares nothing about work.

    Releasing a workspace that holds no engaged stop is refused rather than treated as a
    no-op -- a caller releasing a stop that is not engaged has lost track of which
    workspace it is talking to. Repeating a release under the same identifier is answered
    from the ledger.
    """
    return _append_admission_stop(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
        admission_stop_id=admission_stop_id,
        state=ADMISSION_STOP_RELEASED,
        running_work=None,
        effective_at_us=effective_at_us,
        reason=reason,
        audit_ref=audit_ref,
    )


def _append_admission_stop(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    admission_stop_id: str,
    state: str,
    running_work: str | None,
    effective_at_us: int,
    reason: str,
    audit_ref: str,
) -> AdmissionStop:
    """One ledger entry, and the replay and alternation rules both entries share."""
    with runtime_writer(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as writer:
        current = read_admission_stop(connection, workspace_id=workspace_id)
        if current is not None and current.admission_stop_id == admission_stop_id:
            entry = AdmissionStop(
                workspace_id=workspace_id,
                sequence=current.sequence,
                admission_stop_id=admission_stop_id,
                state=state,
                running_work=running_work,
                effective_at=runtime_timestamp(effective_at_us),
                reason=reason,
                audit_reference=audit_ref,
            )
            if current != entry:
                raise RunStopError(
                    f"admission stop {admission_stop_id!r} is already recorded "
                    f"{current.state!r}; a second, different entry under one identifier "
                    "is a contradiction"
                )
            return current
        engaged = admission_stopped(current)
        if state == ADMISSION_STOP_ENGAGED and engaged:
            assert current is not None  # `admission_stopped` refuses `None`.
            raise RunStopError(
                f"admission is already stopped by {current.admission_stop_id!r}; two live "
                "emergency stops would be two authorities over one workspace"
            )
        if state == ADMISSION_STOP_RELEASED and not engaged:
            raise RunStopError(
                "admission is not stopped in this workspace; there is nothing to release"
            )
        entry = AdmissionStop(
            workspace_id=workspace_id,
            sequence=0,
            admission_stop_id=admission_stop_id,
            state=state,
            running_work=running_work,
            effective_at=runtime_timestamp(effective_at_us),
            reason=reason,
            audit_reference=audit_ref,
        )
        return AdmissionStop(
            workspace_id=entry.workspace_id,
            sequence=writer.record_admission_stop(entry),
            admission_stop_id=entry.admission_stop_id,
            state=entry.state,
            running_work=entry.running_work,
            effective_at=entry.effective_at,
            reason=entry.reason,
            audit_reference=entry.audit_reference,
        )


def admission_stopped(stop: AdmissionStop | None) -> bool:
    """Whether `stop` denies new admission. Pure, and fail-safe on an open vocabulary.

    Only an entry this build reads as `engaged` denies anything. A released entry does not,
    an absent ledger does not, and a state outside the vocabulary does not either -- for
    the reason the contract's own open-vocabulary readers answer "no": a value this build
    cannot reason about grants nothing and blocks nothing, and the durable guard is what
    actually enforces the stop.
    """
    return stop is not None and stop.state == ADMISSION_STOP_ENGAGED


def require_admission_open(
    connection: sqlite3.Connection, *, workspace_id: str
) -> None:
    """Raise :class:`RunStopError` while this workspace's admission stop is engaged.

    The readable refusal in front of 0025's guards, and deliberately not a second
    authority: the guards on `omnivia_runtime_runs` and `omnivia_runtime_effect_intents`
    are what make the denial true, and this only lets a caller say so before the insert
    aborts. A caller that skips it is refused by the database anyway.
    """
    stop = read_admission_stop(connection, workspace_id=workspace_id)
    if admission_stopped(stop):
        assert stop is not None  # `admission_stopped` refuses `None`.
        raise RunStopError(
            f"emergency admission stop {stop.admission_stop_id!r} is engaged in workspace "
            f"{workspace_id!r} ({stop.reason}); no run is admitted and no effect is "
            "intended until it is released"
        )


__all__ = [
    "ADMISSION_STOP_ENGAGED",
    "ADMISSION_STOP_RELEASED",
    "EVENT_KIND_RUN_STOPPED",
    "REASON_STOPPED_CANCELLED",
    "REASON_STOPPED_SUPERSEDED",
    "REASON_STOPPED_TIMED_OUT",
    "REASON_STOP_DEFERRED_UNCERTAIN",
    "RUNNING_WORK_AWAIT",
    "RUNNING_WORK_POLICIES",
    "RUNNING_WORK_RELEASE",
    "STOP_REASONS",
    "STOP_REASON_CANCELLED",
    "STOP_REASON_SUPERSEDED",
    "STOP_REASON_TIMED_OUT",
    "RunStopError",
    "admission_stopped",
    "decide_stop_settlement",
    "engage_admission_stop",
    "release_admission_stop",
    "request_run_stop",
    "require_admission_open",
    "settle_run_stop",
]
