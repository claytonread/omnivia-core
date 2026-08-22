"""RT-105 acceptance for transactional Run summaries and full replay."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
import test_rt102_agent_runtime_repository as repository
from omnivia_core_runtime.storage import agent_runtime
from omnivia_core_runtime.storage import projections as projections_package
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.storage.projections import runtime_run_summary as projection
from omnivia_core_runtime.storage.projections.runtime_run_summary import (
    read_runtime_run_summary,
    rebuild_runtime_run_summaries,
    runtime_run_summary_projection_bytes,
    runtime_run_summary_projection_digest,
)

WORKSPACE_ID = m18.WORKSPACE_ID
RUN_ID = m18.RUN_ID
BASE_US = m18.BASE_US
MS = 1_000


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


def admit(
    holder: m1.Owned,
    *,
    run_id: str = RUN_ID,
    job_id: str = m18.JOB_ID,
    event_id: str = "evt-0001",
) -> None:
    record = repository.admission(
        run_id=run_id,
        job_id=job_id,
        event_id=event_id,
    )
    m18.seed_job(holder, job_id=job_id)
    agent_runtime.admit_run(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        admission=record,
    )


def append_status(
    holder: m1.Owned,
    *,
    run_id: str,
    event_id: str,
    status: str,
    occurred_at_us: int,
) -> int:
    return agent_runtime.append_run_event(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        run_id=run_id,
        runtime_event_id=event_id,
        occurred_at_us=occurred_at_us,
        event_kind=f"run_{status}",
        run_status=status,
    )


def test_admission_and_event_append_materialise_in_their_transactions(
    owned: m1.Owned,
) -> None:
    admit(owned)
    admitted = read_runtime_run_summary(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert admitted is not None
    assert admitted.status == "admitted"
    assert admitted.last_runtime_event_id == "evt-0001"
    assert admitted.last_sequence == 0
    assert admitted.created_at == admitted.updated_at
    assert admitted.finished_at is None

    assert (
        append_status(
            owned,
            run_id=RUN_ID,
            event_id="evt-0002",
            status="running",
            occurred_at_us=BASE_US + MS,
        )
        == 1
    )
    running = read_runtime_run_summary(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert running is not None
    assert running.status == "running"
    assert running.last_runtime_event_id == "evt-0002"
    assert running.last_sequence == 1
    assert running.updated_at != running.created_at
    assert running.finished_at is None


def test_terminal_timestamp_is_the_terminal_event_and_nonterminal_has_none(
    owned: m1.Owned,
) -> None:
    admit(owned)
    append_status(
        owned,
        run_id=RUN_ID,
        event_id="evt-0002",
        status="running",
        occurred_at_us=BASE_US + MS,
    )
    append_status(
        owned,
        run_id=RUN_ID,
        event_id="evt-0003",
        status="succeeded",
        occurred_at_us=BASE_US + 2 * MS,
    )
    summary = read_runtime_run_summary(
        owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert summary is not None
    assert summary.status == "succeeded"
    assert summary.finished_at == summary.updated_at
    assert summary.last_sequence == 2


def test_projection_failure_rolls_back_the_canonical_event(
    owned: m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    admit(owned)
    before = runtime_run_summary_projection_bytes(
        owned.connection, workspace_id=WORKSPACE_ID
    )

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise StorageError("forced projection refusal")

    monkeypatch.setattr(projection, "apply_runtime_run_summary_event", refuse)
    with pytest.raises(StorageError, match="forced projection refusal"):
        append_status(
            owned,
            run_id=RUN_ID,
            event_id="evt-0002",
            status="running",
            occurred_at_us=BASE_US + MS,
        )

    assert (
        agent_runtime.read_run_sequence(
            owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
        )
        == 0
    )
    assert (
        runtime_run_summary_projection_bytes(
            owned.connection, workspace_id=WORKSPACE_ID
        )
        == before
    )


def test_projection_failure_rolls_back_admission_itself(
    owned: m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = repository.admission()
    m18.seed_job(owned, job_id=record.job_id)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise StorageError("forced admission projection refusal")

    monkeypatch.setattr(projection, "apply_runtime_run_summary_event", refuse)
    with pytest.raises(StorageError, match="forced admission projection refusal"):
        agent_runtime.admit_run(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
            admission=record,
        )
    assert (
        agent_runtime.read_run(
            owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
        )
        is None
    )
    assert (
        owned.connection.execute(
            "SELECT COUNT(*) FROM omnivia_runtime_events WHERE workspace_id = ?",
            (WORKSPACE_ID,),
        ).fetchone()[0]
        == 0
    )


def test_reapplying_latest_and_older_canonical_events_is_idempotent(
    owned: m1.Owned,
) -> None:
    admit(owned)
    append_status(
        owned,
        run_id=RUN_ID,
        event_id="evt-0002",
        status="running",
        occurred_at_us=BASE_US + MS,
    )
    before = runtime_run_summary_projection_bytes(
        owned.connection, workspace_id=WORKSPACE_ID
    )
    latest = projection.apply_runtime_run_summary_event(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        runtime_event_id="evt-0002",
    )
    older = projection.apply_runtime_run_summary_event(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        runtime_event_id="evt-0001",
    )
    assert latest == older
    assert latest.last_sequence == 1
    assert (
        runtime_run_summary_projection_bytes(
            owned.connection, workspace_id=WORKSPACE_ID
        )
        == before
    )


def test_missing_and_cross_workspace_sources_fail_closed(owned: m1.Owned) -> None:
    admit(owned)
    with pytest.raises(StorageError, match="no canonical run/event source"):
        projection.apply_runtime_run_summary_event(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            run_id=RUN_ID,
            runtime_event_id="evt-missing",
        )
    with pytest.raises(StorageError, match="no canonical run/event source"):
        projection.apply_runtime_run_summary_event(
            owned.connection,
            workspace_id=m18.OTHER_WORKSPACE_ID,
            run_id=RUN_ID,
            runtime_event_id="evt-0001",
        )
    assert (
        read_runtime_run_summary(
            owned.connection, workspace_id=m18.OTHER_WORKSPACE_ID, run_id=RUN_ID
        )
        is None
    )


def test_gap_and_run_conflict_are_refused_by_the_pure_reducer() -> None:
    run = projection._RunSource(
        workspace_id="ws-a",
        run_id="run-a",
        job_id="job-a",
        definition_kind="agent_component",
        definition_id="component.a",
        definition_version="1.0.0",
        logical_key="logical-a",
        originating_operation="runtime.admit",
        audit_ref="audit-a",
        created_at_us=BASE_US,
    )
    admitted = projection._reduce_event(
        None,
        run,
        projection._EventSource("evt-a0", 0, BASE_US, "admitted"),
    )
    with pytest.raises(StorageError, match="not contiguous"):
        projection._reduce_event(
            admitted,
            run,
            projection._EventSource("evt-a2", 2, BASE_US + MS, "running"),
        )
    other = projection._RunSource(
        workspace_id="ws-a",
        run_id="run-b",
        job_id="job-a",
        definition_kind="agent_component",
        definition_id="component.a",
        definition_version="1.0.0",
        logical_key="logical-a",
        originating_operation="runtime.admit",
        audit_ref="audit-a",
        created_at_us=BASE_US,
    )
    with pytest.raises(StorageError, match="another run summary"):
        projection._reduce_event(
            admitted,
            other,
            projection._EventSource("evt-b1", 1, BASE_US + MS, "running"),
        )


def test_clean_and_repeated_rebuild_equal_incremental_bytes_and_digest(
    owned: m1.Owned,
) -> None:
    admit(owned)
    append_status(
        owned,
        run_id=RUN_ID,
        event_id="evt-0002",
        status="running",
        occurred_at_us=BASE_US + MS,
    )
    admit(
        owned,
        run_id="run-0002",
        job_id="job-run-0002",
        event_id="evt-run2-0001",
    )
    incremental = runtime_run_summary_projection_bytes(
        owned.connection, workspace_id=WORKSPACE_ID
    )
    incremental_digest = runtime_run_summary_projection_digest(
        owned.connection, workspace_id=WORKSPACE_ID
    )

    first = rebuild_runtime_run_summaries(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    )
    assert first.record_count == 2
    assert first.build_digest == incremental_digest
    assert (
        runtime_run_summary_projection_bytes(
            owned.connection, workspace_id=WORKSPACE_ID
        )
        == incremental
    )

    second = rebuild_runtime_run_summaries(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    )
    assert second == first
    assert (
        runtime_run_summary_projection_bytes(
            owned.connection, workspace_id=WORKSPACE_ID
        )
        == incremental
    )


def test_failed_rebuild_restores_the_previous_live_projection(
    owned: m1.Owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    admit(owned)
    append_status(
        owned,
        run_id=RUN_ID,
        event_id="evt-0002",
        status="running",
        occurred_at_us=BASE_US + MS,
    )
    before = runtime_run_summary_projection_bytes(
        owned.connection, workspace_id=WORKSPACE_ID
    )
    original = projection._reduce_event
    calls = 0

    def fail_second(
        current: projection._StoredSummary | None,
        run: projection._RunSource,
        event: projection._EventSource,
    ) -> projection._StoredSummary:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise StorageError("forced rebuild failure")
        return original(current, run, event)

    monkeypatch.setattr(projection, "_reduce_event", fail_second)
    with pytest.raises(StorageError, match="forced rebuild failure"):
        rebuild_runtime_run_summaries(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        )
    assert (
        runtime_run_summary_projection_bytes(
            owned.connection, workspace_id=WORKSPACE_ID
        )
        == before
    )


def test_projection_package_documents_both_projection_shapes() -> None:
    assert "runtime_run_summary" in (projections_package.__doc__ or "")
