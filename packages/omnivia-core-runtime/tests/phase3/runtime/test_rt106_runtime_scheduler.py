"""RT-106 acceptance for the fenced runtime scheduler adapter."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
import test_rt102_agent_runtime_repository as rt102
from omnivia_core_runtime.ownership.fencing import StaleGeneration
from omnivia_core_runtime.ownership.lease import acquire_lease
from omnivia_core_runtime.service.runtime_scheduler import (
    RuntimeScheduler,
    RuntimeSchedulingError,
)
from omnivia_core_runtime.storage.agent_runtime import (
    RuntimeWriter,
    admit_run,
    append_run_step,
)
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline

WORKSPACE_ID = m18.WORKSPACE_ID
BASE_US = m18.BASE_US


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


def _seed_run(
    holder: m1.Owned,
    *,
    job_id: str,
    run_id: str,
    step_id: str,
    ordinal: int = 1,
    max_attempts: int = 8,
) -> None:
    audit_ref = m18.audit_ref_for(job_id)
    with m18.guarded(holder):
        holder.connection.execute(
            "INSERT OR IGNORE INTO omnivia_application_audit_events "
            "(audit_ref, workspace_id, principal_id, operation, purpose, request_id, "
            "correlation_id, trace_id, granted_authority_json, outcome_class, "
            "error_code, recorded_at_us) VALUES "
            "(?, ?, 'core-service', 'runtime.admit', 'runtime.execute', ?, ?, ?, "
            "'{}', 'succeeded', NULL, ?)",
            (
                audit_ref,
                WORKSPACE_ID,
                f"req-{job_id}",
                f"cor-{job_id}",
                f"trc-{job_id}",
                BASE_US,
            ),
        )
        holder.connection.execute(
            "INSERT INTO omnivia_durable_jobs "
            "(job_id, job_type, state, payload_json, created_at, updated_at, "
            "fencing_generation, claimed_by_service_instance) "
            "VALUES (?, 'ingestion.import', 'queued', '{}', ?, ?, ?, ?)",
            (
                job_id,
                "2039-09-18T23:06:40Z",
                "2039-09-18T23:06:40Z",
                holder.generation,
                holder.identity.service_instance_id,
            ),
        )
        holder.connection.execute(
            "INSERT INTO omnivia_job_application_metadata "
            "(workspace_id, job_id, job_kind, originating_operation, audit_ref, "
            "created_at_us, terminal_result_kind, supports_checkpoint_resume, "
            "max_attempts) VALUES (?, ?, 'ingestion.import', 'runtime.admit', ?, ?, "
            "NULL, 1, ?)",
            (WORKSPACE_ID, job_id, audit_ref, BASE_US, max_attempts),
        )
        m18.insert_claim(
            holder,
            claim_id=m18.claim_id_for(job_id),
            audit_ref=audit_ref,
            idempotency_key=m18.logical_key_for(job_id),
            workspace_id=WORKSPACE_ID,
        )
    admit_run(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        admission=rt102.admission(
            run_id=run_id,
            job_id=job_id,
            event_id=f"evt-{run_id}",
        ),
    )
    append_run_step(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        run_id=run_id,
        run_step_id=step_id,
        ordinal=ordinal,
        step_kind="plan",
        created_at_us=BASE_US,
    )


def _scheduler(holder: m1.Owned) -> RuntimeScheduler:
    return RuntimeScheduler(
        holder.connection,
        holder.identity,
        WORKSPACE_ID,
        holder.generation,
        m1.FakeClock(wall=datetime.fromtimestamp((BASE_US + 1_000) / 1_000_000, UTC)),
    )


def _takeover(holder: m1.Owned) -> m1.Owned:
    successor = m1.make_identity(instance="svc-rt106-successor", pid=6106)
    clock = m1.FakeClock(
        wall=datetime.fromtimestamp((BASE_US + 1_000) / 1_000_000, UTC)
    )
    lease = acquire_lease(
        holder.connection,
        successor,
        clock=clock,
        workspace_id=WORKSPACE_ID,
        holds_storage_lock=True,
        lock_mechanism="flock",
        predecessor=holder.identity.service_instance_id,
    )
    m1.open_guard(
        holder.connection,
        successor,
        clock=clock,
        workspace_id=WORKSPACE_ID,
        fencing_generation=lease.fencing_generation,
    )
    return m1.Owned(
        connection=holder.connection,
        identity=successor,
        generation=lease.fencing_generation,
        path=holder.path,
    )


def test_claim_selects_oldest_job_then_first_persisted_step(
    owned: m1.Owned,
) -> None:
    _seed_run(
        owned,
        job_id="job-z-later",
        run_id="run-z-later",
        step_id="step-z-later",
    )
    _seed_run(
        owned,
        job_id="job-a-earlier",
        run_id="run-a-earlier",
        step_id="step-a-earlier",
    )

    claim = _scheduler(owned).claim_next()

    assert claim is not None
    assert (claim.job_id, claim.run_id, claim.run_step_id) == (
        "job-a-earlier",
        "run-a-earlier",
        "step-a-earlier",
    )


def test_claim_commits_job_and_runtime_lineage_atomically(owned: m1.Owned) -> None:
    _seed_run(
        owned,
        job_id="job-atomic",
        run_id="run-atomic",
        step_id="step-atomic",
    )

    claim = _scheduler(owned).claim_next()

    assert claim is not None
    assert owned.connection.execute(
        "SELECT state, claimed_by_service_instance, fencing_generation "
        "FROM omnivia_durable_jobs WHERE job_id = ?",
        (claim.job_id,),
    ).fetchone() == (
        "claimed",
        owned.identity.service_instance_id,
        owned.generation,
    )
    assert owned.connection.execute(
        "SELECT attempt_number, state FROM omnivia_job_attempts "
        "WHERE workspace_id = ? AND job_id = ?",
        (WORKSPACE_ID, claim.job_id),
    ).fetchall() == [(1, "running")]
    assert owned.connection.execute(
        "SELECT state_sequence, status FROM omnivia_runtime_run_step_states "
        "WHERE workspace_id = ? AND run_step_id = ? ORDER BY state_sequence",
        (WORKSPACE_ID, claim.run_step_id),
    ).fetchall() == [(0, "pending"), (1, "running")]
    assert owned.connection.execute(
        "SELECT attempt_id, attempt_number FROM omnivia_runtime_attempts "
        "WHERE workspace_id = ? AND run_step_id = ?",
        (WORKSPACE_ID, claim.run_step_id),
    ).fetchall() == [(claim.runtime_attempt_id, 1)]
    event = owned.connection.execute(
        "SELECT event_kind, run_status, details_json FROM omnivia_runtime_events "
        "WHERE workspace_id = ? AND run_id = ? ORDER BY sequence DESC LIMIT 1",
        (WORKSPACE_ID, claim.run_id),
    ).fetchone()
    assert event is not None and event[:2] == ("attempt_started", "running")
    details = json.loads(str(event[2]))
    assert details == {
        "application_attempt_number": 1,
        "fencing_generation": owned.generation,
        "job_id": claim.job_id,
        "run_id": claim.run_id,
        "run_step_id": claim.run_step_id,
        "runtime_attempt_id": claim.runtime_attempt_id,
        "runtime_attempt_number": 1,
        "service_instance_id": owned.identity.service_instance_id,
        "workspace_id": WORKSPACE_ID,
    }


def test_stale_owner_cannot_claim_after_takeover(owned: m1.Owned) -> None:
    _seed_run(
        owned,
        job_id="job-stale",
        run_id="run-stale",
        step_id="step-stale",
    )
    stale = _scheduler(owned)
    successor = _takeover(owned)

    with pytest.raises(StaleGeneration):
        stale.claim_next()

    claim = _scheduler(successor).claim_next()
    assert claim is not None
    assert claim.service_instance_id == successor.identity.service_instance_id
    assert claim.fencing_generation == successor.generation


def test_runtime_failure_rolls_back_underlying_job_claim(
    owned: m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_run(
        owned,
        job_id="job-rollback",
        run_id="run-rollback",
        step_id="step-rollback",
    )

    def fail_start(_writer: RuntimeWriter, **_values: object) -> None:
        raise RuntimeError("injected runtime write failure")

    monkeypatch.setattr(RuntimeWriter, "start_attempt", fail_start)
    with pytest.raises(RuntimeError, match="injected runtime write failure"):
        _scheduler(owned).claim_next()

    assert owned.connection.execute(
        "SELECT state, claimed_by_service_instance FROM omnivia_durable_jobs "
        "WHERE job_id = 'job-rollback'"
    ).fetchone() == ("queued", owned.identity.service_instance_id)
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_job_attempts WHERE job_id = 'job-rollback'"
    ).fetchone() == (0,)
    assert owned.connection.execute(
        "SELECT state_sequence, status FROM omnivia_runtime_run_step_states "
        "WHERE run_step_id = 'step-rollback'"
    ).fetchall() == [(0, "pending")]


def test_no_runnable_runtime_job_is_an_empty_poll(owned: m1.Owned) -> None:
    assert _scheduler(owned).claim_next() is None


def test_exact_claim_completes_job_attempt_step_and_run(owned: m1.Owned) -> None:
    _seed_run(
        owned,
        job_id="job-complete",
        run_id="run-complete",
        step_id="step-complete",
    )
    scheduler = _scheduler(owned)
    claim = scheduler.claim_next()
    assert claim is not None

    scheduler.complete(
        claim,
        result_kind="runtime_completion",
        result={"outcome": "complete"},
    )

    assert owned.connection.execute(
        "SELECT state, claimed_by_service_instance FROM omnivia_durable_jobs "
        "WHERE job_id = ?",
        (claim.job_id,),
    ).fetchone() == ("succeeded", None)
    assert owned.connection.execute(
        "SELECT state FROM omnivia_job_attempts WHERE job_id = ?",
        (claim.job_id,),
    ).fetchone() == ("succeeded",)
    assert owned.connection.execute(
        "SELECT status FROM omnivia_runtime_attempt_outcomes "
        "WHERE attempt_id = ?",
        (claim.runtime_attempt_id,),
    ).fetchone() == ("succeeded",)
    assert owned.connection.execute(
        "SELECT status FROM omnivia_runtime_run_step_states "
        "WHERE run_step_id = ? ORDER BY state_sequence DESC LIMIT 1",
        (claim.run_step_id,),
    ).fetchone() == ("succeeded",)
    assert owned.connection.execute(
        "SELECT run_status FROM omnivia_runtime_events WHERE run_id = ? "
        "ORDER BY sequence DESC LIMIT 1",
        (claim.run_id,),
    ).fetchone() == ("succeeded",)


def test_mismatched_or_stale_claim_cannot_complete(owned: m1.Owned) -> None:
    _seed_run(
        owned,
        job_id="job-no-stale-complete",
        run_id="run-no-stale-complete",
        step_id="step-no-stale-complete",
    )
    stale_scheduler = _scheduler(owned)
    claim = stale_scheduler.claim_next()
    assert claim is not None

    with pytest.raises(RuntimeSchedulingError):
        stale_scheduler.complete(
            replace(claim, runtime_attempt_id="attempt-mismatch"),
            result_kind="runtime_completion",
            result={"outcome": "wrong"},
        )
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_runtime_attempt_outcomes"
    ).fetchone() == (0,)

    successor = _takeover(owned)
    with pytest.raises(StaleGeneration):
        stale_scheduler.complete(
            claim,
            result_kind="runtime_completion",
            result={"outcome": "stale"},
        )
    with pytest.raises(RuntimeSchedulingError):
        _scheduler(successor).complete(
            claim,
            result_kind="runtime_completion",
            result={"outcome": "wrong-owner"},
        )
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_runtime_attempt_outcomes"
    ).fetchone() == (0,)


def test_recovery_fails_open_attempt_once_then_requeues(owned: m1.Owned) -> None:
    _seed_run(
        owned,
        job_id="job-recover",
        run_id="run-recover",
        step_id="step-recover",
    )
    claim = _scheduler(owned).claim_next()
    assert claim is not None
    successor = _takeover(owned)
    scheduler = _scheduler(successor)

    recovered = scheduler.recover_stranded()

    assert len(recovered) == 1
    assert recovered[0].runtime_attempt_id == claim.runtime_attempt_id
    assert recovered[0].requeued is True
    assert scheduler.recover_stranded() == ()
    assert owned.connection.execute(
        "SELECT state FROM omnivia_durable_jobs WHERE job_id = ?", (claim.job_id,)
    ).fetchone() == ("queued",)
    assert owned.connection.execute(
        "SELECT status FROM omnivia_runtime_attempt_outcomes WHERE attempt_id = ?",
        (claim.runtime_attempt_id,),
    ).fetchone() == ("failed",)
    assert owned.connection.execute(
        "SELECT status FROM omnivia_runtime_run_step_states "
        "WHERE run_step_id = ? ORDER BY state_sequence DESC LIMIT 1",
        (claim.run_step_id,),
    ).fetchone() == ("pending",)

    retry = scheduler.claim_next()
    assert retry is not None
    assert retry.application_attempt_number == 2
    assert retry.runtime_attempt_number == 2


def test_recovery_stops_when_persisted_attempt_budget_is_exhausted(
    owned: m1.Owned,
) -> None:
    _seed_run(
        owned,
        job_id="job-exhausted",
        run_id="run-exhausted",
        step_id="step-exhausted",
        max_attempts=1,
    )
    claim = _scheduler(owned).claim_next()
    assert claim is not None
    successor = _takeover(owned)

    recovered = _scheduler(successor).recover_stranded()

    assert len(recovered) == 1 and recovered[0].requeued is False
    assert owned.connection.execute(
        "SELECT state FROM omnivia_durable_jobs WHERE job_id = ?", (claim.job_id,)
    ).fetchone() == ("failed",)
    assert owned.connection.execute(
        "SELECT status FROM omnivia_runtime_run_step_states "
        "WHERE run_step_id = ? ORDER BY state_sequence DESC LIMIT 1",
        (claim.run_step_id,),
    ).fetchone() == ("failed",)
    assert owned.connection.execute(
        "SELECT run_status FROM omnivia_runtime_events WHERE run_id = ? "
        "ORDER BY sequence DESC LIMIT 1",
        (claim.run_id,),
    ).fetchone() == ("failed",)
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_application_job_controls WHERE job_id = ?",
        (claim.job_id,),
    ).fetchone() == (0,)
