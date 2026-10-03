"""C21 T3 acceptance: external-signal telemetry at the wait authority.

An accepted signal is observed by the fenced transaction that resolves its wait, so the wait
closes and its observation lands together, or neither does. A refused signal rolls that
transaction back, then is recorded by a fenced write of its own: a `refused` audit event that
states the outcome, and a dead-lettered observation that names it. The wait authority decides;
these tests assert what it recorded, and what it deliberately did not.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
import test_rt104_runtime_command_transaction as rt104
import test_rt107_runtime_waits as rt107
import test_t0693_workflow_application as app
import test_t0693_workflow_live_runtime as live
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.ownership.fencing import StaleGeneration, read_guard
from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.ownership.lease import acquire_lease
from omnivia_core_runtime.service import runtime_waits
from omnivia_core_runtime.service.handlers.workflow import WORKFLOW_CONTROL_OPERATION
from omnivia_core_runtime.service.mutation import issue_mutation_grant
from omnivia_core_runtime.service.runtime_command import (
    RuntimeAggregateExpectation,
    execute_runtime_command,
)
from omnivia_core_runtime.service.runtime_waits import (
    WaitNotFound,
    WaitOpening,
    WaitPolicyDenied,
    WaitResolutionConflict,
    open_runtime_wait,
    resolve_runtime_wait,
)
from omnivia_core_runtime.storage.agent_runtime import (
    RunAdmission,
    admit_run,
    record_step_status,
    start_attempt,
)
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.storage.trigger_telemetry import (
    read_wait_signal_observation,
    read_wait_signal_telemetry,
)

from omnivia_core.contracts.v1 import ErrorResponseEnvelope

admitted = rt107.admitted
running = rt107.running
owned = live.owned

WORKSPACE_ID = rt107.WORKSPACE_ID
OTHER_WORKSPACE = m1.OTHER_WORKSPACE_ID
WAIT_ID = rt107.WAIT_ID
DIGEST = rt107.DIGEST
RESOLVE_US = rt107.RESOLVE_US
refused_signals = rt107.refused_signals
without_audit = rt107.without_audit
RecordingPolicy = rt107.RecordingPolicy


def counts(holder: m1.Owned) -> dict[str, int]:
    return rt104.counts(holder.connection)


def audit_row(connection: Any, audit_ref: str) -> tuple[str, str | None]:
    """The outcome class and error code the audit event of `audit_ref` states."""
    row = connection.execute(
        "SELECT outcome_class, error_code FROM omnivia_application_audit_events "
        "WHERE audit_ref = ? AND workspace_id = ?",
        (audit_ref, WORKSPACE_ID),
    ).fetchone()
    return (str(row[0]), None if row[1] is None else str(row[1]))


def supersede(holder: m1.Owned, *, instance: str) -> None:
    """A takeover: another service instance acquires the lease and the generation moves."""
    taken = acquire_lease(
        holder.connection,
        m1.make_identity(instance, pid=4545),
        clock=FakeClock(),
        workspace_id=WORKSPACE_ID,
        holds_storage_lock=True,
        lock_mechanism="flock",
    )
    assert taken.fencing_generation > holder.generation


def telemetry_of(holder: m1.Owned) -> Any:
    telemetry = read_wait_signal_telemetry(
        holder.connection, workspace_id=WORKSPACE_ID, wait_id=WAIT_ID
    )
    assert telemetry is not None
    return telemetry


def test_an_accepted_signal_is_observed_by_the_resolution_that_accepted_it(
    running: m1.Owned,
) -> None:
    rt107.open_wait(running)
    rt107.resolve_wait(running, policy=RecordingPolicy())

    telemetry = telemetry_of(running)
    observed = telemetry.last_observation
    assert telemetry.wait_status == "resolved"
    assert telemetry.observation_total == 1
    assert observed is not None
    assert observed.delivery_status == "accepted"
    assert observed.delivery_reason is None
    assert observed.event_id.startswith("sig-")
    assert observed.envelope_digest.startswith("sha256:")
    # Observed at the instant the wait closed, because the two are written together.
    resolved_at = running.connection.execute(
        "SELECT resolved_at_us FROM omnivia_runtime_wait_resolutions "
        "WHERE workspace_id = ? AND wait_id = ?",
        (WORKSPACE_ID, WAIT_ID),
    ).fetchone()
    assert resolved_at is not None
    assert observed.observed_at_us == int(resolved_at[0])
    # The observation is tied to the settlement's own audit event, which succeeded.
    assert audit_row(running.connection, observed.audit_ref) == ("succeeded", None)
    # The command states no source time, so the read model says so rather than guessing.
    assert telemetry.uncertainty == ("source_time_unknown",)


def test_an_accepted_signal_is_recorded_once_however_often_it_is_repeated(
    running: m1.Owned,
) -> None:
    rt107.open_wait(running)
    rt107.resolve_wait(running, policy=RecordingPolicy())
    settled = counts(running)

    # The same key is answered from the mutation seam's stored outcome: no settlement runs,
    # so no audit event and no observation is written. The seam does spend the grant it was
    # presented with, which is its own ledger and not a trigger fact.
    rt107.resolve_wait(running, policy=RecordingPolicy(), expected_sequence=3)
    assert (
        counts(running)["omnivia_application_audit_events"]
        == settled["omnivia_application_audit_events"]
    )
    assert telemetry_of(running).observation_total == 1

    # A second key carrying the same resolution is answered from the resolution the wait
    # already holds, so the wait authority writes no second observation for it.
    rt107.resolve_wait(
        running,
        policy=RecordingPolicy(),
        key="c21-repeat-0001",
        expected_sequence=3,
    )
    assert telemetry_of(running).observation_total == 1


def test_an_accepted_signal_that_cannot_be_observed_leaves_its_wait_pending(
    running: m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt107.open_wait(running)
    before = counts(running)

    class RefusingObservation:
        def record_wait_signal(self, **_: object) -> None:
            raise StorageError("the test refuses this observation")

    monkeypatch.setattr(
        runtime_waits,
        "transaction_local_telemetry_writer",
        lambda connection, *, workspace_id: RefusingObservation(),
    )
    with pytest.raises(StorageError, match="refuses this observation"):
        rt107.resolve_wait(running, policy=RecordingPolicy())

    assert counts(running) == before
    telemetry = telemetry_of(running)
    assert telemetry.wait_status == "pending"
    assert telemetry.observation_total == 0


def test_a_refused_signal_rolls_back_and_is_recorded_as_refused(
    running: m1.Owned,
) -> None:
    rt107.open_wait(running)
    rt107.resolve_wait(running, policy=RecordingPolicy())
    settled = counts(running)

    with pytest.raises(
        WaitResolutionConflict, match="resolved exactly once"
    ) as refusal:
        rt107.resolve_wait(
            running,
            policy=RecordingPolicy(),
            command=rt107.resolution(reason="different_signal"),
            key="c21-refused-0001",
            expected_sequence=3,
        )

    audit_ref = refusal.value.audit_reference
    assert audit_ref is not None
    assert refusal.value.code == "conflict"
    assert "no workflow mutation was committed" in str(refusal.value)
    assert audit_row(running.connection, audit_ref) == ("refused", "conflict")
    # The workflow side is exactly as the accepted resolution left it.
    assert without_audit(counts(running)) == without_audit(settled)
    assert refused_signals(running.connection) == [("wait_already_resolved", "refused")]
    telemetry = telemetry_of(running)
    assert telemetry.wait_status == "resolved"
    assert telemetry.observation_total == 2
    assert telemetry.delivery_counts == {
        "accepted": 1,
        "duplicate": 0,
        "dead_lettered": 1,
        "uncertain": 0,
    }
    assert telemetry.last_observation is not None
    assert telemetry.last_observation.delivery_status == "dead_lettered"
    assert telemetry.last_observation.audit_ref == audit_ref


def test_a_signal_refused_for_its_payload_is_recorded_as_payload_rejected(
    running: m1.Owned,
) -> None:
    rt107.open_wait(running)
    settled = counts(running)

    with pytest.raises(WaitResolutionConflict, match="resume_digest") as refusal:
        rt107.resolve_wait(
            running,
            policy=RecordingPolicy(),
            command=rt107.resolution(resume_digest="sha256:" + "f" * 64),
        )

    assert refusal.value.audit_reference is not None
    assert refused_signals(running.connection) == [("payload_rejected", "refused")]
    assert without_audit(counts(running)) == without_audit(settled)
    assert telemetry_of(running).wait_status == "pending"


def test_a_malformed_signal_is_recorded_as_payload_rejected(running: m1.Owned) -> None:
    rt107.open_wait(running)

    # An external signal names no approval; the shape check refuses one that does.
    with pytest.raises(WaitResolutionConflict, match="names no approval"):
        rt107.resolve_wait(
            running,
            policy=RecordingPolicy(),
            command=rt107.resolution(approval_id="apr-c21-0001"),
        )

    assert refused_signals(running.connection) == [("payload_rejected", "refused")]


def test_a_signal_after_its_deadline_is_recorded_as_deadline_passed(
    running: m1.Owned,
) -> None:
    rt107.open_wait(running, expires_at_us=RESOLVE_US)

    with pytest.raises(WaitResolutionConflict, match="has expired"):
        rt107.resolve_wait(
            running,
            policy=RecordingPolicy(),
            settled_at_us=RESOLVE_US + 10_000,
        )

    assert refused_signals(running.connection) == [("deadline_passed", "refused")]
    # The wait itself is not resolved by a refused signal, so it stays pending.
    assert telemetry_of(running).wait_status == "pending"


def test_a_retry_of_a_refused_signal_under_its_key_names_the_record_it_has(
    running: m1.Owned,
) -> None:
    rt107.open_wait(running)
    rt107.resolve_wait(running, policy=RecordingPolicy())
    refused = {
        "command": rt107.resolution(reason="different_signal"),
        "key": "c21-retry-0001",
        "expected_sequence": 3,
    }

    with pytest.raises(WaitResolutionConflict) as first:
        rt107.resolve_wait(running, policy=RecordingPolicy(), **refused)
    recorded = counts(running)
    with pytest.raises(WaitResolutionConflict) as second:
        rt107.resolve_wait(running, policy=RecordingPolicy(), **refused)

    assert second.value.audit_reference == first.value.audit_reference
    assert counts(running) == recorded
    assert refused_signals(running.connection) == [("wait_already_resolved", "refused")]


def test_a_refusal_written_under_a_superseded_generation_is_not_recorded(
    running: m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt107.open_wait(running)
    rt107.resolve_wait(running, policy=RecordingPolicy())
    settled = counts(running)
    refusal_record = runtime_waits._recorded_refusal

    def supersede_then_record(*args: Any, **kwargs: Any) -> Any:
        # The settlement has rolled back. Authority moves before the refusal is written.
        supersede(running, instance="svc-c21-takeover")
        return refusal_record(*args, **kwargs)

    monkeypatch.setattr(runtime_waits, "_recorded_refusal", supersede_then_record)
    with pytest.raises(StaleGeneration):
        rt107.resolve_wait(
            running,
            policy=RecordingPolicy(),
            command=rt107.resolution(reason="different_signal"),
            key="c21-stale-0001",
            expected_sequence=3,
        )

    assert counts(running) == settled
    assert refused_signals(running.connection) == []


def test_a_refusal_the_telemetry_cannot_attach_to_records_nothing(
    running: m1.Owned,
) -> None:
    rt107.open_wait(running)
    before = counts(running)

    with pytest.raises(WaitNotFound):
        rt107.resolve_wait(
            running,
            policy=RecordingPolicy(),
            command=rt107.resolution(wait_id="wait-c21-missing"),
        )

    assert counts(running) == before
    assert refused_signals(running.connection) == []


def test_a_denied_signal_records_nothing(running: m1.Owned) -> None:
    rt107.open_wait(running)
    before = counts(running)

    with pytest.raises(WaitPolicyDenied):
        rt107.resolve_wait(running, policy=RecordingPolicy(denied=True))

    assert counts(running) == before
    assert refused_signals(running.connection) == []


def test_an_external_signal_for_a_timer_wait_is_refused_and_not_recorded(
    running: m1.Owned,
) -> None:
    rt107.open_wait(running, kind="timer", expires_at_us=RESOLVE_US + 1_000_000)
    before = counts(running)

    with pytest.raises(WaitResolutionConflict, match="does not resolve"):
        rt107.resolve_wait(running, policy=RecordingPolicy())

    assert counts(running) == before
    assert refused_signals(running.connection) == []


def test_a_cancellation_is_not_a_signal_and_records_no_observation(
    running: m1.Owned,
) -> None:
    rt107.open_wait(running)

    rt107.resolve_wait(
        running,
        policy=RecordingPolicy(),
        command=rt107.resolution(
            resolution_kind="cancelled", reason="operator_cancelled"
        ),
    )

    telemetry = telemetry_of(running)
    assert telemetry.wait_status == "cancelled"
    assert telemetry.observation_total == 0


def test_a_signal_record_is_read_only_within_its_own_workspace(
    running: m1.Owned,
) -> None:
    rt107.open_wait(running)
    rt107.resolve_wait(running, policy=RecordingPolicy())
    observed = telemetry_of(running).last_observation
    assert observed is not None

    assert (
        read_wait_signal_telemetry(
            running.connection, workspace_id="c21-other-workspace", wait_id=WAIT_ID
        )
        is None
    )
    assert (
        read_wait_signal_observation(
            running.connection,
            workspace_id="c21-other-workspace",
            wait_id=WAIT_ID,
            wait_signal_observation_id=observed.wait_signal_observation_id,
        )
        is None
    )
    assert (
        read_wait_signal_observation(
            running.connection,
            workspace_id=WORKSPACE_ID,
            wait_id="wait-c21-other",
            wait_signal_observation_id=observed.wait_signal_observation_id,
        )
        is None
    )


def test_a_refused_signal_through_workflow_control_names_its_record(
    owned: m1.Owned,
) -> None:
    """The boundary: the refusal names the record of the signal it refused.

    The caller gets a conflict whose envelope carries the audit reference of the refused
    record, and whose message says that no workflow mutation was committed. The record reads
    back as refused, beside the accepted resolution that the wait already holds.
    """
    clock = FakeClock(wall=live.WALL)
    dispatcher, run_id = live.started(
        owned, wait_policy=lambda *args, **kwargs: None, clock=clock
    )
    claim = live.scheduler(owned).claim_next()
    assert claim is not None
    live.suspend(owned, claim)
    clock.advance_wall(3.0)
    app.result(live.resolve(dispatcher, run_id))

    # A second signal, under its own key, stating a different reason: a different signal.
    response = dispatcher.dispatch(
        app.request(
            WORKFLOW_CONTROL_OPERATION,
            {
                "run_id": run_id,
                "action": "resolve_wait",
                "wait_id": "wait-live-1",
                "resolution": "external_signal",
                "reason": "operator.again",
            },
            request_id="req-resolve-2",
            idempotency_key="idem-resolve-2",
        )
    )

    assert isinstance(response, ErrorResponseEnvelope)
    assert response.error.code == "conflict"
    assert "no workflow mutation was committed" in response.error.message
    audit_ref = response.metadata.audit_reference
    assert audit_ref is not None
    assert audit_row(owned.connection, audit_ref) == ("refused", "conflict")
    assert refused_signals(owned.connection) == [("wait_already_resolved", "refused")]


def test_a_grant_superseded_before_it_settles_neither_resolves_nor_observes(
    running: m1.Owned,
) -> None:
    """A grant issued before a takeover settles nothing and observes nothing."""
    rt107.open_wait(running)
    context, equivalence, grant = rt107.authority(running, "c21-stale-accept-0001")
    supersede(running, instance="svc-c21-takeover")
    before = counts(running)

    with pytest.raises(StaleGeneration):
        runtime_waits.resolve_runtime_wait(
            running.connection,
            running.identity,
            grant=grant,
            context=context,
            equivalence=equivalence,
            command=rt107.resolution(),
            policy=RecordingPolicy(),
            runtime_event_id="evt-c21-stale-accept",
            validate_result=s0.accept_any,
            clock=rt107.clock_at(RESOLVE_US),
            expected=RuntimeAggregateExpectation(run_id=rt107.RUN_ID, sequence=2),
        )

    assert counts(running) == before
    telemetry = telemetry_of(running)
    assert telemetry.wait_status == "pending"
    assert telemetry.observation_total == 0


# --- two workspaces, one wait ID, one idempotency key ---------------------------------

SHARED_KEY = "c21-shared-0001"
SHARED_REFUSED_KEY = "c21-shared-0002"


def grant_in(holder: m1.Owned, workspace_id: str, key: str) -> tuple[Any, Any, Any]:
    """The real authorize and grant seams, scoped to `workspace_id` rather than the default."""
    session = s0.session_for(rt104.ENTRY, workspaces=frozenset({workspace_id}))
    binding = replace(s0.BINDING, workspace_id=workspace_id)
    context = s0.authorize(
        rt104.ENTRY,
        session=session,
        binding=binding,
        workspace_id=workspace_id,
        idempotency_key=key,
    )
    equivalence = s0.idempotency_equivalence(
        rt104.ENTRY.name,
        s0.metadata_for(rt104.ENTRY, workspace_id=workspace_id, idempotency_key=key),
        dict(rt104.OPERATION_INPUT),
        principal_id=s0.PRINCIPAL,
        workspace_id=workspace_id,
    )
    grant = issue_mutation_grant(
        context,
        session=session,
        binding=binding,
        guard=read_guard(holder.connection),
        equivalence=equivalence,
        clock=rt104.clock(),
    )
    return context, equivalence, grant


def running_in(tmp_path: Path, workspace_id: str) -> m1.Owned:
    """One workspace in its own store, with its run started and its one attempt running."""
    path = tmp_path / f"{workspace_id}.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=workspace_id)
    holder = m1.take_ownership(path, workspace_id=workspace_id)
    m18.seed_job(holder, job_id=rt104.JOB_ID, workspace_id=workspace_id)
    admit_run(
        holder.connection,
        holder.identity,
        workspace_id=workspace_id,
        fencing_generation=holder.generation,
        admission=RunAdmission(
            run_id=rt104.RUN_ID,
            job_id=rt104.JOB_ID,
            claim_id=m18.claim_id_for(rt104.JOB_ID),
            definition=rt104.DEFINITION,
            logical_key=m18.logical_key_for(rt104.JOB_ID),
            originating_operation="runtime.admit",
            audit_ref=m18.audit_ref_for(rt104.JOB_ID),
            admitted_at_us=rt104.ADMITTED_US,
            runtime_event_id=rt104.ADMITTED_EVENT_ID,
            message="run admitted",
        ),
    )
    context, equivalence, grant = grant_in(holder, workspace_id, "c21-start-0001")
    execute_runtime_command(
        holder.connection,
        holder.identity,
        grant=grant,
        context=context,
        equivalence=equivalence,
        command=rt104.StartRunCommand(),
        validate_result=s0.accept_any,
        clock=rt104.clock(),
        expected=RuntimeAggregateExpectation(run_id=rt104.RUN_ID, sequence=0),
    )
    record_step_status(
        holder.connection,
        holder.identity,
        workspace_id=workspace_id,
        fencing_generation=holder.generation,
        run_step_id=rt107.STEP_ID,
        status="running",
        observed_at_us=rt107.RUNNING_US,
    )
    start_attempt(
        holder.connection,
        holder.identity,
        workspace_id=workspace_id,
        fencing_generation=holder.generation,
        attempt_id=rt107.ATTEMPT_ID,
        run_id=rt104.RUN_ID,
        run_step_id=rt107.STEP_ID,
        attempt_number=1,
        started_at_us=rt107.RUNNING_US,
    )
    return holder


def open_in(holder: m1.Owned, workspace_id: str) -> None:
    context, equivalence, grant = grant_in(holder, workspace_id, "c21-open-0001")
    open_runtime_wait(
        holder.connection,
        holder.identity,
        grant=grant,
        context=context,
        equivalence=equivalence,
        opening=WaitOpening(
            wait_id=WAIT_ID,
            run_id=rt104.RUN_ID,
            run_step_id=rt107.STEP_ID,
            kind="external_signal",
            resume_digest=DIGEST,
            expires_at_us=None,
            runtime_event_id="evt-c21-open-0001",
        ),
        validate_result=s0.accept_any,
        clock=rt107.clock_at(rt107.OPEN_US),
        expected=RuntimeAggregateExpectation(run_id=rt104.RUN_ID, sequence=1),
    )


def signal_in(
    holder: m1.Owned,
    workspace_id: str,
    *,
    key: str,
    reason: str,
    expected_sequence: int,
) -> Any:
    context, equivalence, grant = grant_in(holder, workspace_id, key)
    return resolve_runtime_wait(
        holder.connection,
        holder.identity,
        grant=grant,
        context=context,
        equivalence=equivalence,
        command=rt107.resolution(workspace_id=workspace_id, reason=reason),
        policy=lambda _context, _command, _wait: None,
        runtime_event_id=f"evt-{key}",
        validate_result=s0.accept_any,
        clock=rt107.clock_at(rt107.RESOLVE_US),
        expected=RuntimeAggregateExpectation(
            run_id=rt104.RUN_ID, sequence=expected_sequence
        ),
    )


def audit_outcome_in(
    connection: Any, workspace_id: str, audit_ref: str
) -> tuple[str, str | None]:
    row = connection.execute(
        "SELECT outcome_class, error_code FROM omnivia_application_audit_events "
        "WHERE audit_ref = ? AND workspace_id = ?",
        (audit_ref, workspace_id),
    ).fetchone()
    assert row is not None
    return (str(row[0]), None if row[1] is None else str(row[1]))


def test_two_workspaces_with_the_same_wait_and_key_never_collide_or_leak(
    tmp_path: Path,
) -> None:
    """Adversarial: identical wait IDs and idempotency keys, in two workspaces.

    Each workspace is its own store, as the storage layer keeps them, and each signal is
    accepted and then refused under the same keys. The records must not collide: each
    refusal names an audit event of its own workspace, and each observation binds to that
    event. Nothing one workspace holds is visible from the other's store.
    """
    first = running_in(tmp_path, WORKSPACE_ID)
    second = running_in(tmp_path, OTHER_WORKSPACE)
    stores = ((first, WORKSPACE_ID), (second, OTHER_WORKSPACE))
    try:
        accepted: dict[str, Any] = {}
        refused: dict[str, str] = {}
        for holder, workspace_id in stores:
            open_in(holder, workspace_id)
            signal_in(
                holder,
                workspace_id,
                key=SHARED_KEY,
                reason="signal_received",
                expected_sequence=2,
            )
            accepted_observation = telemetry_in(holder, workspace_id).last_observation
            assert accepted_observation is not None
            accepted[workspace_id] = accepted_observation
            with pytest.raises(WaitResolutionConflict) as refusal:
                signal_in(
                    holder,
                    workspace_id,
                    key=SHARED_REFUSED_KEY,
                    reason="different_signal",
                    expected_sequence=3,
                )
            assert refusal.value.audit_reference is not None
            refused[workspace_id] = refusal.value.audit_reference

        # The derived identifiers do not collide across workspaces.
        assert refused[WORKSPACE_ID] != refused[OTHER_WORKSPACE]
        assert (
            accepted[WORKSPACE_ID].wait_signal_observation_id
            != accepted[OTHER_WORKSPACE].wait_signal_observation_id
        )
        assert accepted[WORKSPACE_ID].event_id != accepted[OTHER_WORKSPACE].event_id

        for holder, workspace_id in stores:
            other = OTHER_WORKSPACE if workspace_id == WORKSPACE_ID else WORKSPACE_ID
            # Each refusal binds to its own workspace's audit event, and its observation to that.
            assert audit_outcome_in(
                holder.connection, workspace_id, refused[workspace_id]
            ) == (
                "refused",
                "conflict",
            )
            telemetry = telemetry_in(holder, workspace_id)
            assert telemetry.wait_status == "resolved"
            assert telemetry.observation_total == 2
            assert telemetry.last_observation is not None
            assert telemetry.last_observation.delivery_status == "dead_lettered"
            assert telemetry.last_observation.audit_ref == refused[workspace_id]
            assert audit_outcome_in(
                holder.connection, workspace_id, accepted[workspace_id].audit_ref
            ) == ("succeeded", None)
            # No leakage: the other workspace's records are absent from this store.
            absent = holder.connection.execute(
                "SELECT count(*) FROM omnivia_application_audit_events WHERE audit_ref = ?",
                (refused[other],),
            ).fetchone()
            assert absent is not None and absent[0] == 0
            assert (
                read_wait_signal_observation(
                    holder.connection,
                    workspace_id=workspace_id,
                    wait_id=WAIT_ID,
                    wait_signal_observation_id=accepted[
                        other
                    ].wait_signal_observation_id,
                )
                is None
            )
    finally:
        first.connection.close()
        second.connection.close()


def telemetry_in(holder: m1.Owned, workspace_id: str) -> Any:
    telemetry = read_wait_signal_telemetry(
        holder.connection, workspace_id=workspace_id, wait_id=WAIT_ID
    )
    assert telemetry is not None
    return telemetry
