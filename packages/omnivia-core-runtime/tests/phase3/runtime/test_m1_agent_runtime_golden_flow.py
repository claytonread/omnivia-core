"""M1 golden-flow acceptance: claim, suspend, restart, adopt, resolve, complete.

What survives a real crash here is exactly the canonical Run/Wait/Step/Attempt: the
SQLite connection is genuinely closed and reopened by a successor service instance at
a higher fencing generation, and startup recovery adopts the durable wait from what
the file says, not from anything held in memory. The RT-108 deterministic worker
adapter is an in-memory session with no store of its own -- it is disposed *before*
the restart, and the successor drives a fresh adapter instance opened against the
same canonical lineage. That is a replacement/reconnection, not a persisted external
worker session, and this test claims no live Platform route or durable worker
transport -- only the canonical runtime state RT-102/104/106/107/108/109 already own.
"""

from __future__ import annotations

import dataclasses
import sqlite3

import pytest
import test_rt104_runtime_command_transaction as rt104
import test_rt109_runtime_recovery as rt109
from omnivia_core_runtime.ownership.fencing import StaleGeneration
from omnivia_core_runtime.service.mutation import MutationIdempotencyConflict
from omnivia_core_runtime.service.runtime_command import RuntimeAggregateExpectation
from omnivia_core_runtime.service.runtime_recovery import (
    CLASSIFICATION_DURABLE_OPEN_WAIT,
    recover_at_startup,
)
from omnivia_core_runtime.service.runtime_waits import resolve_runtime_wait
from omnivia_core_runtime.service.worker_adapter import (
    ADAPTER_TYPE_DETERMINISTIC,
    HostLineage,
    ScriptedWorkerEvent,
    WorkerAdapterRegistry,
    WorkerStateError,
)
from omnivia_core_runtime.storage.agent_runtime import (
    append_run_event,
    read_run,
    read_run_sequence,
)
from omnivia_core_runtime.storage.projections.runtime_run_summary import (
    rebuild_runtime_run_summaries,
    runtime_run_summary_projection_digest,
)

from omnivia_core.contracts.v1 import ResolveWait

owned = rt109.owned

WAIT_ID = "wait-m1-golden"
RESOLVE_KEY = "m1-golden-resolve-0001"


def _scripted(source_event_id: str, kind: str, **kwargs: object) -> ScriptedWorkerEvent:
    return ScriptedWorkerEvent(
        source_event_id=source_event_id,
        kind=kind,
        occurred_at_us=1_000,
        safe_summary="ok",
        **kwargs,
    )


def test_m1_golden_flow_survives_restart_resolves_and_completes(
    owned: rt109.m1.Owned,
) -> None:
    seeded = rt109.seed(owned, "m1-golden")
    claimed = rt109.claim(owned)
    assert claimed.job_id == seeded.job_id
    assert claimed.run_id == seeded.run_id

    lineage = HostLineage(
        workspace_id=rt109.WORKSPACE_ID,
        run_id=claimed.run_id,
        run_step_id=claimed.run_step_id,
        attempt_id=claimed.runtime_attempt_id,
    )

    # --- 3/4: a deterministic worker session, with a harmless duplicate ---------

    registry = WorkerAdapterRegistry()
    pre_restart_adapter = registry.create(adapter_type=ADAPTER_TYPE_DETERMINISTIC)

    script = (
        _scripted("evt-1", "message_delta"),
        _scripted("evt-1", "message_delta"),  # identical duplicate: replay is harmless
        _scripted("evt-2", "wait_requested"),
        _scripted("evt-3", "message_completed"),
        _scripted("evt-4", "turn_completed"),
    )

    sink_events: list[str] = []
    suspended = {"count": 0}

    def sink(event: object) -> None:
        sink_events.append(event.source_event_id)  # type: ignore[attr-defined]
        if event.kind == "wait_requested":  # type: ignore[attr-defined]
            suspended["count"] += 1
            rt109.suspend(owned, claimed, wait_id=WAIT_ID)

    opened = pre_restart_adapter.open(lineage=lineage, script=script)
    result = pre_restart_adapter.start(session_id=opened.session_id, sink=sink)

    assert result.state == "waiting"
    assert sink_events == ["evt-1", "evt-2"]
    assert suspended["count"] == 1

    # --- 5: capture a stale scheduler, prove the wait is durable, dispose -------

    stale = rt109.scheduler_at(owned)
    before_restart = read_run(
        owned.connection, workspace_id=rt109.WORKSPACE_ID, run_id=claimed.run_id
    )
    assert before_restart is not None
    assert before_restart.status == "waiting"
    assert before_restart.waits[0].wait_id == WAIT_ID
    assert before_restart.steps[0].run_step_id == claimed.run_step_id
    assert [a.attempt_id for a in before_restart.steps[0].attempts] == [
        claimed.runtime_attempt_id
    ]

    pre_restart_adapter.dispose()
    assert pre_restart_adapter.session_count == 0
    with pytest.raises(WorkerStateError, match="disposed"):
        pre_restart_adapter.open(lineage=lineage, script=())

    # --- 6: a real restart, and recovery adopts the durable wait ----------------

    successor = rt109.restart(owned)
    report = recover_at_startup(rt109.scheduler_at(successor))

    assert (
        rt109.classification_of(report, claimed.job_id) == CLASSIFICATION_DURABLE_OPEN_WAIT
    )
    assert len(report.adoptions) == 1
    adoption = report.adoptions[0]
    assert adoption.wait_id == WAIT_ID
    assert adoption.run_step_id == claimed.run_step_id
    assert adoption.runtime_attempt_id == claimed.runtime_attempt_id
    assert adoption.previous_fencing_generation == owned.generation
    assert successor.generation > owned.generation

    # --- 7: the stale owner is refused, and mutates nothing ---------------------

    stale.connection = successor.connection
    ledger_before_stale_attempt = rt109.ledger(successor)

    with pytest.raises(StaleGeneration):
        stale.complete(
            claimed, result_kind="runtime_completion", result={"outcome": "complete"}
        )

    assert rt109.ledger(successor) == ledger_before_stale_attempt

    # --- 8: resolve the adopted wait as one RT-104 command -----------------------

    command = ResolveWait(
        workspace_id=rt109.WORKSPACE_ID,
        run_id=claimed.run_id,
        wait_id=WAIT_ID,
        resolution="external_signal",
        approval_id=None,
        resume_digest=rt109.DIGEST,
        requested_at=rt109.timestamp(rt109.RESOLVE_US),
        reason="signal_received",
    )
    wire = command.to_wire()
    context = rt104.authorize(operation_input=wire, idempotency_key=RESOLVE_KEY)
    equivalence = rt104.equivalence_for(operation_input=wire, idempotency_key=RESOLVE_KEY)
    grant = rt104.issue(successor, context, equivalence=equivalence)

    def no_approval(_context: object, _command: object, _wait: object) -> None:
        return None

    counts_before_resolve = _runtime_counts(successor.connection)
    sequence = read_run_sequence(
        successor.connection, workspace_id=rt109.WORKSPACE_ID, run_id=claimed.run_id
    )
    outcome = resolve_runtime_wait(
        successor.connection,
        successor.identity,
        grant=grant,
        context=context,
        equivalence=equivalence,
        command=command,
        policy=no_approval,
        runtime_event_id=f"evt-{RESOLVE_KEY}",
        validate_result=rt109.s0.accept_any,
        clock=rt109.clock_at(rt109.RESOLVE_US),
        expected=RuntimeAggregateExpectation(run_id=claimed.run_id, sequence=sequence),
    )

    assert outcome.replayed is False
    assert outcome.result["status"] == "resolved"
    snapshot = read_run(
        successor.connection, workspace_id=rt109.WORKSPACE_ID, run_id=claimed.run_id
    )
    assert snapshot is not None
    assert snapshot.status == "running"
    assert snapshot.steps[0].run_step_id == claimed.run_step_id
    assert snapshot.steps[0].status == "running"
    assert [a.attempt_id for a in snapshot.steps[0].attempts] == [
        claimed.runtime_attempt_id
    ]
    assert snapshot.steps[0].attempts[-1].status == "running"
    counts_after_resolve = _runtime_counts(successor.connection)
    assert counts_after_resolve == {
        **counts_before_resolve,
        "omnivia_runtime_events": counts_before_resolve["omnivia_runtime_events"] + 1,
        "omnivia_runtime_wait_resolutions": counts_before_resolve[
            "omnivia_runtime_wait_resolutions"
        ]
        + 1,
        "omnivia_mutation_executions": counts_before_resolve["omnivia_mutation_executions"]
        + 1,
    }

    # --- 9: the exact same command/key replays without a second write -----------

    replay_context = rt104.authorize(operation_input=wire, idempotency_key=RESOLVE_KEY)
    replay_equivalence = rt104.equivalence_for(
        operation_input=wire, idempotency_key=RESOLVE_KEY
    )
    replay_grant = rt104.issue(successor, replay_context, equivalence=replay_equivalence)

    replayed = resolve_runtime_wait(
        successor.connection,
        successor.identity,
        grant=replay_grant,
        context=replay_context,
        equivalence=replay_equivalence,
        command=command,
        policy=no_approval,
        runtime_event_id=f"evt-{RESOLVE_KEY}",
        validate_result=rt109.s0.accept_any,
        clock=rt109.clock_at(rt109.RESOLVE_US),
        expected=RuntimeAggregateExpectation(run_id=claimed.run_id, sequence=sequence),
    )

    assert replayed.replayed is True
    assert replayed.result == outcome.result
    assert (
        read_run_sequence(
            successor.connection, workspace_id=rt109.WORKSPACE_ID, run_id=claimed.run_id
        )
        == sequence + 1
    )
    assert _runtime_counts(successor.connection) == {
        **counts_after_resolve,
        "omnivia_mutation_executions": counts_after_resolve["omnivia_mutation_executions"]
        + 1,
    }

    # --- 10: the same key with a different request is a typed conflict ---------

    conflicting_command = dataclasses.replace(command, reason="a_different_reason")
    conflicting_wire = conflicting_command.to_wire()
    conflicting_context = rt104.authorize(
        operation_input=conflicting_wire, idempotency_key=RESOLVE_KEY
    )
    conflicting_equivalence = rt104.equivalence_for(
        operation_input=conflicting_wire, idempotency_key=RESOLVE_KEY
    )
    conflicting_grant = rt104.issue(
        successor, conflicting_context, equivalence=conflicting_equivalence
    )
    counts_before_conflict = _runtime_counts(successor.connection)

    with pytest.raises(MutationIdempotencyConflict):
        resolve_runtime_wait(
            successor.connection,
            successor.identity,
            grant=conflicting_grant,
            context=conflicting_context,
            equivalence=conflicting_equivalence,
            command=conflicting_command,
            policy=no_approval,
            runtime_event_id=f"evt-{RESOLVE_KEY}-conflict",
            validate_result=rt109.s0.accept_any,
            clock=rt109.clock_at(rt109.RESOLVE_US),
            expected=RuntimeAggregateExpectation(
                run_id=claimed.run_id, sequence=sequence
            ),
        )

    assert _runtime_counts(successor.connection) == counts_before_conflict

    # --- 11: reusing the already-used runtime event identity is refused ---------

    sequence_before_repeat = read_run_sequence(
        successor.connection, workspace_id=rt109.WORKSPACE_ID, run_id=claimed.run_id
    )
    counts_before_repeat = _runtime_counts(successor.connection)

    with pytest.raises(sqlite3.Error, match="identifier is used at most once"):
        append_run_event(
            successor.connection,
            successor.identity,
            workspace_id=rt109.WORKSPACE_ID,
            fencing_generation=successor.generation,
            run_id=claimed.run_id,
            runtime_event_id=f"evt-{RESOLVE_KEY}",
            occurred_at_us=rt109.RESOLVE_US + 1_000,
            event_kind="worker_progress",
            run_status="running",
        )

    assert (
        read_run_sequence(
            successor.connection, workspace_id=rt109.WORKSPACE_ID, run_id=claimed.run_id
        )
        == sequence_before_repeat
    )
    assert _runtime_counts(successor.connection) == counts_before_repeat

    # --- 12: a fresh adapter after restart, same canonical lineage --------------

    post_restart_registry = WorkerAdapterRegistry()
    post_restart_adapter = post_restart_registry.create(
        adapter_type=ADAPTER_TYPE_DETERMINISTIC
    )
    continuation_script = (_scripted("evt-5", "turn_completed"),)
    continuation_sink_events: list[str] = []
    reopened = post_restart_adapter.open(lineage=lineage, script=continuation_script)
    completion = post_restart_adapter.start(
        session_id=reopened.session_id,
        sink=lambda event: continuation_sink_events.append(event.source_event_id),
    )

    assert completion.state == "completed"
    assert continuation_sink_events == ["evt-5"]

    # --- 13/14: complete the adopted claim, and check nothing else settled -----

    adopted_claim = dataclasses.replace(
        claimed,
        service_instance_id=successor.identity.service_instance_id,
        fencing_generation=successor.generation,
    )
    rt109.scheduler_at(successor, now_us=rt109.RESOLVE_US + 5_000).complete(
        adopted_claim, result_kind="runtime_completion", result={"outcome": "complete"}
    )

    final_snapshot = read_run(
        successor.connection, workspace_id=rt109.WORKSPACE_ID, run_id=claimed.run_id
    )
    assert final_snapshot is not None
    assert final_snapshot.status == "succeeded"
    assert final_snapshot.steps[0].status == "succeeded"
    assert [a.attempt_id for a in final_snapshot.steps[0].attempts] == [
        claimed.runtime_attempt_id
    ]
    assert final_snapshot.steps[0].attempts[-1].status == "succeeded"
    assert final_snapshot.waits[0].wait_id == WAIT_ID
    assert final_snapshot.waits[0].status == "resolved"
    assert rt109.job_row(successor, claimed.job_id)[0] == "succeeded"

    # --- 15: dispose the successor's adapter and confirm the refusal -----------

    post_restart_adapter.dispose()
    assert post_restart_adapter.session_count == 0
    with pytest.raises(WorkerStateError, match="disposed"):
        post_restart_adapter.open(lineage=lineage, script=())

    # --- 16: the projection replays to the same digest --------------------------

    live_digest = runtime_run_summary_projection_digest(
        successor.connection, workspace_id=rt109.WORKSPACE_ID
    )
    rebuilt = rebuild_runtime_run_summaries(
        successor.connection,
        successor.identity,
        workspace_id=rt109.WORKSPACE_ID,
        fencing_generation=successor.generation,
    )
    assert rebuilt.build_digest == live_digest
    assert (
        runtime_run_summary_projection_digest(
            successor.connection, workspace_id=rt109.WORKSPACE_ID
        )
        == live_digest
    )

    successor.connection.close()


def _runtime_counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = (
        "omnivia_runtime_events",
        "omnivia_runtime_wait_resolutions",
        "omnivia_runtime_attempt_outcomes",
        "omnivia_mutation_executions",
    )
    return {table: rt109.m1.count(connection, table) for table in tables}
