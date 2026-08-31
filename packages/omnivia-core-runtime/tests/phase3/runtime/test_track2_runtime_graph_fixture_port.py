"""Track 2 clean-room port of the four upstream graph-execution fixtures.

Four scenarios that a node-graph executor states in its own vocabulary are restated
here in the canonical OmniVia language, against the runtime this repository actually
ships. Nothing upstream is vendored: each scenario is re-derived from what the
records mean, and the assertions are about `Run`, `RunStep`, `Attempt`, `Wait`,
`RuntimeEvent` and `WorktreeRef` -- not about nodes, ports or edges.

| Upstream scenario | Canonical restatement |
| --- | --- |
| repeat-join | Connection aggregate completion: a join over N contributions is satisfied only when every contributing `Run` reports a terminal status, and a repeated contribution is refused rather than counted twice. |
| discrete-join | `Loop` iteration identity: each iteration is its own `RunStep`, so an `Attempt` is identified by `(run_step_id, attempt_number)` and never by number alone, and a `Wait` belongs to exactly one iteration. |
| split | Resource-target fan-out: one upstream splits into one `Run` per resource target, and a target is its workspace, its source root and its worktree identifier together. |
| multiplex | Replay / frozen upstream outputs: one output delivered to many consumers is the same output. A `ContextCursor` is a position in a contiguous, append-only stream, so the same cursor delivers the same events to every consumer, and later appends do not disturb what was already delivered. |

Two further assertions are targeted at the policy surface these fixtures land on, and
deliberately stop there: that a fanned-out claim is fair across runs and is not
head-of-line blocked by one that is waiting, and that a run reports its *effective*
policy revision rather than the one it was admitted under.

The scheduler advances one step per queued job, so a multi-step canonical run cannot
be walked end to end through `RuntimeScheduler` alone; where a scenario needs a step
settled without terminalizing its run, it is settled through the repository writers
that RT-102 owns. That is a property of the seam under test, not a workaround, and it
is recorded in the module docstring rather than hidden in a helper.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
import test_rt102_agent_runtime_repository as r102
import test_rt202_policy_budget_snapshot_repository as r202
from omnivia_core_runtime.service.runtime_scheduler import (
    RuntimeScheduler,
    RuntimeSchedulingError,
)
from omnivia_core_runtime.service.worker_adapter import (
    HostLineage,
    ScriptedWorkerEvent,
    WorkerAdapter,
    WorkerEventRejected,
    negotiate_descriptor,
)
from omnivia_core_runtime.storage.agent_runtime import (
    RunSnapshot,
    admit_run,
    append_policy_snapshot,
    append_run_step,
    close_wait,
    finish_attempt,
    open_wait,
    read_run,
    read_run_events,
    read_run_policy_snapshots,
    read_run_steps,
    read_run_waits,
    record_step_status,
    start_attempt,
)
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline

from omnivia_core.contracts.v1 import (
    ATTEMPT_STATUS_SUCCEEDED,
    ContextCursor,
    ContractSemanticError,
    Run,
    WorktreeRef,
    child_run_steps,
    deliver_context,
    is_terminal_run_status,
    validate_worktree_ref,
    waits_under_step,
)

WORKSPACE_ID = m18.WORKSPACE_ID
BASE_US = m18.BASE_US
MS = r102.MS

# One resource target per fanned-out branch. The middle two share a worktree
# identifier under different source roots on purpose: a target is all three members
# together, so they are two targets, not one.
TARGETS: tuple[WorktreeRef, ...] = (
    WorktreeRef(
        workspace_id=WORKSPACE_ID, source_root_id="root-alpha", worktree_id="wt-build"
    ),
    WorktreeRef(
        workspace_id=WORKSPACE_ID, source_root_id="root-beta", worktree_id="wt-shared"
    ),
    WorktreeRef(
        workspace_id=WORKSPACE_ID, source_root_id="root-gamma", worktree_id="wt-shared"
    ),
)

_DIGEST = "sha256:" + "0" * 64


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


# --- fixture construction ---------------------------------------------------------


def _seed(
    holder: m1.Owned,
    *,
    job_id: str,
    run_id: str,
    steps: Sequence[str] = ("step-0001",),
    step_kind: str = "plan",
) -> None:
    """One queued runtime-bound job, its admitted run, and its steps in ordinal order."""
    m18.seed_job(holder, job_id=job_id, state="queued")
    admit_run(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        admission=r102.admission(
            run_id=run_id, job_id=job_id, event_id=f"evt-{run_id}"
        ),
    )
    for ordinal, run_step_id in enumerate(steps, start=1):
        append_run_step(
            holder.connection,
            holder.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=holder.generation,
            run_id=run_id,
            run_step_id=run_step_id,
            ordinal=ordinal,
            step_kind=step_kind,
            created_at_us=BASE_US,
        )


def _scheduler(holder: m1.Owned, *, offset_us: int = 1_000) -> RuntimeScheduler:
    return RuntimeScheduler(
        holder.connection,
        holder.identity,
        WORKSPACE_ID,
        holder.generation,
        m1.FakeClock(
            wall=datetime.fromtimestamp((BASE_US + offset_us) / 1_000_000, UTC)
        ),
    )


def _aggregate(snapshot: RunSnapshot) -> Run:
    """The canonical `Run` a stored snapshot is, field for field.

    A `RunSnapshot` is deliberately not a `Run` (it reports reconciliations and a stop
    command the aggregate has no place for), but every field the aggregate does have
    is one the snapshot already holds. Porting is a projection, never an invention --
    nothing here supplies a value the repository did not read back.
    """
    return Run(
        workspace_id=snapshot.workspace_id,
        run_id=snapshot.run_id,
        definition=snapshot.definition,
        status=snapshot.status,
        logical_key=snapshot.logical_key,
        originating_operation=snapshot.originating_operation,
        audit_reference=snapshot.audit_reference,
        created_at=snapshot.created_at,
        updated_at=snapshot.updated_at,
        finished_at=snapshot.finished_at,
        policy=snapshot.policy,
        budget=snapshot.budget,
        capability_grants=snapshot.capability_grants,
        steps=snapshot.steps,
        waits=snapshot.waits,
        approvals=snapshot.approvals,
        effect_intents=snapshot.effect_intents,
        effect_receipts=snapshot.effect_receipts,
        effect_settlements=snapshot.effect_settlements,
        events=snapshot.events,
        artifacts=snapshot.artifacts,
        evidence=snapshot.evidence,
        cleanup_receipts=snapshot.cleanup_receipts,
        correlations=snapshot.correlations,
    )


def _run_of(holder: m1.Owned, run_id: str) -> Run:
    snapshot = read_run(holder.connection, workspace_id=WORKSPACE_ID, run_id=run_id)
    assert snapshot is not None
    return _aggregate(snapshot)


def _settle_step(
    holder: m1.Owned, *, attempt_id: str, run_step_id: str, at_us: int
) -> None:
    """Terminalize one step and its open attempt without terminalizing the run.

    `RuntimeScheduler.complete` settles the whole run and refuses while any step is
    unfinished, so a join or a loop with more than one step settles its intermediate
    steps here instead. Both writes are the repository's own.
    """
    finish_attempt(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        attempt_id=attempt_id,
        status=ATTEMPT_STATUS_SUCCEEDED,
        finished_at_us=at_us,
    )
    record_step_status(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        run_step_id=run_step_id,
        status="succeeded",
        observed_at_us=at_us,
    )


def _fan_out(holder: m1.Owned) -> tuple[tuple[str, str, WorktreeRef], ...]:
    """One admitted run per resource target, in target order."""
    branches = tuple(
        (f"job-split-{index}", f"run-split-{index}", target)
        for index, target in enumerate(TARGETS, start=1)
    )
    for job_id, run_id, _ in branches:
        _seed(holder, job_id=job_id, run_id=run_id, steps=(f"{run_id}-step",))
    return branches


def _aggregate_complete(holder: m1.Owned, run_ids: Sequence[str]) -> bool:
    """The join: satisfied exactly when every named contribution is terminal.

    Keyed on run identity rather than on how many completions arrived, which is what
    makes a repeated contribution harmless: reading the same terminal run twice is the
    same fact twice, not two contributions.
    """
    return all(
        is_terminal_run_status(_run_of(holder, run_id).status) for run_id in run_ids
    )


# --- split: resource-target fan-out ------------------------------------------------


def test_a_split_admits_one_run_per_resource_target(owned: m1.Owned) -> None:
    branches = _fan_out(owned)

    for _, run_id, target in branches:
        validate_worktree_ref(target, workspace_id=WORKSPACE_ID)
        run = _run_of(owned, run_id)
        # Each branch is its own admission: its own run, its own job, and an event
        # stream that starts at zero rather than continuing the upstream's.
        assert run.events[0].sequence == 0
        assert len(run.steps) == 1

    assert len({run_id for _, run_id, _ in branches}) == len(branches)


def test_a_shared_worktree_name_under_another_source_root_is_another_target() -> None:
    beta, gamma = TARGETS[1], TARGETS[2]

    assert beta.worktree_id == gamma.worktree_id
    assert beta != gamma
    # And the same three members are the same target, so identity is the triple and
    # nothing else: a fan-out over these two is a fan-out of two, not a duplicate.
    assert gamma == dataclasses.replace(beta, source_root_id=gamma.source_root_id)


def test_a_target_from_another_workspace_is_refused_not_reinterpreted() -> None:
    foreign = dataclasses.replace(TARGETS[0], workspace_id=m18.OTHER_WORKSPACE_ID)

    with pytest.raises(ContractSemanticError, match="workspace"):
        validate_worktree_ref(foreign, workspace_id=WORKSPACE_ID)


# --- repeat-join: Connection aggregate completion ----------------------------------


def test_a_join_completes_only_once_every_contribution_is_terminal(
    owned: m1.Owned,
) -> None:
    branches = _fan_out(owned)
    run_ids = [run_id for _, run_id, _ in branches]
    scheduler = _scheduler(owned)

    assert _aggregate_complete(owned, run_ids) is False
    for index, run_id in enumerate(run_ids, start=1):
        claim = scheduler.claim_next()
        assert claim is not None and claim.run_id == run_id
        scheduler.complete(claim, result_kind="runtime.step", result={"branch": run_id})
        # The join is unsatisfied for as long as one contribution has not landed.
        assert _aggregate_complete(owned, run_ids) is (index == len(run_ids))

    assert all(is_terminal_run_status(_run_of(owned, r).status) for r in run_ids)


def test_a_repeated_contribution_is_refused_rather_than_counted_twice(
    owned: m1.Owned,
) -> None:
    _seed(owned, job_id="job-repeat", run_id="run-repeat", steps=("step-repeat",))
    scheduler = _scheduler(owned)
    claim = scheduler.claim_next()
    assert claim is not None
    scheduler.complete(claim, result_kind="runtime.step", result={})
    before = read_run_events(
        owned.connection, workspace_id=WORKSPACE_ID, run_id="run-repeat"
    )

    with pytest.raises(RuntimeSchedulingError):
        scheduler.complete(claim, result_kind="runtime.step", result={})

    assert (
        read_run_events(
            owned.connection, workspace_id=WORKSPACE_ID, run_id="run-repeat"
        )
        == before
    )


# --- discrete-join: Loop iteration identity ----------------------------------------


def test_each_loop_iteration_carries_its_own_attempt_and_wait_identity(
    owned: m1.Owned,
) -> None:
    iterations = ("iteration-0001", "iteration-0002", "iteration-0003")
    _seed(owned, job_id="job-loop", run_id="run-loop", steps=iterations)

    for index, run_step_id in enumerate(iterations, start=1):
        at_us = BASE_US + index * MS
        # Attempt number one *of this iteration*: identity is the pair, so three
        # iterations produce three distinct attempts that all number themselves 1.
        start_attempt(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
            attempt_id=f"attempt-{run_step_id}",
            run_id="run-loop",
            run_step_id=run_step_id,
            attempt_number=1,
            started_at_us=at_us,
        )
        open_wait(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
            wait_id=f"wait-{run_step_id}",
            run_id="run-loop",
            run_step_id=run_step_id,
            kind="external_signal",
            created_at_us=at_us,
            resume_digest=_DIGEST,
        )
        close_wait(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
            wait_id=f"wait-{run_step_id}",
            status="resolved",
            resolved_at_us=at_us + 1,
            resolution_reason="external_signal",
        )
        _settle_step(
            owned, attempt_id=f"attempt-{run_step_id}", run_step_id=run_step_id, at_us=at_us + 2
        )

    steps = read_run_steps(
        owned.connection, workspace_id=WORKSPACE_ID, run_id="run-loop"
    )
    attempts = [attempt for step in steps for attempt in step.attempts]
    assert [attempt.attempt_number for attempt in attempts] == [1, 1, 1]
    assert len({attempt.attempt_id for attempt in attempts}) == len(iterations)
    assert [attempt.run_step_id for attempt in attempts] == list(iterations)

    waits = read_run_waits(
        owned.connection, workspace_id=WORKSPACE_ID, run_id="run-loop"
    )
    assert [wait.run_step_id for wait in waits] == list(iterations)


def test_a_wait_is_reported_under_its_own_iteration_and_no_other(
    owned: m1.Owned,
) -> None:
    iterations = ("iteration-0001", "iteration-0002")
    _seed(owned, job_id="job-discrete", run_id="run-discrete", steps=iterations)
    for index, run_step_id in enumerate(iterations, start=1):
        open_wait(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
            wait_id=f"wait-{run_step_id}",
            run_id="run-discrete",
            run_step_id=run_step_id,
            kind="external_signal",
            created_at_us=BASE_US + index * MS,
            resume_digest=_DIGEST,
        )
        record_step_status(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
            run_step_id=run_step_id,
            status="waiting",
            observed_at_us=BASE_US + index * MS,
        )

    run = _run_of(owned, "run-discrete")

    for run_step_id in iterations:
        held = waits_under_step(run, run_step_id=run_step_id)
        assert [wait.wait_id for wait in held] == [f"wait-{run_step_id}"]
    # Storage records no step parentage, so every iteration reads as a root step and
    # `child_run_steps` is empty for all of them. Asserted rather than skipped: it is
    # what makes the iterations siblings under one run instead of a nested chain.
    assert all(child_run_steps(run, run_step_id=s) == () for s in iterations)


# --- multiplex: replay and frozen upstream outputs ---------------------------------


def test_one_cursor_delivers_the_same_output_to_every_consumer(
    owned: m1.Owned,
) -> None:
    _seed(owned, job_id="job-mux", run_id="run-mux", steps=("step-mux",))
    scheduler = _scheduler(owned)
    claim = scheduler.claim_next()
    assert claim is not None
    run = _run_of(owned, "run-mux")
    cursor = ContextCursor(
        workspace_id=WORKSPACE_ID,
        run_id="run-mux",
        run_step_id=claim.run_step_id,
        attempt_id=claim.runtime_attempt_id,
        next_sequence=0,
        max_items=8,
        issued_at=run.created_at,
    )

    first_consumer, advanced = deliver_context(
        cursor, run=run, workspace_id=WORKSPACE_ID
    )
    second_consumer, advanced_again = deliver_context(
        cursor, run=run, workspace_id=WORKSPACE_ID
    )

    assert first_consumer == second_consumer
    assert advanced == advanced_again
    # The cursor a delivery returns yields only what came after it, so multiplexing is
    # a fan-out of reads and never a queue two consumers race each other to drain.
    assert deliver_context(advanced, run=run, workspace_id=WORKSPACE_ID)[0] == ()


def test_a_later_append_does_not_disturb_what_was_already_delivered(
    owned: m1.Owned,
) -> None:
    _seed(owned, job_id="job-frozen", run_id="run-frozen", steps=("step-frozen",))
    scheduler = _scheduler(owned)
    claim = scheduler.claim_next()
    assert claim is not None
    run = _run_of(owned, "run-frozen")
    cursor = ContextCursor(
        workspace_id=WORKSPACE_ID,
        run_id="run-frozen",
        run_step_id=claim.run_step_id,
        attempt_id=claim.runtime_attempt_id,
        next_sequence=0,
        max_items=8,
        issued_at=run.created_at,
    )
    delivered, advanced = deliver_context(cursor, run=run, workspace_id=WORKSPACE_ID)

    scheduler.complete(claim, result_kind="runtime.step", result={})
    extended = _run_of(owned, "run-frozen")

    assert len(extended.events) > len(run.events)
    assert extended.events[: len(delivered)] == delivered
    assert deliver_context(cursor, run=extended, workspace_id=WORKSPACE_ID)[0][
        : len(delivered)
    ] == delivered
    # And the events after it are exactly what the advanced cursor now sees.
    assert deliver_context(advanced, run=extended, workspace_id=WORKSPACE_ID)[0] == (
        extended.events[len(delivered) :]
    )


def test_a_recorded_output_cannot_be_edited_into_a_different_one(
    owned: m1.Owned,
) -> None:
    _seed(owned, job_id="job-immutable", run_id="run-immutable", steps=("step-imm",))
    before = read_run_events(
        owned.connection, workspace_id=WORKSPACE_ID, run_id="run-immutable"
    )

    with pytest.raises(sqlite3.DatabaseError), m18.guarded(owned):
        owned.connection.execute(
            "UPDATE omnivia_runtime_events SET message = 'rewritten' "
            "WHERE workspace_id = ? AND run_id = ?",
            (WORKSPACE_ID, "run-immutable"),
        )

    assert (
        read_run_events(
            owned.connection, workspace_id=WORKSPACE_ID, run_id="run-immutable"
        )
        == before
    )


# --- scheduler fairness across a fan-out -------------------------------------------


def test_a_fan_out_is_claimed_in_order_and_each_branch_exactly_once(
    owned: m1.Owned,
) -> None:
    branches = _fan_out(owned)
    scheduler = _scheduler(owned)

    claimed = [scheduler.claim_next() for _ in branches]

    assert [claim.run_id for claim in claimed if claim is not None] == [
        run_id for _, run_id, _ in branches
    ]
    assert scheduler.claim_next() is None


def test_a_waiting_branch_does_not_block_the_rest_of_the_fan_out(
    owned: m1.Owned,
) -> None:
    _seed(owned, job_id="job-a-blocked", run_id="run-a-blocked", steps=("step-blocked",))
    _seed(owned, job_id="job-b-ready", run_id="run-b-ready", steps=("step-ready",))
    open_wait(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        wait_id="wait-blocked",
        run_id="run-a-blocked",
        run_step_id="step-blocked",
        kind="external_signal",
        created_at_us=BASE_US + MS,
        resume_digest=_DIGEST,
    )

    # The blocked branch sorts first, so serving the ready one proves the scheduler
    # skips rather than stalls at the head of the queue.
    claim = _scheduler(owned).claim_next()

    assert claim is not None and claim.run_id == "run-b-ready"


# --- effective policy reporting ----------------------------------------------------


def test_a_run_reports_the_policy_revision_in_force_not_the_one_it_opened_with(
    owned: m1.Owned,
) -> None:
    _seed(owned, job_id="job-policy", run_id="run-policy", steps=("step-policy",))
    admitted = r202.policy(run_id="run-policy", policy_snapshot_id="policy-0001")
    narrowed = r202.policy(
        run_id="run-policy",
        policy_snapshot_id="policy-0002",
        revision=2,
        pinned_at=r202.pinned_at(1),
        granted_capabilities=("memory.read",),
    )
    for snapshot in (admitted, narrowed):
        append_policy_snapshot(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
            snapshot=snapshot,
        )

    effective = read_run(
        owned.connection, workspace_id=WORKSPACE_ID, run_id="run-policy"
    )

    assert effective is not None and effective.policy is not None
    assert effective.policy.revision == 2
    assert effective.policy.granted_capabilities == ("memory.read",)
    # The superseded revision is still readable: narrowing appends, it never edits.
    history = read_run_policy_snapshots(
        owned.connection, workspace_id=WORKSPACE_ID, run_id="run-policy"
    )
    assert [stored.snapshot.revision for stored in history] == [1, 2]


# --- worker child-environment hygiene ----------------------------------------------


def test_the_worker_seam_carries_no_environment_channel_at_all() -> None:
    surfaces = (
        ScriptedWorkerEvent,
        HostLineage,
        negotiate_descriptor(requested_contract_version=1).__class__,
    )

    names = {
        field.name for surface in surfaces for field in dataclasses.fields(surface)
    }

    # Nothing a worker is handed, and nothing it hands back, is an environment: there
    # is no field for a child process to inherit one through, so the hygiene rule is
    # structural rather than a scrub applied to a channel that exists.
    assert not {name for name in names if "env" in name or "environ" in name}


def test_an_environment_shaped_secret_never_reaches_the_sink() -> None:
    lineage = HostLineage(
        workspace_id=WORKSPACE_ID,
        run_id="run-worker",
        run_step_id="step-worker",
        attempt_id="attempt-worker",
    )
    adapter = WorkerAdapter()
    opened = adapter.open(
        lineage=lineage,
        requested_contract_version=1,
        script=(
            ScriptedWorkerEvent(
                source_event_id="evt-0001",
                kind="diagnostic",
                occurred_at_us=BASE_US,
                safe_summary="child env: AWS_SECRET_ACCESS_KEY=AKIAEXAMPLE0000",
            ),
        ),
    )
    delivered: list[object] = []

    with pytest.raises(WorkerEventRejected, match="secret"):
        adapter.start(session_id=opened.session_id, sink=delivered.append)

    assert delivered == []
