"""C06 acceptance evidence for the registered Workflow task commands.

Focused tests for the three C06 plan steps the accepted tree had no test standing
behind. Everything else in the plan is already held by an existing node id and is
cited in the C06 evidence table rather than restated here; duplicating a passing
assertion would add a second place to keep it true and prove nothing new.

*Which entrypoints actually mount these two mutations.* The plan assumed two --
the MCP allow-list and the CLI surface. The accepted tree mounts one: `manifest.py`
names three admissible mutations and neither Workflow mutation is among them, so a
model cannot reach `workflow.start` or `workflow.control` through any profile. That
is a deliberate exposure decision rather than a missing wiring, and this pins it, so
a later build that quietly adds a Workflow tool has to edit a test that says why.

*A stale fence writes nothing through `workflow.control` either.* T-0693 holds that
for `workflow.start`. The plan asks for the `control` case, and it is a different
path: `cancel` reads the run, takes a grant and writes through the stop ledger, and
`resolve_wait` goes out to the runtime wait authority and writes through RT-107's
own transaction. Both are covered here, because a lost lease must refuse both.

*One wait resolution is one transaction.* The wait's terminal row, the step's
resumption, the run event that moves the projection and the operation's own audit
and idempotency receipt are counted together across a single dispatch, so a build
that started committing the resolution without the receipt -- or the receipt without
the resolution -- fails here rather than at the next replay.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import test_application_audit_idempotency_migration as m1
import test_t0693_workflow_application as app
from omnivia_core_cli.surface import APPLICATION_COMMANDS
from omnivia_core_mcp.manifest import (
    ADMITTED_MUTATIONS,
    PROFILES,
    exposure_manifest,
)
from omnivia_core_runtime.ownership.fencing import StaleGeneration
from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.ownership.lease import acquire_lease
from omnivia_core_runtime.service.handlers.workflow import (
    WORKFLOW_CONTROL_OPERATION,
    WORKFLOW_FAMILY_OPERATIONS,
    WORKFLOW_START_OPERATION,
)
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline

WORKSPACE_ID = app.WORKSPACE_ID
WALL = app.WALL

#: The tables one accepted `resolve_wait` must move, and the two receipt tables the
#: mutation seam writes beside them. Counted rather than inspected: what is under test
#: is that they move *together*, which a count states and a field-by-field read does not.
WAIT_RESOLUTIONS = "omnivia_runtime_wait_resolutions"
RUNTIME_EVENTS = "omnivia_runtime_events"
STEP_STATES = "omnivia_runtime_run_step_states"
IDEMPOTENCY_CLAIMS = "omnivia_idempotency_claims"
IDEMPOTENCY_OUTCOMES = "omnivia_idempotency_outcomes"


def _step_status(owned: m1.Owned, run_step_id: str) -> str:
    """The status a step currently holds, which is the last one observed of it.

    0018 keeps a step's status as an append-only history rather than a column, so
    "is it running again" is a question about the newest entry.
    """
    row = owned.connection.execute(
        f"SELECT status FROM {STEP_STATES} WHERE run_step_id = ? "
        "ORDER BY state_sequence DESC LIMIT 1",
        (run_step_id,),
    ).fetchone()
    assert row is not None, run_step_id
    return str(row[0])


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    """The same migrated, owned workspace the T-0693 suite drives."""
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


# --- step 1: what is actually mounted ----------------------------------------------


def test_c06_acceptance_the_cli_surface_is_the_only_mounted_entrypoint(
    owned: m1.Owned,
) -> None:
    """One mounted entrypoint for both Workflow mutations, and it is the CLI.

    Three statements, and the third is the one the plan's premise turned on: the CLI
    names both commands and the operation each reaches; the registered family serves
    exactly the four Workflow operations and nothing else; and the MCP allow-list
    admits neither mutation under either profile.
    """
    commands = {
        command.operation: command
        for command in APPLICATION_COMMANDS
        if command.operation in WORKFLOW_FAMILY_OPERATIONS
    }
    assert commands[WORKFLOW_START_OPERATION].path == ("workflow", "start")
    assert commands[WORKFLOW_START_OPERATION].purpose == "workflow_execution"
    assert commands[WORKFLOW_CONTROL_OPERATION].path == ("workflow", "control")
    assert commands[WORKFLOW_CONTROL_OPERATION].purpose == "workflow_control"

    served = app.dispatcher(owned, releases=(app.release(),))
    assert set(served.registry.operations) == set(WORKFLOW_FAMILY_OPERATIONS)

    for profile in PROFILES:
        exposed = {entry.operation for entry in exposure_manifest(profile)}
        assert exposed.isdisjoint(WORKFLOW_FAMILY_OPERATIONS), profile
    assert ADMITTED_MUTATIONS.isdisjoint(WORKFLOW_FAMILY_OPERATIONS)


# --- step 5: a stale fence, through `workflow.control` ------------------------------


def _supersede(owned: m1.Owned, *, instance: str) -> None:
    """A takeover: another service instance acquires the lease and the generation moves."""
    taken = acquire_lease(
        owned.connection,
        m1.make_identity(instance, pid=4545),
        clock=FakeClock(),
        workspace_id=WORKSPACE_ID,
        holds_storage_lock=True,
        lock_mechanism="flock",
    )
    assert taken.fencing_generation > owned.generation


def _ledger(owned: m1.Owned) -> tuple[int, ...]:
    """Every table one `workflow.control` may write, as a count of its rows."""
    return tuple(
        app.count(owned, table)
        for table in (
            WAIT_RESOLUTIONS,
            RUNTIME_EVENTS,
            STEP_STATES,
            IDEMPOTENCY_CLAIMS,
            IDEMPOTENCY_OUTCOMES,
            app.STOP_REQUESTS,
            app.STOP_OUTCOMES,
        )
    )


def test_c06_acceptance_a_cancel_under_a_superseded_generation_writes_nothing(
    owned: m1.Owned,
) -> None:
    """A cancellation from an instance that has lost its lease records no stop.

    The run is started while this instance still holds authority, so what is under
    test is authority lost *between* the two requests -- which is the shape a takeover
    actually has, and the one that would otherwise leave a stop request behind over a
    run this instance no longer owns.
    """
    served = app.dispatcher(owned, releases=(app.release(),))
    run_id = app.run_id_of(app.start(served))
    before = _ledger(owned)
    _supersede(owned, instance="svc-c06-cancel-successor")

    with pytest.raises(StaleGeneration):
        served.dispatch(
            app.request(
                WORKFLOW_CONTROL_OPERATION,
                {"run_id": run_id, "action": "cancel", "reason": "operator.cancelled"},
                request_id="req-c06-cancel-stale",
                idempotency_key="idem-c06-cancel-stale",
            )
        )

    assert _ledger(owned) == before


def test_c06_acceptance_a_wait_resolution_under_a_superseded_generation_writes_nothing(
    owned: m1.Owned,
) -> None:
    """The wait authority's own transaction refuses a lost lease too.

    A different path from the cancellation above: `resolve_wait` hands the command to
    RT-107, which opens its own fenced transaction. The refusal has to come from there
    rather than from this handler, and the wait has to be left pending.
    """
    clock = FakeClock(wall=WALL)
    served = app.dispatcher(
        owned,
        releases=(app.release(),),
        wait_policy=lambda *args, **kwargs: None,
        clock=clock,
    )
    run_id = app.run_id_of(app.start(served))
    run_step_id = app._suspend(owned, run_id)
    clock.advance_wall(1.0)
    before = _ledger(owned)
    _supersede(owned, instance="svc-c06-resolve-successor")

    with pytest.raises(StaleGeneration):
        served.dispatch(
            app.request(
                WORKFLOW_CONTROL_OPERATION,
                {
                    "run_id": run_id,
                    "action": "resolve_wait",
                    "wait_id": "wait-t0693-1",
                    "resolution": "external_signal",
                    "reason": "operator.resolved",
                },
                request_id="req-c06-resolve-stale",
                idempotency_key="idem-c06-resolve-stale",
            )
        )

    # The wait is still pending, which 0018 states as the absence of a resolution row
    # rather than as a column, and the step it holds is still waiting.
    assert _ledger(owned) == before
    assert _step_status(owned, run_step_id) == "waiting"


# --- step 6: one resolution, one transaction ----------------------------------------


def test_c06_acceptance_one_wait_resolution_commits_state_event_and_receipt_together(
    owned: m1.Owned,
) -> None:
    """The wait's terminal row, the step's resumption, the run event and the receipt.

    All four are written by one dispatch, so each is counted before and after it. The
    step transition is read as a status rather than counted, because a step accumulates
    observations and the property is which one it now holds.

    The pairing that matters is the last two: `workflow.control` serves its answer out
    of the bytes the mutation seam stored, so a resolution committed without its
    idempotency outcome would replay as a second settlement of a wait that is no longer
    pending -- which is the `conflict` this operation is meant never to reach for a
    caller who simply retried.
    """
    clock = FakeClock(wall=WALL)
    served = app.dispatcher(
        owned,
        releases=(app.release(),),
        wait_policy=lambda *args, **kwargs: None,
        clock=clock,
    )
    run_id = app.run_id_of(app.start(served))
    run_step_id = app._suspend(owned, run_id)
    clock.advance_wall(1.0)
    before = _ledger(owned)

    answer = app.result(
        served.dispatch(
            app.request(
                WORKFLOW_CONTROL_OPERATION,
                {
                    "run_id": run_id,
                    "action": "resolve_wait",
                    "wait_id": "wait-t0693-1",
                    "resolution": "external_signal",
                    "reason": "operator.resolved",
                },
                request_id="req-c06-resolve-atomic",
                idempotency_key="idem-c06-resolve-atomic",
            )
        )
    )
    after = _ledger(owned)

    assert answer["disposition"] == "wait_resolved"
    moved = tuple(after[index] - before[index] for index in range(len(before)))
    # One resolution, one step transition, one run event, one claim and the one stored
    # outcome that answers its replay -- and nothing at all in the stop ledger, which
    # this action never touches.
    resolutions, events, transitions, claims, outcomes, stops, stop_outcomes = moved
    assert (resolutions, transitions, events, claims, outcomes) == (1, 1, 1, 1, 1)
    assert (stops, stop_outcomes) == (0, 0)
    assert owned.connection.execute(
        "SELECT status FROM omnivia_runtime_wait_resolutions WHERE wait_id = ?",
        ("wait-t0693-1",),
    ).fetchone() == ("resolved",)
    assert _step_status(owned, run_step_id) == "running"

    # The receipt is the replay, so the operation is asked again under its own key: the
    # same bytes come back and none of the four counts moves.
    again = app.result(
        served.dispatch(
            app.request(
                WORKFLOW_CONTROL_OPERATION,
                {
                    "run_id": run_id,
                    "action": "resolve_wait",
                    "wait_id": "wait-t0693-1",
                    "resolution": "external_signal",
                    "reason": "operator.resolved",
                },
                request_id="req-c06-resolve-atomic",
                idempotency_key="idem-c06-resolve-atomic",
            )
        )
    )
    assert again == answer
    assert _ledger(owned) == after
