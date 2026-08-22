"""RT-102 acceptance for the canonical Agent Runtime repositories.

`storage/agent_runtime.py` is persistence and nothing above it. These tests hold it
to three properties the migration alone cannot state.

*It enters its own fenced transaction.* Every write is called on a bare owned
connection here -- no surrounding transaction, no mutation seam -- and either commits
under current authority or leaves the database exactly as it found it.

*It returns contract records where it can, and says so where it cannot.* `RunStep`,
`Attempt`, `Wait` and `RuntimeEvent` come back as the generated types. A whole `Run`
does not, because the accepted aggregate requires a policy snapshot, a budget
snapshot, grants, artifacts, evidence and cleanup receipts whose stores are RT-103's
and RT-202's; `read_run` returns a `RunSnapshot` rather than six invented fields.
What it does return is held to `semantics_runtime`'s own validators, so "honest" is
checked against the accepted contract rather than asserted here.

*It reads a workspace, never a record's claim about one.* Every read takes the
caller's workspace and filters on it, so a run in another workspace is invisible
rather than merely unlikely to be asked for.

No public wire surface is touched: the contract version is asserted unchanged.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
from omnivia_core_runtime.ownership.fencing import StaleGeneration
from omnivia_core_runtime.storage.agent_runtime import (
    RunAdmission,
    RunSnapshot,
    admit_run,
    append_run_event,
    append_run_step,
    close_wait,
    finish_attempt,
    open_wait,
    read_run,
    read_run_events,
    read_run_id_by_job,
    read_run_id_by_logical_key,
    read_run_steps,
    read_run_waits,
    read_workspace_run_ids,
    record_step_status,
    runtime_writer,
    start_attempt,
)
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    ApiError,
    RunDefinitionRef,
    validate_attempt_history,
    validate_run_step,
    validate_runtime_event_stream,
    validate_wait,
)

WORKSPACE_ID = m18.WORKSPACE_ID
OTHER_WORKSPACE_ID = m18.OTHER_WORKSPACE_ID
BASE_US = m18.BASE_US
JOB_ID = m18.JOB_ID
RUN_ID = m18.RUN_ID
STEP_ID = m18.STEP_ID
DIGEST = m18.DIGEST

#: One millisecond. `_timestamp` renders millisecond precision, exactly as every
#: other repository in this package does, so a test that wants two distinguishable
#: instants has to move by at least one.
MS = 1_000

DEFINITION = RunDefinitionRef(
    definition_kind="agent_component",
    definition_id="component.triage",
    definition_version="1.4.0",
)


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


def admission(
    *,
    run_id: str = RUN_ID,
    job_id: str = JOB_ID,
    logical_key: str | None = None,
    claim_id: str | None = None,
    audit_ref: str | None = None,
    event_id: str = "evt-0001",
) -> RunAdmission:
    """The admission `m18.seed_job` has already recorded the claim and metadata for."""
    return RunAdmission(
        run_id=run_id,
        job_id=job_id,
        claim_id=claim_id or m18.claim_id_for(job_id),
        definition=DEFINITION,
        logical_key=logical_key or m18.logical_key_for(job_id),
        originating_operation="runtime.admit",
        audit_ref=audit_ref or m18.audit_ref_for(job_id),
        admitted_at_us=BASE_US,
        runtime_event_id=event_id,
        message="run admitted",
    )


def admit(holder: m1.Owned, record: RunAdmission | None = None) -> RunAdmission:
    """Seed the durable job this run binds to, then admit the run through the API."""
    record = record or admission()
    m18.seed_job(holder, job_id=record.job_id)
    admit_run(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        admission=record,
    )
    return record


def add_step(
    holder: m1.Owned,
    *,
    run_id: str = RUN_ID,
    run_step_id: str = STEP_ID,
    ordinal: int = 1,
    step_kind: str = "plan",
    created_at_us: int = BASE_US,
) -> None:
    append_run_step(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        run_id=run_id,
        run_step_id=run_step_id,
        ordinal=ordinal,
        step_kind=step_kind,
        created_at_us=created_at_us,
    )


# --- round trip -------------------------------------------------------------------


def test_admitting_a_run_binds_the_job_and_opens_the_stream(owned: m1.Owned) -> None:
    admit(owned)
    snapshot = read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert snapshot is not None
    assert isinstance(snapshot, RunSnapshot)
    assert snapshot.job_id == JOB_ID
    assert snapshot.definition == DEFINITION
    assert snapshot.status == "admitted"
    assert snapshot.created_at == snapshot.updated_at
    assert snapshot.finished_at is None
    assert snapshot.steps == () and snapshot.waits == ()
    assert [event.sequence for event in snapshot.events] == [0]
    assert snapshot.events[0].message == "run admitted"
    correlation = snapshot.correlations[0]
    assert correlation.source_kind == "application_job"
    assert correlation.source_id == JOB_ID
    assert correlation.workspace_id == WORKSPACE_ID
    scheduler_rows = owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_durable_jobs"
    ).fetchone()
    assert scheduler_rows[0] == 1


def test_a_full_run_round_trips_as_contract_records(owned: m1.Owned) -> None:
    """Steps, attempts, waits and events read back as the accepted types.

    Held to `semantics_runtime`'s own validators rather than to assertions written
    here: the point is that what storage returns satisfies the contract, not that it
    satisfies this file's idea of it.
    """
    admit(owned)
    add_step(owned)
    append_run_event(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        run_id=RUN_ID,
        runtime_event_id="evt-0002",
        occurred_at_us=BASE_US + MS,
        event_kind="run_started",
        run_status="running",
        run_step_id=STEP_ID,
        details={"attempt": 1},
    )
    record_step_status(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        run_step_id=STEP_ID,
        status="running",
        observed_at_us=BASE_US + MS,
    )
    start_attempt(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        attempt_id="att-0001",
        run_id=RUN_ID,
        run_step_id=STEP_ID,
        attempt_number=1,
        started_at_us=BASE_US + MS,
    )
    finish_attempt(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        attempt_id="att-0001",
        status="failed",
        finished_at_us=BASE_US + 2 * MS,
        failure=ApiError(
            code="internal_recoverable",
            message="the provider timed out",
            retry_class="retryable",
        ),
    )
    open_wait(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        wait_id="wait-0001",
        run_id=RUN_ID,
        run_step_id=STEP_ID,
        kind="approval",
        created_at_us=BASE_US + 3 * MS,
        resume_digest=DIGEST,
    )
    record_step_status(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        run_step_id=STEP_ID,
        status="waiting",
        observed_at_us=BASE_US + 3 * MS,
    )

    snapshot = read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert snapshot is not None
    step = snapshot.steps[0]
    assert step.status == "waiting"
    assert step.wait_id == "wait-0001"
    assert step.updated_at != step.created_at
    validate_run_step(step, run_id=RUN_ID, workspace_id=WORKSPACE_ID)
    validate_attempt_history(
        step.attempts,
        run_id=RUN_ID,
        run_step_id=STEP_ID,
        workspace_id=WORKSPACE_ID,
    )
    attempt = step.attempts[0]
    assert attempt.status == "failed"
    assert attempt.failure is not None
    assert attempt.failure.code == "internal_recoverable"

    wait = snapshot.waits[0]
    assert wait.status == "pending"
    assert wait.resolved_at is None and wait.approval_id is None
    validate_wait(wait, run_id=RUN_ID, workspace_id=WORKSPACE_ID)

    validate_runtime_event_stream(
        snapshot.events, run_id=RUN_ID, workspace_id=WORKSPACE_ID
    )
    assert snapshot.events[1].details == {"attempt": 1}
    assert snapshot.status == "running"
    assert snapshot.finished_at is None


def test_a_resolved_wait_reports_its_decision(owned: m1.Owned) -> None:
    admit(owned)
    add_step(owned)
    open_wait(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        wait_id="wait-0001",
        run_id=RUN_ID,
        run_step_id=STEP_ID,
        kind="approval",
        created_at_us=BASE_US,
        resume_digest=DIGEST,
    )
    close_wait(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        wait_id="wait-0001",
        status="resolved",
        resolved_at_us=BASE_US + 9,
        resolution_reason="approved",
        approval_id="apr-0001",
    )
    wait = read_run_waits(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)[0]
    assert wait.status == "resolved"
    assert wait.approval_id == "apr-0001"
    assert wait.resolution_reason == "approved"
    validate_wait(wait, run_id=RUN_ID, workspace_id=WORKSPACE_ID)
    step = read_run_steps(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)[0]
    assert step.wait_id is None


def test_a_terminal_run_reports_when_it_finished(owned: m1.Owned) -> None:
    admit(owned)
    for sequence, (status, kind, offset) in enumerate(
        (("running", "run_started", 1), ("succeeded", "run_succeeded", 5)), start=1
    ):
        assert (
            append_run_event(
                owned.connection,
                owned.identity,
                workspace_id=WORKSPACE_ID,
                fencing_generation=owned.generation,
                run_id=RUN_ID,
                runtime_event_id=f"evt-000{sequence + 1}",
                occurred_at_us=BASE_US + offset,
                event_kind=kind,
                run_status=status,
            )
            == sequence
        )
    snapshot = read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert snapshot is not None
    assert snapshot.status == "succeeded"
    assert snapshot.finished_at == snapshot.updated_at
    assert snapshot.finished_at is not None


# --- replay and deterministic ordering ---------------------------------------------


def test_a_logical_key_finds_the_run_it_already_names(owned: m1.Owned) -> None:
    """Replay is provable: equal keys are one run replayed, not two runs."""
    record = admit(owned)
    assert (
        read_run_id_by_logical_key(
            owned.connection, workspace_id=WORKSPACE_ID, logical_key=record.logical_key
        )
        == RUN_ID
    )
    assert (
        read_run_id_by_job(owned.connection, workspace_id=WORKSPACE_ID, job_id=JOB_ID)
        == RUN_ID
    )
    m18.seed_job(owned, job_id="job-run-0002")
    with m18.guarded(owned):
        m18.insert_claim(
            owned,
            claim_id="clm-second-principal",
            audit_ref=m18.audit_ref_for("job-run-0002"),
            idempotency_key=record.logical_key,
            principal_id="core-service-2",
        )
    with pytest.raises(sqlite3.IntegrityError, match="logical key already names a run"):
        admit_run(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
            admission=admission(
                run_id="run-0002",
                job_id="job-run-0002",
                claim_id="clm-second-principal",
                logical_key=record.logical_key,
                event_id="evt-0002",
            ),
        )


def test_a_run_is_admitted_under_the_existing_claim_it_names(owned: m1.Owned) -> None:
    """The claim is read back off the snapshot, and it is 0007's row, not a new one."""
    record = admit(owned)
    snapshot = read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert snapshot is not None
    assert snapshot.claim_id == record.claim_id
    claim = owned.connection.execute(
        "SELECT operation, idempotency_key, audit_ref FROM omnivia_idempotency_claims "
        "WHERE claim_id = ? AND workspace_id = ?",
        (snapshot.claim_id, WORKSPACE_ID),
    ).fetchone()
    assert claim == (
        snapshot.originating_operation,
        snapshot.logical_key,
        snapshot.audit_reference,
    )
    m18.seed_job(owned, job_id="job-run-0002")
    with pytest.raises(sqlite3.IntegrityError, match="idempotency claim admitting it"):
        admit_run(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
            admission=admission(
                run_id="run-0002",
                job_id="job-run-0002",
                claim_id="clm-absent",
                event_id="evt-0002",
            ),
        )


def test_replay_reads_the_stream_in_one_deterministic_order(owned: m1.Owned) -> None:
    """Sequence, not time. Two events in the same microsecond still have one order."""
    admit(owned)
    for index in range(1, 5):
        append_run_event(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
            run_id=RUN_ID,
            runtime_event_id=f"evt-{index:04d}b",
            occurred_at_us=BASE_US + 7,
            event_kind="step_started",
            run_status="running",
        )
    events = read_run_events(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert [event.sequence for event in events] == [0, 1, 2, 3, 4]
    assert len({event.occurred_at for event in events[1:]}) == 1
    assert events == read_run_events(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    tail = read_run_events(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID, start_sequence=3
    )
    assert [event.sequence for event in tail] == [3, 4]
    assert [
        event.sequence
        for event in read_run_events(
            owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID, limit=2
        )
    ] == [0, 1]


def test_steps_read_back_in_ordinal_order_whatever_order_they_were_written(
    owned: m1.Owned,
) -> None:
    admit(owned)
    for ordinal in (1, 2, 3):
        add_step(
            owned,
            run_step_id=f"step-{ordinal:04d}",
            ordinal=ordinal,
            created_at_us=BASE_US + ordinal,
        )
    steps = read_run_steps(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert [step.ordinal for step in steps] == [1, 2, 3]
    assert [step.run_step_id for step in steps] == [
        "step-0001",
        "step-0002",
        "step-0003",
    ]


def test_workspace_run_ids_are_listed_in_one_deterministic_order(
    owned: m1.Owned,
) -> None:
    admit(owned)
    m18.seed_job(owned, job_id="job-run-0002")
    admit_run(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        admission=admission(
            run_id="run-0002", job_id="job-run-0002", event_id="evt-0002"
        ),
    )
    assert read_workspace_run_ids(owned.connection, workspace_id=WORKSPACE_ID) == (
        RUN_ID,
        "run-0002",
    )


# --- missing records and foreign workspaces -----------------------------------------


def test_a_run_that_does_not_exist_reads_as_absent_rather_than_empty(
    owned: m1.Owned,
) -> None:
    assert read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id="run-absent") is None
    assert (
        read_run_id_by_logical_key(
            owned.connection, workspace_id=WORKSPACE_ID, logical_key="lk-absent"
        )
        is None
    )
    assert (
        read_run_id_by_job(
            owned.connection, workspace_id=WORKSPACE_ID, job_id="job-absent"
        )
        is None
    )
    assert (
        read_run_steps(owned.connection, workspace_id=WORKSPACE_ID, run_id="run-absent")
        == ()
    )
    assert (
        read_run_waits(owned.connection, workspace_id=WORKSPACE_ID, run_id="run-absent")
        == ()
    )
    assert (
        read_run_events(
            owned.connection, workspace_id=WORKSPACE_ID, run_id="run-absent"
        )
        == ()
    )
    assert read_workspace_run_ids(owned.connection, workspace_id=WORKSPACE_ID) == ()


def test_a_reader_never_answers_for_a_workspace_it_was_not_asked_about(
    owned: m1.Owned,
) -> None:
    """A workspace arrives as an argument, never out of the record being read."""
    admit(owned)
    assert (
        read_run(owned.connection, workspace_id=OTHER_WORKSPACE_ID, run_id=RUN_ID)
        is None
    )
    assert (
        read_run_id_by_logical_key(
            owned.connection,
            workspace_id=OTHER_WORKSPACE_ID,
            logical_key=m18.logical_key_for(JOB_ID),
        )
        is None
    )
    assert read_workspace_run_ids(
        owned.connection, workspace_id=OTHER_WORKSPACE_ID
    ) == ()
    snapshot = read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert snapshot is not None
    assert snapshot.events[0].workspace_id == WORKSPACE_ID


# --- transactions, authority and malformed input --------------------------------------


def test_every_write_opens_its_own_fenced_transaction(owned: m1.Owned) -> None:
    """No caller-supplied transaction, and no autocommit outside the fence.

    A stale generation is refused at the fence rather than at the row, and nothing
    from the refused call survives -- including the first of the two statements
    `admit_run` issues.
    """
    m18.seed_job(owned)
    with pytest.raises(StaleGeneration):
        admit_run(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation + 1,
            admission=admission(),
        )
    assert (
        owned.connection.execute(
            "SELECT COUNT(*) FROM omnivia_runtime_runs"
        ).fetchone()[0]
        == 0
    )
    assert (
        owned.connection.execute(
            "SELECT COUNT(*) FROM omnivia_runtime_events"
        ).fetchone()[0]
        == 0
    )
    admit_run(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        admission=admission(),
    )
    assert read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)


def standalone_writes(holder: m1.Owned, generation: int) -> dict[str, object]:
    """Every public write, bound to one generation, ready to call with no arguments.

    Named so a failure says which writer was not fenced. Testing only `admit_run` left
    seven functions whose fence was asserted by their shape rather than by a test.
    """
    common = {
        "workspace_id": WORKSPACE_ID,
        "fencing_generation": generation,
    }
    return {
        "admit_run": lambda: admit_run(
            holder.connection, holder.identity, **common, admission=admission()
        ),
        "append_run_step": lambda: append_run_step(
            holder.connection,
            holder.identity,
            **common,
            run_id=RUN_ID,
            run_step_id="step-0009",
            ordinal=2,
            step_kind="act",
            created_at_us=BASE_US,
        ),
        "record_step_status": lambda: record_step_status(
            holder.connection,
            holder.identity,
            **common,
            run_step_id=STEP_ID,
            status="running",
            observed_at_us=BASE_US + MS,
        ),
        "start_attempt": lambda: start_attempt(
            holder.connection,
            holder.identity,
            **common,
            attempt_id="att-0009",
            run_id=RUN_ID,
            run_step_id=STEP_ID,
            attempt_number=1,
            started_at_us=BASE_US,
        ),
        "finish_attempt": lambda: finish_attempt(
            holder.connection,
            holder.identity,
            **common,
            attempt_id="att-0001",
            status="succeeded",
            finished_at_us=BASE_US + MS,
        ),
        "open_wait": lambda: open_wait(
            holder.connection,
            holder.identity,
            **common,
            wait_id="wait-0009",
            run_id=RUN_ID,
            run_step_id=STEP_ID,
            kind="timer",
            created_at_us=BASE_US,
            resume_digest=DIGEST,
        ),
        "close_wait": lambda: close_wait(
            holder.connection,
            holder.identity,
            **common,
            wait_id="wait-0001",
            status="cancelled",
            resolved_at_us=BASE_US + MS,
            resolution_reason="run_cancelled",
        ),
        "append_run_event": lambda: append_run_event(
            holder.connection,
            holder.identity,
            **common,
            run_id=RUN_ID,
            runtime_event_id="evt-0009",
            occurred_at_us=BASE_US + MS,
            event_kind="run_started",
            run_status="running",
        ),
    }


RUNTIME_TABLES = (
    "omnivia_runtime_runs",
    "omnivia_runtime_run_steps",
    "omnivia_runtime_run_step_states",
    "omnivia_runtime_attempts",
    "omnivia_runtime_attempt_outcomes",
    "omnivia_runtime_waits",
    "omnivia_runtime_wait_resolutions",
    "omnivia_runtime_events",
)


def runtime_row_counts(holder: m1.Owned) -> dict[str, int]:
    return {
        table: int(
            holder.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        )
        for table in RUNTIME_TABLES
    }


STANDALONE_WRITERS = (
    "admit_run",
    "append_run_step",
    "record_step_status",
    "start_attempt",
    "finish_attempt",
    "open_wait",
    "close_wait",
    "append_run_event",
)


@pytest.mark.parametrize("name", STANDALONE_WRITERS)
def test_every_standalone_writer_is_refused_by_a_stale_fence(
    owned: m1.Owned, name: str
) -> None:
    """Not just `admit_run`: a writer that forgot its fence is a writer nothing checks."""
    if name == "admit_run":
        m18.seed_job(owned)
    else:
        admit(owned)
        add_step(owned)
    if name == "finish_attempt":
        start_attempt(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
            attempt_id="att-0001",
            run_id=RUN_ID,
            run_step_id=STEP_ID,
            attempt_number=1,
            started_at_us=BASE_US,
        )
    if name == "close_wait":
        open_wait(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
            wait_id="wait-0001",
            run_id=RUN_ID,
            run_step_id=STEP_ID,
            kind="timer",
            created_at_us=BASE_US,
            resume_digest=DIGEST,
        )
    before = runtime_row_counts(owned)
    stale = standalone_writes(owned, owned.generation + 1)[name]
    with pytest.raises(StaleGeneration):
        stale()
    assert runtime_row_counts(owned) == before
    # And the same call under current authority commits, so the refusal was the fence
    # rather than the row.
    standalone_writes(owned, owned.generation)[name]()
    assert runtime_row_counts(owned) != before


def test_two_runtime_writes_compose_under_one_fence(owned: m1.Owned) -> None:
    """The seam RT-104 needs: several writes, one `BEGIN IMMEDIATE`, one commit.

    Calling the standalone functions in sequence would open one transaction each, so a
    command that had to settle its claim and append its events atomically could not be
    built out of them at all.
    """
    admit(owned)
    with runtime_writer(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as writer:
        writer.append_run_step(
            run_id=RUN_ID,
            run_step_id=STEP_ID,
            ordinal=1,
            step_kind="plan",
            created_at_us=BASE_US,
        )
        assert (
            writer.append_run_event(
                run_id=RUN_ID,
                runtime_event_id="evt-0002",
                occurred_at_us=BASE_US + MS,
                event_kind="step_started",
                run_status="running",
                run_step_id=STEP_ID,
            )
            == 1
        )
    snapshot = read_run(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert snapshot is not None
    assert [step.run_step_id for step in snapshot.steps] == [STEP_ID]
    assert [event.sequence for event in snapshot.events] == [0, 1]


def test_a_composition_that_fails_later_rolls_back_what_succeeded_earlier(
    owned: m1.Owned,
) -> None:
    """One fence, one outcome. The first write does not survive the second's refusal."""
    admit(owned)
    before = runtime_row_counts(owned)
    with (
        pytest.raises(sqlite3.IntegrityError, match="ordinals must be contiguous"),
        runtime_writer(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ) as writer,
    ):
            writer.append_run_step(
                run_id=RUN_ID,
                run_step_id=STEP_ID,
                ordinal=1,
                step_kind="plan",
                created_at_us=BASE_US,
            )
            writer.append_run_step(
                run_id=RUN_ID,
                run_step_id="step-0002",
                ordinal=4,
                step_kind="act",
                created_at_us=BASE_US,
            )
    assert runtime_row_counts(owned) == before


def test_a_composition_carries_a_callers_own_statement_in_the_same_transaction(
    owned: m1.Owned,
) -> None:
    """What RT-104 will actually do: settle the existing claim beside the run history.

    The settlement here is 0007's `omnivia_idempotency_outcomes` row -- the relation
    that already decides replay -- written on the same connection inside the same
    fence, and rolled back with the runtime write when the runtime write is refused.
    """
    admit(owned)
    before = runtime_row_counts(owned)
    settle = (
        "INSERT INTO omnivia_idempotency_outcomes (outcome_id, claim_id, workspace_id, "
        "outcome_branch, error_code, outcome_json, outcome_reference, outcome_digest, "
        "audit_ref, settled_at_us) VALUES (?, ?, ?, 'success', NULL, '{}', NULL, ?, ?, ?)"
    )
    parameters = (
        "out-0001",
        m18.claim_id_for(JOB_ID),
        WORKSPACE_ID,
        DIGEST,
        m18.audit_ref_for(JOB_ID),
        BASE_US + MS,
    )
    with (
        pytest.raises(sqlite3.IntegrityError, match="opens at sequence zero"),
        runtime_writer(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ) as writer,
    ):
            owned.connection.execute(settle, parameters)
            writer.append_run_event(
                run_id=RUN_ID,
                runtime_event_id="evt-0002",
                occurred_at_us=BASE_US + MS,
                event_kind="run_admitted",
                run_status="admitted",
            )
    assert runtime_row_counts(owned) == before
    assert (
        owned.connection.execute(
            "SELECT COUNT(*) FROM omnivia_idempotency_outcomes"
        ).fetchone()[0]
        == 0
    )
    with runtime_writer(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as writer:
        owned.connection.execute(settle, parameters)
        writer.append_run_event(
            run_id=RUN_ID,
            runtime_event_id="evt-0002",
            occurred_at_us=BASE_US + MS,
            event_kind="run_succeeded",
            run_status="running",
        )
    assert (
        owned.connection.execute(
            "SELECT COUNT(*) FROM omnivia_idempotency_outcomes"
        ).fetchone()[0]
        == 1
    )


def test_a_refused_write_rolls_back_both_of_its_statements(owned: m1.Owned) -> None:
    """`append_run_step` writes a step and its first status, or neither."""
    admit(owned)
    with pytest.raises(sqlite3.IntegrityError, match="ordinals must be contiguous"):
        add_step(owned, ordinal=4)
    assert read_run_steps(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID) == ()
    assert (
        owned.connection.execute(
            "SELECT COUNT(*) FROM omnivia_runtime_run_step_states"
        ).fetchone()[0]
        == 0
    )
    add_step(owned)
    step = read_run_steps(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)[0]
    assert step.status == "pending"


def tamper(holder: m1.Owned, *statements: str) -> sqlite3.Connection:
    """Close the owned connection and reopen the file with no guard in the way.

    The guards refuse this from inside the runtime, which is the point of them; a
    reader still has to behave when the file was corrupted from outside it, and the
    only way to get such a file is to make one.
    """
    holder.connection.close()
    connection = sqlite3.connect(str(holder.path))
    for statement in statements:
        connection.execute(statement)
    connection.commit()
    return connection


def test_a_step_with_no_recorded_status_is_reported_not_guessed(
    owned: m1.Owned,
) -> None:
    """A reader that invented `pending` here would report a status nobody wrote."""
    admit(owned)
    add_step(owned)
    connection = tamper(
        owned,
        "DROP TRIGGER omnivia_guard_runtime_run_step_states_delete",
        "DELETE FROM omnivia_runtime_run_step_states",
    )
    try:
        with pytest.raises(StorageError, match="has no recorded status"):
            read_run_steps(connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    finally:
        connection.close()


def test_an_attempt_outcome_that_misstates_its_failure_is_refused_before_the_write(
    owned: m1.Owned,
) -> None:
    """Uncertainty is not failure, and a failed attempt must state its failure."""
    admit(owned)
    add_step(owned)
    start_attempt(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        attempt_id="att-0001",
        run_id=RUN_ID,
        run_step_id=STEP_ID,
        attempt_number=1,
        started_at_us=BASE_US,
    )
    failure = ApiError(code="internal", message="boom", retry_class="non_retryable")
    with pytest.raises(StorageError, match="does not carry a failure"):
        finish_attempt(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
            attempt_id="att-0001",
            status="uncertain",
            finished_at_us=BASE_US + 1,
            failure=failure,
        )
    with pytest.raises(StorageError, match="does not carry a failure"):
        finish_attempt(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
            attempt_id="att-0001",
            status="failed",
            finished_at_us=BASE_US + 1,
        )
    assert (
        owned.connection.execute(
            "SELECT COUNT(*) FROM omnivia_runtime_attempt_outcomes"
        ).fetchone()[0]
        == 0
    )
    finish_attempt(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        attempt_id="att-0001",
        status="uncertain",
        finished_at_us=BASE_US + 1,
    )
    step = read_run_steps(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)[0]
    assert step.attempts[0].status == "uncertain"
    assert step.attempts[0].failure is None


def test_a_detail_that_is_not_a_json_object_has_nowhere_to_go(owned: m1.Owned) -> None:
    admit(owned)
    with pytest.raises(StorageError, match="must be a JSON object"):
        append_run_event(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
            run_id=RUN_ID,
            runtime_event_id="evt-0002",
            occurred_at_us=BASE_US + 1,
            event_kind="run_started",
            run_status="running",
            details=["not", "an", "object"],  # type: ignore[arg-type]
        )
    assert [
        event.sequence
        for event in read_run_events(
            owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
        )
    ] == [0]


def digest_of(document: str) -> str:
    return "sha256:" + hashlib.sha256(document.encode("utf-8")).hexdigest()


def seed_event_with_details(owned: m1.Owned) -> None:
    admit(owned)
    append_run_event(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        run_id=RUN_ID,
        runtime_event_id="evt-0002",
        occurred_at_us=BASE_US + MS,
        event_kind="run_started",
        run_status="running",
        details={"reason": "policy_narrowed"},
    )


def seed_failed_attempt(owned: m1.Owned) -> None:
    admit(owned)
    add_step(owned)
    start_attempt(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        attempt_id="att-0001",
        run_id=RUN_ID,
        run_step_id=STEP_ID,
        attempt_number=1,
        started_at_us=BASE_US,
    )
    finish_attempt(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        attempt_id="att-0001",
        status="failed",
        finished_at_us=BASE_US + MS,
        failure=ApiError(
            code="internal_recoverable", message="boom", retry_class="retryable"
        ),
    )


def test_a_stored_detail_that_is_no_longer_an_object_is_reported_not_returned(
    owned: m1.Owned,
) -> None:
    """A reader that shrugged at a corrupt document would hand it on as a fact."""
    document = "[1, 2]"
    admit(owned)
    connection = tamper(
        owned,
        "DROP TRIGGER omnivia_guard_runtime_events_update",
        f"UPDATE omnivia_runtime_events SET details_json = '{document}', "
        f"details_digest = '{digest_of(document)}', details_byte_length = 6",
    )
    try:
        with pytest.raises(StorageError, match="not a JSON object"):
            read_run_events(connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    finally:
        connection.close()


def test_a_stored_detail_whose_digest_is_false_is_refused_however_well_it_parses(
    owned: m1.Owned,
) -> None:
    """The corrupt document is a perfectly good JSON object, and that is the point.

    Every earlier check -- the CHECK constraints, the shape test above -- passes on a
    substituted object of the same length. Only recomputing the digest notices that the
    bytes are not the bytes that were hashed, so a reader that returned them would be
    reporting somebody else's document as this event's detail.
    """
    seed_event_with_details(owned)
    substituted = '{"reason":"policy_widened!"}'
    original = owned.connection.execute(
        "SELECT details_json, details_byte_length FROM omnivia_runtime_events "
        "WHERE sequence = 1"
    ).fetchone()
    assert len(substituted.encode()) == int(original[1])
    connection = tamper(
        owned,
        "DROP TRIGGER omnivia_guard_runtime_events_update",
        f"UPDATE omnivia_runtime_events SET details_json = '{substituted}' "
        "WHERE sequence = 1",
    )
    try:
        assert json.loads(substituted) == {"reason": "policy_widened!"}
        with pytest.raises(StorageError, match="does not match its recorded digest"):
            read_run_events(connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    finally:
        connection.close()


def test_a_stored_detail_whose_byte_length_is_wrong_fails_closed(
    owned: m1.Owned,
) -> None:
    """The CHECK holds this for writes through this database; an offline edit need not.

    `ignore_check_constraints` is how a test reaches that state honestly: the file's
    own constraint is switched off for one statement, exactly as a tool that never
    enforced it would leave the row, and the reader still refuses the document.
    """
    seed_event_with_details(owned)
    connection = tamper(
        owned,
        "PRAGMA ignore_check_constraints = ON",
        "DROP TRIGGER omnivia_guard_runtime_events_update",
        "UPDATE omnivia_runtime_events SET details_byte_length = details_byte_length + 1 "
        "WHERE sequence = 1",
    )
    try:
        with pytest.raises(
            StorageError, match="does not match its recorded byte length"
        ):
            read_run_events(connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    finally:
        connection.close()


def test_a_stored_failure_whose_digest_is_false_is_refused(owned: m1.Owned) -> None:
    """The same rule one relation over, on the other nested document 0018 stores."""
    seed_failed_attempt(owned)
    stored = owned.connection.execute(
        "SELECT failure_json FROM omnivia_runtime_attempt_outcomes"
    ).fetchone()
    substituted = str(stored[0]).replace("boom", "BOOM")
    assert len(substituted) == len(str(stored[0]))
    connection = tamper(
        owned,
        "DROP TRIGGER omnivia_guard_runtime_attempt_outcomes_update",
        "UPDATE omnivia_runtime_attempt_outcomes SET failure_json = "
        f"'{substituted}'",
    )
    try:
        with pytest.raises(StorageError, match="does not match its recorded digest"):
            read_run_steps(connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    finally:
        connection.close()


def test_a_stored_failure_that_is_not_a_valid_api_error_is_a_storage_error(
    owned: m1.Owned,
) -> None:
    """A contract decoding failure three layers down is not what a caller can act on."""
    seed_failed_attempt(owned)
    substituted = '{"code":true}'
    connection = tamper(
        owned,
        "DROP TRIGGER omnivia_guard_runtime_attempt_outcomes_update",
        "UPDATE omnivia_runtime_attempt_outcomes SET failure_json = "
        f"'{substituted}', failure_digest = '{digest_of(substituted)}', "
        f"failure_byte_length = {len(substituted.encode())}",
    )
    try:
        with pytest.raises(StorageError, match="not a valid ApiError"):
            read_run_steps(connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    finally:
        connection.close()


def test_a_stored_document_that_is_not_json_at_all_is_a_storage_error(
    owned: m1.Owned,
) -> None:
    """`JSONDecodeError` is an implementation detail of how the bytes were parsed."""
    seed_event_with_details(owned)
    broken = "{not json"
    connection = tamper(
        owned,
        "DROP TRIGGER omnivia_guard_runtime_events_update",
        f"UPDATE omnivia_runtime_events SET details_json = '{broken}', "
        f"details_digest = '{digest_of(broken)}', "
        f"details_byte_length = {len(broken.encode())} WHERE sequence = 1",
    )
    try:
        with pytest.raises(StorageError, match="not valid JSON"):
            read_run_events(connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    finally:
        connection.close()


# --- no wire drift ---------------------------------------------------------------------


def test_this_lane_moved_no_public_contract() -> None:
    """RT-102 is persistence. The frozen application surface is untouched."""
    assert CONTRACT_VERSION == "1.3"
