"""Durable job queue acceptance (B9), extending LC-08 and LC-10.

Every operation here runs through a fenced transaction, so these also exercise the
fencing path under realistic read-then-write pressure rather than single statements.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from omnivia_core_runtime.ownership.fencing import (
    StaleGeneration,
    close_guard,
    open_guard,
)
from omnivia_core_runtime.ownership.identity import (
    FakeClock,
    ProcessEvidence,
    ServiceInstanceIdentity,
)
from omnivia_core_runtime.ownership.lease import acquire_lease
from omnivia_core_runtime.service.jobs import (
    JobError,
    JobQueue,
    JobState,
    list_jobs,
    read_job,
)
from omnivia_core_runtime.storage.connection import OpenMode, open_database
from omnivia_core_runtime.storage.migrations import (
    apply_pending_migrations,
    bootstrap_generation_one,
    materialise_phase0_baseline,
)

WORKSPACE_ID = "ws-jobs-0001"


def identity_for(instance: str, pid: int) -> ServiceInstanceIdentity:
    return ServiceInstanceIdentity(
        service_instance_id=instance,
        installation_id="inst-jobs",
        process=ProcessEvidence(
            pid=pid, start_time="100", boot_id="boot-a", os_principal="me"
        ),
    )


@pytest.fixture
def queue(tmp_path: Path):
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    clock = FakeClock()
    identity = identity_for("svc-jobs", 1111)

    state = bootstrap_generation_one(
        connection,
        workspace_id=WORKSPACE_ID,
        mode=OpenMode.EXCLUSIVE_MAINTENANCE,
        expect_phase0_baseline=True,
    )
    apply_pending_migrations(
        connection,
        mode=OpenMode.EXCLUSIVE_MAINTENANCE,
        service_instance_id=identity.service_instance_id,
        fencing_generation=state.fencing_generation,
        workspace_id=WORKSPACE_ID,
    )
    lease = acquire_lease(
        connection,
        identity,
        clock=clock,
        workspace_id=WORKSPACE_ID,
        holds_storage_lock=True,
        lock_mechanism="flock",
    )
    open_guard(
        connection,
        identity,
        clock=clock,
        workspace_id=WORKSPACE_ID,
        fencing_generation=lease.fencing_generation,
    )
    yield JobQueue(
        connection=connection,
        identity=identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=lease.fencing_generation,
        clock=clock,
    )
    connection.close()


def test_enqueue_claim_and_complete(queue: JobQueue) -> None:
    job = queue.enqueue("reindex", {"scope": "all"}, job_id="job-1")
    assert job.state == JobState.QUEUED.value
    assert job.payload == {"scope": "all"}
    assert job.attempts == 0

    claimed = queue.claim_next()
    assert claimed is not None
    assert claimed.job_id == "job-1"
    assert claimed.state == JobState.CLAIMED.value
    assert claimed.claimed_by_service_instance == "svc-jobs"

    done = queue.complete("job-1")
    assert done.state == JobState.SUCCEEDED.value
    assert done.terminal


def test_claim_returns_none_on_an_empty_queue(queue: JobQueue) -> None:
    assert queue.claim_next() is None


def test_claim_is_oldest_first_and_filterable_by_type(queue: JobQueue) -> None:
    queue.enqueue("a", job_id="job-1")
    queue.enqueue("b", job_id="job-2")
    queue.enqueue("a", job_id="job-3")

    first = queue.claim_next(job_type="b")
    assert first is not None and first.job_id == "job-2"

    second = queue.claim_next()
    assert second is not None and second.job_id == "job-1", "oldest of the remainder"


def test_a_claimed_job_is_not_claimed_twice(queue: JobQueue) -> None:
    """The conditional UPDATE is what makes this safe, not a Python-side check."""
    queue.enqueue("once", job_id="job-1")
    assert queue.claim_next() is not None
    assert queue.claim_next() is None, "no queued job remains"


def test_a_second_service_cannot_claim_under_a_stale_generation(
    queue: JobQueue,
) -> None:
    queue.enqueue("contested", job_id="job-1")

    stale = JobQueue(
        connection=queue.connection,
        identity=queue.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=queue.fencing_generation + 5,
        clock=queue.clock,
    )
    with pytest.raises(StaleGeneration):
        stale.claim_next()

    # The job is untouched and still claimable by the real owner.
    still = read_job(queue.connection, "job-1")
    assert still is not None and still.state == JobState.QUEUED.value
    assert queue.claim_next() is not None


def test_failure_requeues_until_the_attempt_budget_is_spent(queue: JobQueue) -> None:
    """A permanently failing job must stop, not cycle forever looking like progress."""
    queue.enqueue("flaky", job_id="job-1")

    for attempt in range(1, queue.max_attempts):
        claimed = queue.claim_next()
        assert claimed is not None
        requeued = queue.fail("job-1", f"boom {attempt}")
        assert requeued.state == JobState.QUEUED.value
        assert requeued.attempts == attempt
        assert requeued.last_error == f"boom {attempt}"
        assert requeued.claimed_by_service_instance is None

    final_claim = queue.claim_next()
    assert final_claim is not None
    exhausted = queue.fail("job-1", "boom final")
    assert exhausted.state == JobState.FAILED.value
    assert exhausted.attempts == queue.max_attempts
    assert exhausted.terminal
    assert queue.claim_next() is None, "an exhausted job is not requeued"


def test_cancel_works_from_queued_and_claimed_but_not_from_terminal(
    queue: JobQueue,
) -> None:
    queue.enqueue("cancel-me", job_id="job-1")
    cancelled = queue.cancel("job-1")
    assert cancelled.state == JobState.CANCELLED.value
    assert cancelled.claimed_by_service_instance is None

    queue.enqueue("cancel-claimed", job_id="job-2")
    assert queue.claim_next() is not None
    assert queue.cancel("job-2").state == JobState.CANCELLED.value

    with pytest.raises(JobError, match="expected one of"):
        queue.cancel("job-1")


def test_completing_an_unclaimed_job_is_refused(queue: JobQueue) -> None:
    queue.enqueue("not-claimed", job_id="job-1")
    with pytest.raises(JobError, match="expected one of"):
        queue.complete("job-1")


def test_duplicate_job_ids_are_refused(queue: JobQueue) -> None:
    queue.enqueue("dup", job_id="job-1")
    with pytest.raises(JobError, match="already exists"):
        queue.enqueue("dup", job_id="job-1")


def test_transitioning_an_unknown_job_is_refused(queue: JobQueue) -> None:
    with pytest.raises(JobError, match="no job"):
        queue.complete("job-absent")
    with pytest.raises(JobError, match="no job"):
        queue.fail("job-absent", "x")


def test_recover_stranded_requeues_older_generation_claims(queue: JobQueue) -> None:
    """LC-10 at the queue level."""
    queue.enqueue("stranded", job_id="job-1")
    claimed = queue.claim_next()
    assert claimed is not None

    successor = identity_for("svc-successor", 2222)
    new_lease = acquire_lease(
        queue.connection,
        successor,
        clock=queue.clock,
        workspace_id=WORKSPACE_ID,
        holds_storage_lock=True,
        lock_mechanism="flock",
    )
    open_guard(
        queue.connection,
        successor,
        clock=queue.clock,
        workspace_id=WORKSPACE_ID,
        fencing_generation=new_lease.fencing_generation,
    )
    successor_queue = JobQueue(
        connection=queue.connection,
        identity=successor,
        workspace_id=WORKSPACE_ID,
        fencing_generation=new_lease.fencing_generation,
        clock=queue.clock,
    )

    requeued = successor_queue.recover_stranded()
    assert requeued == ["job-1"]

    recovered = read_job(queue.connection, "job-1")
    assert recovered is not None
    assert recovered.state == JobState.QUEUED.value
    assert recovered.claimed_by_service_instance is None

    # And the successor can now claim it.
    taken = successor_queue.claim_next()
    assert taken is not None
    assert taken.claimed_by_service_instance == "svc-successor"


def test_recover_stranded_leaves_current_generation_claims_alone(
    queue: JobQueue,
) -> None:
    queue.enqueue("mine", job_id="job-1")
    assert queue.claim_next() is not None
    assert queue.recover_stranded() == []
    still = read_job(queue.connection, "job-1")
    assert still is not None and still.state == JobState.CLAIMED.value


def test_every_queue_operation_requires_an_open_guard(queue: JobQueue) -> None:
    """With the guard closed, no job state can change at all."""
    queue.enqueue("guarded", job_id="job-1")
    close_guard(queue.connection)

    with pytest.raises(StaleGeneration):
        queue.enqueue("blocked", job_id="job-2")
    with pytest.raises(StaleGeneration):
        queue.claim_next()
    with pytest.raises(StaleGeneration):
        queue.cancel("job-1")

    unchanged = read_job(queue.connection, "job-1")
    assert unchanged is not None and unchanged.state == JobState.QUEUED.value


def test_listing_is_deterministic_and_filterable(queue: JobQueue) -> None:
    for index in range(3):
        queue.enqueue("t", job_id=f"job-{index}")
    assert [job.job_id for job in list_jobs(queue.connection)] == [
        "job-0",
        "job-1",
        "job-2",
    ]
    assert queue.claim_next() is not None
    claimed = list_jobs(queue.connection, state=JobState.CLAIMED.value)
    assert [job.job_id for job in claimed] == ["job-0"]


def test_payload_survives_every_transition(queue: JobQueue) -> None:
    """Bookkeeping shares the payload column, so the payload must not be lost."""
    payload = {"unicode": "☃", "nested": {"b": 2, "a": 1}, "empty": ""}
    queue.enqueue("payload", payload, job_id="job-1")
    assert queue.claim_next() is not None
    requeued = queue.fail("job-1", "retry once")
    assert requeued.payload == payload
    assert queue.claim_next() is not None
    assert queue.complete("job-1").payload == payload


def test_job_states_declare_which_are_terminal() -> None:
    assert not JobState.QUEUED.terminal
    assert not JobState.CLAIMED.terminal
    assert JobState.SUCCEEDED.terminal
    assert JobState.FAILED.terminal
    assert JobState.CANCELLED.terminal


def test_a_failed_transaction_leaves_no_partial_job_state(queue: JobQueue) -> None:
    """The fenced transaction rolls the whole operation back."""
    queue.enqueue("atomic", job_id="job-1")
    before = read_job(queue.connection, "job-1")
    assert before is not None

    stale = JobQueue(
        connection=queue.connection,
        identity=queue.identity,
        workspace_id="ws-wrong",
        fencing_generation=queue.fencing_generation,
        clock=queue.clock,
    )
    with pytest.raises(StaleGeneration):
        stale.enqueue("never", job_id="job-2")

    assert read_job(queue.connection, "job-2") is None
    after = read_job(queue.connection, "job-1")
    assert after == before


def test_the_queue_never_opens_its_own_connection() -> None:
    """Storage authority stays with the service; the queue is handed a connection."""
    import omnivia_core_runtime.service.jobs as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "open_database" not in source
    assert "sqlite3.connect" not in source


def test_a_stranded_job_from_an_unknown_generation_is_recovered(
    queue: JobQueue,
) -> None:
    """A NULL generation counts as older, so legacy rows are not left claimed."""
    queue.enqueue("null-gen", job_id="job-1")
    assert queue.claim_next() is not None
    queue.connection.execute(
        "UPDATE omnivia_durable_jobs SET fencing_generation = NULL WHERE job_id = ?",
        ("job-1",),
    )
    assert queue.recover_stranded() == ["job-1"]


def test_max_attempts_is_configurable(queue: JobQueue) -> None:
    once = JobQueue(
        connection=queue.connection,
        identity=queue.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=queue.fencing_generation,
        clock=queue.clock,
        max_attempts=1,
    )
    once.enqueue("single", job_id="job-1")
    assert once.claim_next() is not None
    assert once.fail("job-1", "no retries").state == JobState.FAILED.value


def test_connection_type_is_unchanged_by_the_queue(queue: JobQueue) -> None:
    assert isinstance(queue.connection, sqlite3.Connection)
