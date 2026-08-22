"""RT-107 acceptance for durable Wait opening and ``ResolveWait``.

The tests use RT-104's real authorization, grant, idempotency and settlement seam.
They therefore prove the wait/state/event rows commit with the application command,
not merely that the three storage calls work when issued separately.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
import test_rt104_runtime_command_transaction as rt104
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.service.mutation import MutationDenied, MutationSeamFault
from omnivia_core_runtime.service.runtime_command import RuntimeAggregateExpectation
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
    read_run,
    record_step_status,
    start_attempt,
)
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline

from omnivia_core.contracts.v1 import Approval, ResolveWait

WORKSPACE_ID = rt104.WORKSPACE_ID
RUN_ID = rt104.RUN_ID
STEP_ID = rt104.STEP_ID
ATTEMPT_ID = "att-rt107-0001"
WAIT_ID = "wait-rt107-0001"
DIGEST = m18.DIGEST

RUNNING_US = rt104.SETTLED_US + 1_000
OPEN_US = RUNNING_US + 1_000
RESOLVE_US = OPEN_US + 1_000

RUNTIME_TABLES = (
    "omnivia_runtime_waits",
    "omnivia_runtime_wait_resolutions",
    "omnivia_runtime_run_step_states",
    "omnivia_runtime_events",
)


def timestamp(value: int) -> str:
    moment = datetime.fromtimestamp(value / 1_000_000, tz=UTC)
    milliseconds = moment.microsecond // 1_000
    if milliseconds == 0:
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{milliseconds:03d}Z"


def clock_at(value: int) -> FakeClock:
    return FakeClock(
        monotonic=s0.MONOTONIC_BASE,
        wall=datetime.fromtimestamp(value / 1_000_000, tz=UTC),
    )


def runtime_counts(holder: m1.Owned) -> dict[str, int]:
    return {table: m1.count(holder.connection, table) for table in RUNTIME_TABLES}


@pytest.fixture
def admitted(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    holder = m1.take_ownership(path)
    m18.seed_job(holder, job_id=rt104.JOB_ID)
    admit_run(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        admission=RunAdmission(
            run_id=RUN_ID,
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
    yield holder
    holder.connection.close()


@pytest.fixture
def running(admitted: m1.Owned) -> m1.Owned:
    """One run with one step and one active attempt, ready to suspend."""
    rt104.run_command(admitted, command=rt104.StartRunCommand())
    record_step_status(
        admitted.connection,
        admitted.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=admitted.generation,
        run_step_id=STEP_ID,
        status="running",
        observed_at_us=RUNNING_US,
    )
    start_attempt(
        admitted.connection,
        admitted.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=admitted.generation,
        attempt_id=ATTEMPT_ID,
        run_id=RUN_ID,
        run_step_id=STEP_ID,
        attempt_number=1,
        started_at_us=RUNNING_US,
    )
    return admitted


class RecordingPolicy:
    """A visible fail-closed policy seam for the assertions below."""

    def __init__(
        self, *, approval: Approval | None = None, denied: bool = False
    ) -> None:
        self.approval = approval
        self.denied = denied
        self.calls = 0
        self.waits: list[str] = []

    def __call__(
        self, context: Any, command: ResolveWait, wait: Any
    ) -> Approval | None:
        self.calls += 1
        self.waits.append(wait.wait_id)
        assert context.workspace_id == WORKSPACE_ID
        assert command.wait_id == wait.wait_id
        if self.denied:
            raise WaitPolicyDenied("the test policy denied this wait resolution")
        return self.approval


def authority(holder: m1.Owned, key: str) -> tuple[Any, Any, Any]:
    context = rt104.authorize(idempotency_key=key)
    equivalence = rt104.equivalence_for(idempotency_key=key)
    grant = rt104.issue(holder, context, equivalence=equivalence)
    return context, equivalence, grant


def open_wait(
    holder: m1.Owned,
    *,
    key: str = "rt107-open-0001",
    kind: str = "external_signal",
    expires_at_us: int | None = None,
    validate_result: Any = s0.accept_any,
    expected: RuntimeAggregateExpectation | None = None,
) -> Any:
    context, equivalence, grant = authority(holder, key)
    return open_runtime_wait(
        holder.connection,
        holder.identity,
        grant=grant,
        context=context,
        equivalence=equivalence,
        opening=WaitOpening(
            wait_id=WAIT_ID,
            run_id=RUN_ID,
            run_step_id=STEP_ID,
            kind=kind,
            resume_digest=DIGEST,
            expires_at_us=expires_at_us,
            runtime_event_id=f"evt-{key}",
        ),
        validate_result=validate_result,
        clock=clock_at(OPEN_US),
        expected=(
            RuntimeAggregateExpectation(run_id=RUN_ID, sequence=1)
            if expected is None
            else expected
        ),
    )


def resolution(
    *,
    resolution_kind: str = "external_signal",
    reason: str = "signal_received",
    approval_id: str | None = None,
    resume_digest: str = DIGEST,
    workspace_id: str = WORKSPACE_ID,
    run_id: str = RUN_ID,
    wait_id: str = WAIT_ID,
    requested_at_us: int = RESOLVE_US,
) -> ResolveWait:
    return ResolveWait(
        workspace_id=workspace_id,
        run_id=run_id,
        wait_id=wait_id,
        resolution=resolution_kind,
        approval_id=approval_id,
        resume_digest=resume_digest,
        requested_at=timestamp(requested_at_us),
        reason=reason,
    )


def resolve_wait(
    holder: m1.Owned,
    *,
    policy: RecordingPolicy,
    command: ResolveWait | None = None,
    key: str = "rt107-resolve-0001",
    settled_at_us: int = RESOLVE_US,
    expected_sequence: int = 2,
    validate_result: Any = s0.accept_any,
) -> Any:
    context, equivalence, grant = authority(holder, key)
    resolved = resolution() if command is None else command
    return resolve_runtime_wait(
        holder.connection,
        holder.identity,
        grant=grant,
        context=context,
        equivalence=equivalence,
        command=resolved,
        policy=policy,
        runtime_event_id=f"evt-{key}",
        validate_result=validate_result,
        clock=clock_at(settled_at_us),
        expected=RuntimeAggregateExpectation(
            run_id=resolved.run_id, sequence=expected_sequence
        ),
    )


def decided_approval() -> Approval:
    return Approval(
        workspace_id=WORKSPACE_ID,
        approval_id="apr-rt107-0001",
        run_id=RUN_ID,
        wait_id=WAIT_ID,
        requested_at=timestamp(OPEN_US),
        approver_role="runtime_approver",
        decision="approved",
        decided_at=timestamp(RESOLVE_US),
        decided_by="principal-approver",
        audit_reference=m18.audit_ref_for(rt104.JOB_ID),
    )


def test_open_wait_commits_wait_step_and_event_together(running: m1.Owned) -> None:
    outcome = open_wait(running)

    assert outcome.result == {
        "wait_id": WAIT_ID,
        "run_id": RUN_ID,
        "run_step_id": STEP_ID,
        "status": "pending",
        "sequence": 2,
    }
    snapshot = read_run(running.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert snapshot is not None
    assert snapshot.status == "waiting"
    assert snapshot.steps[0].status == "waiting"
    assert snapshot.steps[0].wait_id == WAIT_ID
    assert snapshot.steps[0].attempts[-1].attempt_id == ATTEMPT_ID
    assert snapshot.steps[0].attempts[-1].status == "running"
    assert snapshot.waits[0].status == "pending"
    assert [event.sequence for event in snapshot.events] == [0, 1, 2]


def test_open_wait_requires_the_step_and_attempt_to_be_running(
    admitted: m1.Owned,
) -> None:
    rt104.run_command(admitted, command=rt104.StartRunCommand())
    before = rt104.counts(admitted.connection)

    with pytest.raises(WaitResolutionConflict, match="not 'running'"):
        open_wait(admitted)

    assert rt104.counts(admitted.connection) == before


def test_open_result_refusal_rolls_back_wait_step_and_event(
    running: m1.Owned,
) -> None:
    before = rt104.counts(running.connection)

    with pytest.raises(MutationSeamFault):
        open_wait(running, validate_result=lambda _result: False)

    assert rt104.counts(running.connection) == before
    snapshot = read_run(running.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert snapshot is not None
    assert snapshot.status == "running"
    assert snapshot.steps[0].status == "running"
    assert snapshot.waits == ()


@pytest.mark.parametrize(
    ("resolution_kind", "reason", "status"),
    (
        ("external_signal", "signal_received", "resolved"),
        ("cancelled", "cancelled", "cancelled"),
    ),
)
def test_resolve_wait_resumes_the_same_attempt_without_requeueing(
    running: m1.Owned,
    resolution_kind: str,
    reason: str,
    status: str,
) -> None:
    open_wait(running)
    before_jobs = running.connection.execute(
        "SELECT job_id, state, claimed_by_service_instance, fencing_generation "
        "FROM omnivia_durable_jobs"
    ).fetchall()
    policy = RecordingPolicy()

    outcome = resolve_wait(
        running,
        policy=policy,
        command=resolution(resolution_kind=resolution_kind, reason=reason),
    )

    assert outcome.result["status"] == status
    assert policy.calls == 1 and policy.waits == [WAIT_ID]
    snapshot = read_run(running.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert snapshot is not None
    assert snapshot.status == "running"
    assert snapshot.steps[0].status == "running"
    assert snapshot.steps[0].wait_id is None
    assert [attempt.attempt_id for attempt in snapshot.steps[0].attempts] == [
        ATTEMPT_ID
    ]
    assert snapshot.waits[0].status == status
    assert (
        running.connection.execute(
            "SELECT job_id, state, claimed_by_service_instance, fencing_generation "
            "FROM omnivia_durable_jobs"
        ).fetchall()
        == before_jobs
    )


def test_approval_resolution_uses_the_recorded_policy_decision(
    running: m1.Owned,
) -> None:
    open_wait(running, kind="approval")
    approval = decided_approval()
    policy = RecordingPolicy(approval=approval)

    outcome = resolve_wait(
        running,
        policy=policy,
        command=resolution(
            resolution_kind="approval_decision",
            reason="approved",
            approval_id=approval.approval_id,
        ),
    )

    assert outcome.result["approval_id"] == approval.approval_id
    snapshot = read_run(running.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert snapshot is not None
    assert snapshot.waits[0].approval_id == approval.approval_id


def test_policy_cannot_substitute_an_approval_from_another_wait(
    running: m1.Owned,
) -> None:
    open_wait(running, kind="approval")
    approval = decided_approval()
    substituted = replace(approval, wait_id="wait-rt107-other")
    before = rt104.counts(running.connection)

    with pytest.raises(WaitResolutionConflict, match="approval.wait_id"):
        resolve_wait(
            running,
            policy=RecordingPolicy(approval=substituted),
            command=resolution(
                resolution_kind="approval_decision",
                reason="approved",
                approval_id=approval.approval_id,
            ),
        )

    assert rt104.counts(running.connection) == before


def test_timer_cannot_expire_early_and_expires_at_its_deadline(
    running: m1.Owned,
) -> None:
    deadline = RESOLVE_US + 1_000
    open_wait(running, kind="timer", expires_at_us=deadline)
    before = rt104.counts(running.connection)
    early_policy = RecordingPolicy()

    with pytest.raises(WaitResolutionConflict, match="not reached its deadline"):
        resolve_wait(
            running,
            policy=early_policy,
            command=resolution(
                resolution_kind="timer_expiry", reason="deadline_elapsed"
            ),
        )

    assert early_policy.calls == 0
    assert rt104.counts(running.connection) == before

    outcome = resolve_wait(
        running,
        policy=RecordingPolicy(),
        command=resolution(
            resolution_kind="timer_expiry",
            reason="deadline_elapsed",
            requested_at_us=deadline,
        ),
        key="rt107-resolve-deadline",
        settled_at_us=deadline,
    )
    assert outcome.result["status"] == "expired"


def test_non_timer_resolution_after_deadline_is_expired_not_resolved(
    running: m1.Owned,
) -> None:
    deadline = RESOLVE_US
    open_wait(running, expires_at_us=deadline)
    policy = RecordingPolicy()
    before = rt104.counts(running.connection)

    with pytest.raises(WaitResolutionConflict, match="has expired"):
        resolve_wait(
            running,
            policy=policy,
            settled_at_us=deadline + 10_000,
        )

    assert policy.calls == 0
    assert rt104.counts(running.connection) == before


def test_stale_digest_is_rejected_before_policy(running: m1.Owned) -> None:
    open_wait(running)
    policy = RecordingPolicy()
    before = rt104.counts(running.connection)

    with pytest.raises(WaitResolutionConflict, match="resume_digest"):
        resolve_wait(
            running,
            policy=policy,
            command=resolution(resume_digest="sha256:" + "f" * 64),
        )

    assert policy.calls == 0
    assert rt104.counts(running.connection) == before


def test_resolution_kind_mismatch_is_rejected_before_policy(
    running: m1.Owned,
) -> None:
    open_wait(running, kind="approval")
    policy = RecordingPolicy()
    before = rt104.counts(running.connection)

    with pytest.raises(WaitResolutionConflict, match="does not resolve"):
        resolve_wait(running, policy=policy)

    assert policy.calls == 0
    assert rt104.counts(running.connection) == before


def test_policy_denial_rolls_back_without_resolution(running: m1.Owned) -> None:
    open_wait(running)
    policy = RecordingPolicy(denied=True)
    before = rt104.counts(running.connection)

    with pytest.raises(WaitPolicyDenied):
        resolve_wait(running, policy=policy)

    assert policy.calls == 1
    assert rt104.counts(running.connection) == before


def test_same_key_replays_without_policy_or_second_runtime_write(
    running: m1.Owned,
) -> None:
    open_wait(running)
    policy = RecordingPolicy()
    first = resolve_wait(running, policy=policy)
    settled_runtime = runtime_counts(running)

    second = resolve_wait(running, policy=policy)

    assert first.replayed is False and second.replayed is True
    assert second.result == first.result
    assert policy.calls == 1
    assert runtime_counts(running) == settled_runtime


def test_identical_second_key_returns_the_accepted_resolution(
    running: m1.Owned,
) -> None:
    open_wait(running)
    first = resolve_wait(running, policy=RecordingPolicy())
    settled_runtime = runtime_counts(running)
    second_policy = RecordingPolicy()

    second = resolve_wait(
        running,
        policy=second_policy,
        key="rt107-resolve-second-key",
        expected_sequence=3,
    )

    assert second.replayed is False
    assert second.result == first.result
    assert second_policy.calls == 1
    assert runtime_counts(running) == settled_runtime


def test_conflicting_second_key_is_rejected_before_policy(running: m1.Owned) -> None:
    open_wait(running)
    resolve_wait(running, policy=RecordingPolicy())
    before = rt104.counts(running.connection)
    policy = RecordingPolicy()

    with pytest.raises(WaitResolutionConflict, match="resolved exactly once"):
        resolve_wait(
            running,
            policy=policy,
            command=resolution(reason="different_signal"),
            key="rt107-resolve-conflict",
            expected_sequence=3,
        )

    assert policy.calls == 0
    assert rt104.counts(running.connection) == before


def test_result_refusal_rolls_back_resolution_state_and_event(
    running: m1.Owned,
) -> None:
    open_wait(running)
    before = rt104.counts(running.connection)
    policy = RecordingPolicy()

    with pytest.raises(MutationSeamFault):
        resolve_wait(
            running,
            policy=policy,
            validate_result=lambda _result: False,
        )

    assert policy.calls == 1
    assert rt104.counts(running.connection) == before
    snapshot = read_run(running.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert snapshot is not None
    assert snapshot.status == "waiting" and snapshot.waits[0].status == "pending"


def test_workspace_and_run_mismatches_are_typed_and_leave_no_writes(
    running: m1.Owned,
) -> None:
    open_wait(running)
    before = rt104.counts(running.connection)

    with pytest.raises(MutationDenied):
        resolve_wait(
            running,
            policy=RecordingPolicy(),
            command=resolution(workspace_id=m18.OTHER_WORKSPACE_ID),
        )

    missing = "run-rt107-missing"
    context, equivalence, grant = authority(running, "rt107-resolve-missing-run")
    with pytest.raises(WaitNotFound):
        resolve_runtime_wait(
            running.connection,
            running.identity,
            grant=grant,
            context=context,
            equivalence=equivalence,
            command=resolution(run_id=missing),
            policy=RecordingPolicy(),
            runtime_event_id="evt-rt107-resolve-missing-run",
            validate_result=s0.accept_any,
            clock=clock_at(RESOLVE_US),
            expected=RuntimeAggregateExpectation(run_id=missing, sequence=-1),
        )

    assert rt104.counts(running.connection) == before


def test_expectation_must_name_the_run_the_wait_command_changes(
    running: m1.Owned,
) -> None:
    before = rt104.counts(running.connection)
    with pytest.raises(WaitResolutionConflict, match="aggregate expectation"):
        open_wait(
            running,
            expected=RuntimeAggregateExpectation(run_id="run-rt107-other", sequence=-1),
        )
    assert rt104.counts(running.connection) == before
