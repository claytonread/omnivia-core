"""C21-A acceptance for the trigger telemetry writer and its bounded reads.

Two halves, one property between them: neither may answer a question the ledgers have not
answered.

*Writes are fenced, numbered by the database, and replay-safe.* The same identifier with
the same input returns the stored row; the same identifier with changed input refuses;
an idempotency key is accepted once and a duplicate must repeat it unchanged.

*Reads are bounded, scoped and honest.* A projection is keyed by Project, Workflow and
trigger, and another Project's or Workflow's trigger reads as absent. Delivery is not
processing: an accepted stimulus reads as `unlinked` or `unknown` until the job and run
ledgers say otherwise, and `succeeded` only when they do. Failures and uncertainty are
reported explicitly rather than dropped.

Everything runs against a real SQLite workspace migrated to head.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_c21_trigger_telemetry_migration as m43
import test_rt102_agent_runtime_migration as m18
import test_workflow_runs_migration as m27
from omnivia_core_runtime.storage import trigger_telemetry as store
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.storage.trigger_telemetry import (
    DEAD_LETTER_REASONS,
    MAX_OBSERVATION_WINDOW,
    MAX_TRIGGER_PAGE,
    list_workflow_trigger_telemetry,
    read_accepted_trigger_observation,
    read_trigger_declaration,
    read_trigger_telemetry,
    read_wait_signal_telemetry,
    transaction_local_telemetry_writer,
    trigger_telemetry_writer,
)

from omnivia_core.contracts import v1 as contract

WORKSPACE_ID = m43.WORKSPACE_ID
PROJECT = m43.PROJECT_ID
WORKFLOW = m27.WORKFLOW_ID
TRIGGER = m43.TRIGGER_ID
AUDIT = m43.AUDIT_REF
BASE = m43.BASE_US


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    holder = m1.take_ownership(path)
    m43.seed_plan(holder)
    yield holder
    holder.connection.close()


def writer(holder: m1.Owned) -> Any:
    return trigger_telemetry_writer(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    )


def declare(w: Any, trigger_id: str = TRIGGER, **overrides: object) -> Any:
    values: dict[str, object] = {
        "trigger_declaration_id": f"decl-{trigger_id}-1",
        "trigger_id": trigger_id,
        "trigger_kind": "webhook",
        "project_id": PROJECT,
        "workflow_id": WORKFLOW,
        "workflow_version": m27.WORKFLOW_VERSION,
        "plan_hash": m27.PLAN_HASH,
        "event_type": m43.EVENT_TYPE,
        "event_contract_digest": m43.DIGEST_CONTRACT,
        "configuration_digest": m43.DIGEST_CONFIG,
        "declared_at_us": BASE + 1,
        "audit_ref": AUDIT,
    }
    values.update(overrides)
    return w.declare_trigger(**values)


def subscribe(
    w: Any,
    state: str = "active",
    n: int = 1,
    trigger_id: str = TRIGGER,
    **overrides: object,
) -> Any:
    values: dict[str, object] = {
        "subscription_event_id": f"sub-{trigger_id}-{n}",
        "trigger_id": trigger_id,
        "subscription_state": state,
        "reason": "subscription.changed",
        "observed_at_us": BASE + 1 + n,
        "audit_ref": AUDIT,
    }
    values.update(overrides)
    return w.record_subscription_state(**values)


def observe(w: Any, n: int = 1, trigger_id: str = TRIGGER, **overrides: object) -> Any:
    values: dict[str, object] = {
        "trigger_observation_id": f"obs-{trigger_id}-{n}",
        "trigger_id": trigger_id,
        "event_id": f"event-{n}",
        "idempotency_key": f"key-{n}",
        "event_type": m43.EVENT_TYPE,
        "envelope_digest": m43.DIGEST_ENVELOPE,
        "occurred_at_us": BASE + 100 + n,
        "observed_at_us": BASE + 200 + n,
        "delivery_status": "accepted",
        "audit_ref": AUDIT,
    }
    values.update(overrides)
    return w.record_observation(**values)


def read(holder: m1.Owned, **overrides: Any) -> Any:
    args: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "project_id": PROJECT,
        "workflow_id": WORKFLOW,
        "trigger_id": TRIGGER,
    }
    args.update(overrides)
    return read_trigger_telemetry(holder.connection, **args)


def active_trigger(holder: m1.Owned) -> None:
    with writer(holder) as w:
        declare(w)
        subscribe(w)


def job_event(
    holder: m1.Owned, state: str, sequence: int, scheduler_state: str
) -> None:
    """One job event, with the scheduler row it must agree with."""
    with m43.guarded(holder):
        holder.connection.execute(
            "UPDATE omnivia_durable_jobs SET state = ? WHERE job_id = ?",
            (scheduler_state, m18.JOB_ID),
        )
        m43.insert(
            holder,
            "omnivia_job_events",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": m18.JOB_ID,
                "sequence": sequence,
                "occurred_at_us": m18.BASE_US + sequence,
                "state": state,
            },
        )


def run_event(holder: m1.Owned, status: str, sequence: int) -> None:
    with m43.guarded(holder):
        m18.insert_event(
            holder,
            sequence=sequence,
            runtime_event_id=f"evt-run-{sequence:04d}",
            occurred_at_us=m27.BASE_US + sequence,
            event_kind=f"run.{status}",
            run_status=status,
        )


# --- writes --------------------------------------------------------------------------


def test_declaration_replay_returns_the_stored_row_and_changed_input_conflicts(
    owned: m1.Owned,
) -> None:
    with writer(owned) as w:
        first = declare(w)
        assert first.declaration_sequence == 1
        assert declare(w) == first
    with writer(owned) as w, pytest.raises(StorageError, match="different terms"):
        declare(w, event_type="com.example.other")
    count = owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_runtime_trigger_declarations"
    ).fetchone()
    assert count == (1,)


def test_a_later_declaration_is_numbered_and_the_binding_cannot_move(
    owned: m1.Owned,
) -> None:
    with writer(owned) as w:
        declare(w)
        second = declare(
            w,
            trigger_declaration_id="decl-2",
            configuration_digest="sha256:" + "4" * 64,
            declared_at_us=BASE + 2,
        )
        assert second.declaration_sequence == 2
    for rebind in (
        {"project_id": "project-beta"},
        {"workflow_id": "other-workflow"},
        {"trigger_kind": "manual"},
    ):
        with writer(owned) as w, pytest.raises(StorageError, match="bound to another"):
            declare(w, trigger_declaration_id="decl-3", event_type="x.y", **rebind)


def test_a_declaration_must_change_something_and_must_bind_a_sealed_plan(
    owned: m1.Owned,
) -> None:
    with writer(owned) as w:
        declare(w)
    with writer(owned) as w, pytest.raises(sqlite3.IntegrityError, match="must change"):
        declare(w, trigger_declaration_id="decl-2")
    with writer(owned) as w, pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        declare(w, trigger_id="trigger-2", workflow_version="9.9.9")


@pytest.mark.parametrize(
    "override",
    (
        {"trigger_kind": "wait_timer"},
        {"trigger_kind": "wait_signal"},
        {"trigger_id": "bad id"},
        {"trigger_declaration_id": ""},
        {"project_id": "../escape"},
        {"workflow_id": "Upper"},
        {"workflow_version": "v1"},
        {"plan_hash": "sha256:short"},
        {"event_type": "bad type"},
        {"event_contract_digest": "md5:" + "1" * 64},
        {"declared_at_us": 0},
        {"declared_at_us": True},
        {"audit_ref": 7},
        {"trigger_id": "x" * 129},
    ),
)
def test_a_malformed_declaration_is_refused_before_a_statement_is_issued(
    owned: m1.Owned, override: dict[str, object]
) -> None:
    with writer(owned) as w, pytest.raises(StorageError):
        declare(w, **override)
    count = owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_runtime_trigger_declarations"
    ).fetchone()
    assert count == (0,)


def test_cross_workspace_and_missing_references_are_refused(owned: m1.Owned) -> None:
    with (
        writer(owned) as w,
        pytest.raises(StorageError, match="not recorded in this workspace"),
    ):
        declare(w, audit_ref="audit-elsewhere")
    with m43.guarded(owned):
        foreign = transaction_local_telemetry_writer(
            owned.connection, workspace_id=m1.OTHER_WORKSPACE_ID
        )
        with pytest.raises(StorageError, match="not recorded in this workspace"):
            declare(foreign)
    with writer(owned) as w:
        declare(w)
        subscribe(w)
    with writer(owned) as w, pytest.raises(StorageError, match="job_id"):
        observe(w, job_id="job-missing")
    with writer(owned) as w, pytest.raises(StorageError, match="run_id"):
        observe(w, run_id="run-missing")


def test_a_write_outside_the_fence_is_refused(owned: m1.Owned) -> None:
    w = transaction_local_telemetry_writer(owned.connection, workspace_id=WORKSPACE_ID)
    with pytest.raises(sqlite3.DatabaseError):
        declare(w)


def test_a_failed_fenced_transaction_leaves_nothing_behind(owned: m1.Owned) -> None:
    with pytest.raises(StorageError), writer(owned) as w:
        declare(w)
        subscribe(w, "disabled")
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_runtime_trigger_declarations"
    ).fetchone() == (0,)


def test_subscription_transitions_are_validated_numbered_and_replayable(
    owned: m1.Owned,
) -> None:
    with writer(owned) as w:
        declare(w)
        first = subscribe(w, "paused", 1)
        assert (first.subscription_sequence, first.declaration_sequence) == (1, 1)
        assert subscribe(w, "paused", 1) == first
        assert subscribe(w, "active", 2).subscription_sequence == 2
        subscribe(w, "unavailable", 3)
        subscribe(w, "disabled", 4)
    with writer(owned) as w, pytest.raises(StorageError, match="different terms"):
        subscribe(w, "active", 1)
    for state in ("active", "paused", "unavailable"):
        with writer(owned) as w, pytest.raises(StorageError, match="cannot move"):
            subscribe(w, state, 5)


def test_a_subscription_starts_active_or_paused_and_needs_a_declared_trigger(
    owned: m1.Owned,
) -> None:
    with writer(owned) as w, pytest.raises(StorageError, match="not declared"):
        subscribe(w)
    with writer(owned) as w:
        declare(w)
    for state in ("unavailable", "disabled"):
        with (
            writer(owned) as w,
            pytest.raises(StorageError, match="starts active or paused"),
        ):
            subscribe(w, state)
    with writer(owned) as w, pytest.raises(StorageError, match="subscription_state"):
        subscribe(w, "deleted")
    with writer(owned) as w:
        subscribe(w, "active", 1, observed_at_us=BASE + 50)
    with writer(owned) as w, pytest.raises(StorageError, match="must not regress"):
        subscribe(w, "paused", 2, observed_at_us=BASE + 49)


def test_observation_replay_conflict_and_idempotency_equivalence(
    owned: m1.Owned,
) -> None:
    active_trigger(owned)
    with writer(owned) as w:
        first = observe(w)
        assert (first.observation_sequence, first.declaration_sequence) == (1, 1)
        assert observe(w) == first
    with writer(owned) as w, pytest.raises(StorageError, match="different terms"):
        observe(w, event_id="event-other")
    with writer(owned) as w, pytest.raises(StorageError, match="already accepted"):
        observe(w, 2, idempotency_key="key-1")
    with writer(owned) as w, pytest.raises(StorageError, match="different content"):
        observe(
            w,
            2,
            idempotency_key="key-1",
            envelope_digest="sha256:" + "8" * 64,
            delivery_status="duplicate",
            duplicate_of_observation_id=first.trigger_observation_id,
        )
    with (
        writer(owned) as w,
        pytest.raises(StorageError, match="must name the accepted"),
    ):
        observe(
            w,
            2,
            idempotency_key="key-1",
            delivery_status="duplicate",
            duplicate_of_observation_id="obs-nothing",
        )
    with writer(owned) as w:
        duplicate = observe(
            w,
            2,
            idempotency_key="key-1",
            delivery_status="duplicate",
            duplicate_of_observation_id=first.trigger_observation_id,
        )
        assert duplicate.observation_sequence == 2


@pytest.mark.parametrize(
    "override",
    (
        {"delivery_status": "processed"},
        {"delivery_status": "dead_lettered"},
        {"delivery_status": "dead_lettered", "delivery_reason": "unheard_of"},
        {"delivery_status": "accepted", "delivery_reason": "inactive_trigger"},
        {"delivery_status": "uncertain", "delivery_reason": "inactive_trigger"},
        {"delivery_status": "duplicate"},
        {"duplicate_of_observation_id": "obs-1"},
        {
            "delivery_status": "dead_lettered",
            "delivery_reason": "inactive_trigger",
            "run_id": "run-0001",
        },
        {"event_id": "bad id"},
        {"idempotency_key": ""},
        {"envelope_digest": "sha256:ZZ"},
        {"occurred_at_us": -1},
        {"observed_at_us": None},
        {"job_id": "bad id"},
    ),
)
def test_a_malformed_observation_is_refused(
    owned: m1.Owned, override: dict[str, object]
) -> None:
    active_trigger(owned)
    with writer(owned) as w, pytest.raises(StorageError):
        observe(w, **override)
    assert owned.connection.execute(
        "SELECT COUNT(*) FROM omnivia_runtime_trigger_observations"
    ).fetchone() == (0,)


def test_an_observation_needs_a_declared_trigger_and_an_active_subscription(
    owned: m1.Owned,
) -> None:
    with writer(owned) as w, pytest.raises(StorageError, match="not declared"):
        observe(w)
    with writer(owned) as w:
        declare(w)
    with (
        writer(owned) as w,
        pytest.raises(sqlite3.IntegrityError, match="active subscription"),
    ):
        observe(w)


def test_only_an_accepted_observation_holds_its_idempotency_key(owned: m1.Owned) -> None:
    active_trigger(owned)

    def held(key: str) -> object:
        return read_accepted_trigger_observation(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            trigger_id=TRIGGER,
            idempotency_key=key,
        )

    with writer(owned) as w:
        first = observe(w)
        observe(
            w,
            2,
            idempotency_key="key-2",
            delivery_status="dead_lettered",
            delivery_reason="inactive_trigger",
        )
    assert held("key-1") == first
    assert held("key-2") is None
    with writer(owned) as w:
        accepted = observe(w, 3, idempotency_key="key-2")
    assert held("key-2") == accepted
    assert held("key-unknown") is None


# --- reads ---------------------------------------------------------------------------


def test_a_declared_trigger_with_no_history_reads_as_unsubscribed_and_empty(
    owned: m1.Owned,
) -> None:
    with writer(owned) as w:
        declare(w)
    telemetry = read(owned)
    assert telemetry.subscription.state is None
    assert telemetry.last_observation is None
    assert telemetry.delivery_status is None
    assert telemetry.observation_total == 0
    assert telemetry.window == ()
    assert telemetry.uncertainty == ("no_subscription_recorded",)
    assert telemetry.declaration.event_type == m43.EVENT_TYPE


def test_acceptance_alone_never_reads_as_completion(owned: m1.Owned) -> None:
    active_trigger(owned)
    with writer(owned) as w:
        observe(w, occurred_at_us=None)
    telemetry = read(owned)
    view = telemetry.last_observation
    assert telemetry.subscription.state == "active"
    assert telemetry.delivery_status == "accepted"
    assert view.processing == "unlinked"
    assert view.job_state is None and view.run_status is None
    assert set(view.uncertainty) == {"processing_unlinked", "source_time_unknown"}
    assert telemetry.failures == ()
    assert telemetry.delivery_counts["accepted"] == 1


def test_a_linked_job_separates_delivery_from_processing(owned: m1.Owned) -> None:
    active_trigger(owned)
    m18.seed_job(owned)
    with writer(owned) as w:
        observe(w, job_id=m18.JOB_ID)
    assert read(owned).last_observation.processing == "unknown"
    assert "processing_unknown" in read(owned).uncertainty

    job_event(owned, "running", 0, "claimed")
    view = read(owned).last_observation
    assert (view.processing, view.job_state) == ("in_progress", "running")

    job_event(owned, "failed", 1, "failed")
    telemetry = read(owned)
    view = telemetry.last_observation
    assert telemetry.delivery_status == "accepted"
    assert (view.processing, view.job_state) == ("failed", "failed")
    assert [(f.source, f.reason) for f in telemetry.failures] == [("job", "job_failed")]


def test_a_linked_run_is_read_from_its_event_stream(owned: m1.Owned) -> None:
    with writer(owned) as w:
        declare(w)
        subscribe(w)
    m27.seed_runtime_run(owned)
    with m43.guarded(owned):
        m27.insert(owned, m27.RUNS, m27.workflow_run_row())
        m18.insert_event(owned, occurred_at_us=m27.BASE_US)
    with writer(owned) as w:
        observe(w, job_id=m18.JOB_ID, run_id=m18.RUN_ID)
    assert read(owned).last_observation.processing == "pending"
    run_event(owned, "running", 1)
    assert read(owned).last_observation.processing == "in_progress"
    run_event(owned, "succeeded", 2)
    view = read(owned).last_observation
    assert (view.processing, view.run_status) == ("succeeded", "succeeded")


def test_a_job_and_run_that_disagree_are_reported_as_uncertain(owned: m1.Owned) -> None:
    with writer(owned) as w:
        declare(w)
        subscribe(w)
    m27.seed_runtime_run(owned)
    with m43.guarded(owned):
        m27.insert(owned, m27.RUNS, m27.workflow_run_row())
        m18.insert_event(owned, occurred_at_us=m27.BASE_US)
    with writer(owned) as w:
        observe(w, job_id=m18.JOB_ID, run_id=m18.RUN_ID)
    run_event(owned, "running", 1)
    run_event(owned, "failed", 2)
    job_event(owned, "running", 0, "claimed")
    job_event(owned, "succeeded", 1, "succeeded")
    view = read(owned).last_observation
    assert view.processing == "uncertain"
    assert "job_run_disagree" in view.uncertainty


def test_a_job_whose_run_is_not_a_run_of_the_workflow_is_flagged_not_trusted(
    owned: m1.Owned,
) -> None:
    active_trigger(owned)
    m18.seed_admitted_run(owned)
    with writer(owned) as w:
        observe(w, job_id=m18.JOB_ID)
    view = read(owned).last_observation
    assert view.run_status is None
    assert "linked_run_workflow_mismatch" in view.uncertainty


def test_dead_letters_duplicates_and_uncertain_deliveries_keep_their_reasons(
    owned: m1.Owned,
) -> None:
    active_trigger(owned)
    with writer(owned) as w:
        first = observe(w, 1)
        observe(
            w,
            2,
            idempotency_key="key-1",
            delivery_status="duplicate",
            duplicate_of_observation_id=first.trigger_observation_id,
        )
        observe(
            w, 3, delivery_status="dead_lettered", delivery_reason="trigger_cooldown"
        )
        observe(
            w,
            4,
            occurred_at_us=None,
            delivery_status="uncertain",
            delivery_reason="recovery_interrupted",
        )
    telemetry = read(owned)
    assert telemetry.observation_total == 4
    assert telemetry.delivery_status == "uncertain"
    assert dict(telemetry.delivery_counts) == {
        "accepted": 1,
        "duplicate": 1,
        "dead_lettered": 1,
        "uncertain": 1,
    }
    by_id = {v.observation.trigger_observation_id: v for v in telemetry.window}
    assert by_id[f"obs-{TRIGGER}-2"].processing == "not_applicable"
    assert by_id[f"obs-{TRIGGER}-3"].processing == "not_applicable"
    assert by_id[f"obs-{TRIGGER}-4"].processing == "unknown"
    assert [(f.source, f.reason) for f in telemetry.failures] == [
        ("delivery", "trigger_cooldown")
    ]
    assert {"delivery_uncertain", "source_time_unknown"} <= set(telemetry.uncertainty)
    assert [v.observation.observation_sequence for v in telemetry.window] == [
        4,
        3,
        2,
        1,
    ]


def test_an_unavailable_subscription_is_reported_as_uncertain(owned: m1.Owned) -> None:
    active_trigger(owned)
    with writer(owned) as w:
        subscribe(w, "unavailable", 2)
    telemetry = read(owned)
    assert telemetry.subscription.state == "unavailable"
    assert "subscription_unavailable" in telemetry.uncertainty


def test_every_dead_letter_reason_the_store_admits_is_writable(owned: m1.Owned) -> None:
    active_trigger(owned)
    with writer(owned) as w:
        for index, reason in enumerate(DEAD_LETTER_REASONS, start=1):
            observe(w, index, delivery_status="dead_lettered", delivery_reason=reason)
    assert len(read(owned).failures) == len(DEAD_LETTER_REASONS)


# --- bounds and scope ----------------------------------------------------------------


@pytest.mark.parametrize("limit", (0, -1, MAX_OBSERVATION_WINDOW + 1, True, "5"))
def test_an_unbounded_or_malformed_observation_window_is_refused(
    owned: m1.Owned, limit: Any
) -> None:
    with writer(owned) as w:
        declare(w)
    with pytest.raises(StorageError, match="observation_limit"):
        read(owned, observation_limit=limit)


@pytest.mark.parametrize("limit", (0, MAX_TRIGGER_PAGE + 1, False))
def test_an_unbounded_page_is_refused(owned: m1.Owned, limit: Any) -> None:
    with pytest.raises(StorageError, match="limit"):
        list_workflow_trigger_telemetry(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            project_id=PROJECT,
            workflow_id=WORKFLOW,
            limit=limit,
        )
    with pytest.raises(StorageError, match="observation_limit"):
        list_workflow_trigger_telemetry(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            project_id=PROJECT,
            workflow_id=WORKFLOW,
            observation_limit=21,
        )


def test_the_window_is_bounded_but_the_total_is_not_lost(owned: m1.Owned) -> None:
    active_trigger(owned)
    with writer(owned) as w:
        for index in range(1, 8):
            observe(w, index)
    telemetry = read(owned, observation_limit=3)
    assert len(telemetry.window) == 3
    assert telemetry.observation_total == 7
    assert telemetry.last_observation.observation.observation_sequence == 7
    assert sum(telemetry.delivery_counts.values()) == 3


def test_malformed_read_keys_are_refused(owned: m1.Owned) -> None:
    for override in (
        {"trigger_id": "bad id"},
        {"project_id": ""},
        {"workflow_id": "Upper"},
        {"workspace_id": "../x"},
    ):
        with pytest.raises(StorageError):
            read(owned, **override)


def test_another_project_or_workflow_reads_as_absent(owned: m1.Owned) -> None:
    active_trigger(owned)
    with writer(owned) as w:
        observe(w)
    assert read(owned) is not None
    assert read(owned, project_id="project-beta") is None
    assert read(owned, workflow_id="other-workflow") is None
    assert read(owned, trigger_id="trigger-unknown") is None
    assert (
        read_trigger_declaration(
            owned.connection,
            workspace_id=WORKSPACE_ID,
            project_id="project-beta",
            workflow_id=WORKFLOW,
            trigger_id=TRIGGER,
        )
        is None
    )


def test_a_page_lists_only_its_own_workflow_and_resumes_after_a_trigger(
    owned: m1.Owned,
) -> None:
    with writer(owned) as w:
        for name in ("trigger-a", "trigger-b", "trigger-c"):
            declare(w, name)
            subscribe(w, trigger_id=name)
            observe(w, 1, name)
        declare(w, "trigger-other", project_id="project-beta")
    first = list_workflow_trigger_telemetry(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        project_id=PROJECT,
        workflow_id=WORKFLOW,
        limit=2,
    )
    assert [t.declaration.trigger_id for t in first.items] == ["trigger-a", "trigger-b"]
    assert first.next_after_trigger_id == "trigger-b"
    assert all(len(t.window) == 1 for t in first.items)
    second = list_workflow_trigger_telemetry(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        project_id=PROJECT,
        workflow_id=WORKFLOW,
        limit=2,
        after_trigger_id=first.next_after_trigger_id,
    )
    assert [t.declaration.trigger_id for t in second.items] == ["trigger-c"]
    assert second.next_after_trigger_id is None
    elsewhere = list_workflow_trigger_telemetry(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        project_id="project-beta",
        workflow_id=WORKFLOW,
    )
    assert [t.declaration.trigger_id for t in elsewhere.items] == ["trigger-other"]


# --- wait signals --------------------------------------------------------------------


def signal(w: Any, n: int = 1, **overrides: object) -> Any:
    values: dict[str, object] = {
        "wait_signal_observation_id": f"wsig-{n}",
        "wait_id": m18.WAIT_ID,
        "event_id": f"signal-{n}",
        "envelope_digest": m43.DIGEST_ENVELOPE,
        "occurred_at_us": None,
        "observed_at_us": m18.BASE_US + 10 + n,
        "delivery_status": "accepted",
        "audit_ref": m18.audit_ref_for(m18.JOB_ID),
    }
    values.update(overrides)
    return w.record_wait_signal(**values)


def wait_read(holder: m1.Owned, **overrides: Any) -> Any:
    args: dict[str, Any] = {"workspace_id": WORKSPACE_ID, "wait_id": m18.WAIT_ID}
    args.update(overrides)
    return read_wait_signal_telemetry(holder.connection, **args)


def test_an_accepted_signal_is_delivery_not_resolution(owned: m1.Owned) -> None:
    m43.seed_signal_wait(owned)
    assert wait_read(owned).wait_status == "pending"
    assert wait_read(owned).window == ()
    with writer(owned) as w:
        first = signal(w)
        assert signal(w) == first
    telemetry = wait_read(owned)
    assert telemetry.delivery_status == "accepted"
    assert telemetry.wait_status == "pending"
    assert telemetry.run_status == "admitted"
    assert set(telemetry.uncertainty) == {
        "resolution_not_recorded",
        "source_time_unknown",
    }
    with m43.guarded(owned):
        m18.insert_wait_resolution(owned)
    resolved = wait_read(owned)
    assert resolved.wait_status == "resolved"
    assert "resolution_not_recorded" not in resolved.uncertainty


def test_signal_replay_equivalence_and_failures(owned: m1.Owned) -> None:
    m43.seed_signal_wait(owned)
    with writer(owned) as w:
        accepted = signal(w)
    with writer(owned) as w, pytest.raises(StorageError, match="different terms"):
        signal(w, event_id="signal-other")
    with writer(owned) as w, pytest.raises(StorageError, match="already accepted"):
        signal(w, 2)
    with writer(owned) as w, pytest.raises(StorageError, match="unchanged"):
        signal(
            w,
            2,
            delivery_status="duplicate",
            duplicate_of_observation_id=accepted.wait_signal_observation_id,
            event_id=accepted.event_id,
            envelope_digest="sha256:" + "7" * 64,
        )
    with writer(owned) as w:
        signal(
            w,
            2,
            delivery_status="duplicate",
            duplicate_of_observation_id=accepted.wait_signal_observation_id,
            event_id=accepted.event_id,
        )
        signal(
            w,
            3,
            event_id="signal-late",
            delivery_status="dead_lettered",
            delivery_reason="wait_already_resolved",
        )
        signal(
            w,
            4,
            delivery_status="uncertain",
            delivery_reason="delivery_unconfirmed",
        )
    telemetry = wait_read(owned)
    assert telemetry.observation_total == 4
    assert dict(telemetry.delivery_counts) == {
        "accepted": 1,
        "duplicate": 1,
        "dead_lettered": 1,
        "uncertain": 1,
    }
    assert "delivery_uncertain" in telemetry.uncertainty
    assert len(wait_read(owned, observation_limit=2).window) == 2


def test_only_external_signal_waits_are_recorded_or_read(owned: m1.Owned) -> None:
    m43.seed_signal_wait(owned, kind="approval")
    with writer(owned) as w, pytest.raises(StorageError, match="external_signal"):
        signal(w)
    assert wait_read(owned) is None
    assert wait_read(owned, wait_id="wait-unknown") is None
    with writer(owned) as w, pytest.raises(StorageError, match="not recorded"):
        signal(w, wait_id="wait-unknown")
    for override in (
        {"observation_limit": 0},
        {"observation_limit": 101},
        {"wait_id": "bad id"},
    ):
        with pytest.raises(StorageError):
            wait_read(owned, **override)


def test_the_store_restates_the_contract_vocabularies_exactly() -> None:
    """The store's copies of the trigger vocabularies equal the contract's, so they cannot drift.

    The store restates 0043's CHECK domains for its own refusals. A value added to one copy and
    not the other would be refused by one reader and accepted by the other.
    """
    assert store.TRIGGER_KINDS == contract.TRIGGER_KINDS
    assert store.SUBSCRIPTION_STATES == contract.TRIGGER_SUBSCRIPTION_STATES
    assert store.DELIVERY_STATUSES == contract.TRIGGER_DELIVERY_STATUSES
