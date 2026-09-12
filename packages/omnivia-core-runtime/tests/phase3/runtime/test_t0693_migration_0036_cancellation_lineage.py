"""T-0693 acceptance for migration 0036's `workflow.control` cancellation lineage.

0015 admits a `cancelled` terminal observation only behind an accepted `job.cancel`
control. `workflow.control` is a second public authority over the same durable job and
is not that operation, so under 0015 alone a cancelled Workflow Run had to be left
beside a queued or claimed job -- a half-written history RT-109 reads as contradictory.
0036 replaces that one trigger so the schema recognises the lineage the cancellation
genuinely has: an accepted stop in migration 0025's ledger, for a `workflow` run of this
workspace carried by this job, requested and settled under `workflow.control` audits.

What this file holds to.

*It is a trigger replacement and nothing else.* One `DROP TRIGGER` and one
`CREATE TRIGGER` of the same name, no DML, and a schema whose tables, indexes and
trigger names are byte-for-byte the set 0035's head already had.

*It restates 0015 rather than rewriting it.* Every refusal of the 0015 definition is
compared statement by statement against the 0036 one, so a copy that dropped, reordered
or quietly edited a rule fails here rather than in whichever operation met it first.

*The widened branch requires every part of the lineage.* The settlement written here is
the whole one the widened branch names -- an accepted stop, its request, the idempotency
claim whose `claim_id` is that stop, and the `workflow.control` audit all three carry,
each in the shape the mutation seam writes it -- and every part of it is then broken on
its own: an audit for another operation, under another purpose, or by another principal;
a claim that is absent, made for another operation, or claimed by another principal; a
request and an outcome that name different audits or settle different reasons; a
`rejected` outcome; a reason that is not the one the observation records; a stop
belonging to another job's run; and a request, outcome or audit whose instant is not the
observation's, an entire settlement one instant early or late included.

*The legacy lineage is untouched.* A `job.cancel` cancellation is admitted on exactly
the terms it always was, and the invariants that never concerned cancellation --
contiguity, the scheduler/final-event agreement, the attempt history, the guard
preamble -- still refuse.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_t0693_workflow_application as app
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.service.handlers.workflow import WORKFLOW_CONTROL_OPERATION
from omnivia_core_runtime.service.mutation import WORKFLOW_CONTROL_PURPOSE
from omnivia_core_runtime.storage.agent_runtime import (
    read_run_sequence,
    transaction_local_writer,
)
from omnivia_core_runtime.storage.migrations import (
    Migration,
    applied_migrations,
    load_migrations,
    materialise_phase0_baseline,
)

MIGRATION_VERSION = 36
PREDECESSOR_VERSION = 35
MIGRATION_NAME = "0036_workflow_control_cancellation_lineage.sql"
PREDECESSOR_NAME = "0035_t0688_workflow_runtime_hardening.sql"
DEFINING_MIGRATION_NAME = "0015_application_job_bridges.sql"

TRIGGER = "omnivia_guard_job_terminal_observations_insert"
TERMINALS = "omnivia_job_terminal_observations"
CONTROLS = "omnivia_application_job_controls"
AUDITS = "omnivia_application_audit_events"
CLAIMS = "omnivia_idempotency_claims"
STOP_REQUESTS = "omnivia_runtime_stop_requests"
STOP_OUTCOMES = "omnivia_runtime_stop_outcomes"

WORKSPACE_ID = app.WORKSPACE_ID
WALL_US = app.WALL_US

#: The operation, purpose and reason one served `workflow.control` cancellation records,
#: taken from the production constants rather than restated, so a lane that renamed
#: either would fail here rather than keep passing against a copy.
CONTROL_OPERATION = WORKFLOW_CONTROL_OPERATION
CONTROL_PURPOSE = WORKFLOW_CONTROL_PURPOSE
CANCELLED_REASON = "operator.cancelled"

CANCELLED_CONTROL_MESSAGE = (
    "omnivia: cancelled observation has no accepted cancellation control"
)

#: Every statement of a guard trigger begins at exactly this indentation, so splitting
#: on it recovers the refusals one by one without parsing SQL.
_STATEMENT = re.compile(r"^    SELECT RAISE\(ABORT", re.MULTILINE)


def _migration(version: int) -> Migration:
    found = [m for m in load_migrations() if m.version == version]
    assert len(found) == 1, [m.name for m in load_migrations()]
    return found[0]


MIGRATION = _migration(MIGRATION_VERSION)


# --- reading the two definitions of the one trigger ---------------------------------


def _trigger_definition(sql: str) -> str:
    """The body of the last `CREATE TRIGGER <TRIGGER>` in one migration's text."""
    marker = f"CREATE TRIGGER IF NOT EXISTS {TRIGGER}\n"
    plain = f"CREATE TRIGGER {TRIGGER}\n"
    start = sql.rfind(marker)
    if start < 0:
        start = sql.rfind(plain)
    assert start >= 0, TRIGGER
    end = sql.index("\nEND;", start)
    return sql[start:end]


def _refusals(definition: str) -> list[str]:
    """One trigger body, as its refusals, whitespace-normalised."""
    bounds = [match.start() for match in _STATEMENT.finditer(definition)]
    assert bounds, definition[:200]
    pieces = [
        definition[start:stop]
        for start, stop in zip(bounds, [*bounds[1:], len(definition)], strict=True)
    ]
    return [" ".join(piece.split()).rstrip(";") for piece in pieces]


def _message(refusal: str) -> str:
    return refusal.split("'")[1]


DEFINED_BY_0015 = _refusals(
    _trigger_definition(
        next(m for m in load_migrations() if m.name == DEFINING_MIGRATION_NAME).sql
    )
)
DEFINED_BY_0036 = _refusals(_trigger_definition(MIGRATION.sql))


# --- one owned workspace at head, with a started Workflow Run -----------------------


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


def guarded(holder: m1.Owned) -> Any:
    return fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    )


def insert(
    connection: sqlite3.Connection, table: str, values: dict[str, object]
) -> None:
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(values.values())
    )


def started(holder: m1.Owned, **overrides: Any) -> str:
    """One Workflow Run of this workspace, queued and never claimed."""
    overrides.setdefault("releases", (app.release(),))
    return app.run_id_of(app.start(app.dispatcher(holder, **overrides)))


def audit(
    holder: m1.Owned,
    *,
    audit_ref: str,
    at_us: int,
    operation: str = CONTROL_OPERATION,
    purpose: str = CONTROL_PURPOSE,
    principal: str = app.PRINCIPAL,
) -> None:
    """One `succeeded` audit event, in the shape the mutation seam records one."""
    insert(
        holder.connection,
        AUDITS,
        m1.row_for(
            AUDITS,
            audit_ref=audit_ref,
            principal_id=principal,
            operation=operation,
            purpose=purpose,
            request_id=f"request-{audit_ref}",
            correlation_id=f"correlation-{audit_ref}",
            trace_id=f"trace-{audit_ref}",
            outcome_class="succeeded",
            error_code=None,
            recorded_at_us=at_us,
        ),
    )


def settled_stop(
    holder: m1.Owned,
    run_id: str,
    *,
    stop_request_id: str = "stop-1",
    at_us: int,
    audit_operation: str = CONTROL_OPERATION,
    purpose: str = CONTROL_PURPOSE,
    principal: str = app.PRINCIPAL,
    claim_operation: str | None = CONTROL_OPERATION,
    claim_principal: str = app.PRINCIPAL,
    outcome: str = "accepted",
    reason: str = CANCELLED_REASON,
    outcome_reason: str | None = None,
    settled_by_another_audit: bool = False,
    audit_at_us: int | None = None,
    requested_at_us: int | None = None,
    completed_at_us: int | None = None,
) -> None:
    """The whole lineage a served `workflow.control` cancel leaves behind, by hand.

    Every part is written the way the mutation seam writes it -- the `workflow.control`
    audit recorded as `succeeded`, the idempotency claim whose `claim_id` is the stop
    request, the request and the accepted outcome that both name that audit -- and
    every part has a keyword here, so one of them at a time can be wrong. The
    canonical `cancelled` event is appended through the ordinary runtime writer,
    because 0025 will only admit an `accepted` outcome that names one.

    Hand-written rather than dispatched, inside one fenced transaction, because the
    served operation is incapable of producing the mismatches under test.
    """
    audit_ref = f"aud-{stop_request_id}"
    recorded_at_us = at_us if audit_at_us is None else audit_at_us
    with guarded(holder):
        audit(
            holder,
            audit_ref=audit_ref,
            at_us=recorded_at_us,
            operation=audit_operation,
            purpose=purpose,
            principal=principal,
        )
        if claim_operation is not None:
            insert(
                holder.connection,
                CLAIMS,
                m1.row_for(
                    CLAIMS,
                    claim_id=stop_request_id,
                    principal_id=claim_principal,
                    operation=claim_operation,
                    idempotency_key=f"idem-{stop_request_id}",
                    audit_ref=audit_ref,
                    claimed_at_us=recorded_at_us,
                ),
            )
        insert(
            holder.connection,
            STOP_REQUESTS,
            {
                "workspace_id": WORKSPACE_ID,
                "stop_request_id": stop_request_id,
                "run_id": run_id,
                "requested_at_us": at_us if requested_at_us is None else requested_at_us,
                "requested_by": app.PRINCIPAL,
                "reason": reason,
                "audit_ref": audit_ref,
            },
        )
        sequence: int | None = None
        if outcome == "accepted":
            sequence = transaction_local_writer(
                holder.connection, workspace_id=WORKSPACE_ID
            ).append_run_event(
                run_id=run_id,
                runtime_event_id=f"rtev-{stop_request_id}",
                occurred_at_us=at_us,
                event_kind="run.cancelled",
                run_status="cancelled",
                message=reason,
            )
        settled_ref = audit_ref
        if settled_by_another_audit:
            settled_ref = f"{audit_ref}-second"
            audit(
                holder,
                audit_ref=settled_ref,
                at_us=recorded_at_us,
                operation=audit_operation,
                purpose=purpose,
                principal=principal,
            )
        insert(
            holder.connection,
            STOP_OUTCOMES,
            {
                "workspace_id": WORKSPACE_ID,
                "stop_request_id": stop_request_id,
                "outcome": outcome,
                "completed_at_us": (
                    at_us if completed_at_us is None else completed_at_us
                ),
                "runtime_event_sequence": sequence,
                "reason": reason if outcome_reason is None else outcome_reason,
                "audit_ref": settled_ref,
            },
        )


def cancel_the_job(
    holder: m1.Owned,
    job_id: str,
    *,
    at_us: int,
    observation_job_id: str | None = None,
    attempt_number: int | None = None,
    cancellation_reason: str = CANCELLED_REASON,
) -> None:
    """The scheduler row, event and observation a cancellation settles, by hand.

    `observation_job_id` lets a test offer one job's lineage for another job's
    observation, which is the cross-linking the widened branch must refuse.
    """
    with guarded(holder):
        holder.connection.execute(
            "UPDATE omnivia_durable_jobs SET state = 'cancelled', updated_at = ? "
            "WHERE job_id = ?",
            ("2026-09-03T00:00:01Z", job_id),
        )
        insert(
            holder.connection,
            "omnivia_job_events",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": job_id,
                "sequence": holder.connection.execute(
                    "SELECT COALESCE(MAX(sequence), -1) + 1 FROM omnivia_job_events "
                    "WHERE workspace_id = ? AND job_id = ?",
                    (WORKSPACE_ID, job_id),
                ).fetchone()[0],
                "occurred_at_us": at_us,
                "state": "cancelled",
                "message": "cancelled",
                "details_json": "{}",
            },
        )
        insert(
            holder.connection,
            TERMINALS,
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": observation_job_id or job_id,
                "terminal_observation_number": 1,
                "attempt_number": attempt_number,
                "terminal_state": "cancelled",
                "finished_at_us": at_us,
                "result_kind": None,
                "result_json": None,
                "error_json": None,
                "cancellation_reason": cancellation_reason,
                "provenance_kind": "service_committed",
                "fencing_generation": holder.generation,
            },
        )


# --- what 0036 is, as a migration ---------------------------------------------------


def test_0036_is_the_unique_consecutive_successor_to_0035() -> None:
    migrations = load_migrations()
    versions = [migration.version for migration in migrations]
    assert versions == sorted(versions)
    assert versions == list(range(1, MIGRATION_VERSION + 1))
    assert [m.name for m in migrations if m.version == MIGRATION_VERSION] == [
        MIGRATION_NAME
    ]
    assert _migration(PREDECESSOR_VERSION).name == PREDECESSOR_NAME


def test_the_ledger_records_this_exact_migration_text(owned: m1.Owned) -> None:
    recorded = applied_migrations(owned.connection)
    assert recorded[MIGRATION_VERSION] == MIGRATION.checksum
    assert (
        hashlib.sha256(MIGRATION.sql.encode("utf-8")).hexdigest() == MIGRATION.checksum
    )


def test_0036_replaces_one_trigger_writes_no_row_and_rebuilds_nothing_else() -> None:
    """One `DROP TRIGGER`, one `CREATE TRIGGER`, the same name, and no DML."""
    body = MIGRATION.sql
    assert body.count("DROP TRIGGER") == 1
    assert f"DROP TRIGGER {TRIGGER};" in body
    assert body.count("CREATE TRIGGER") == 1
    assert f"CREATE TRIGGER {TRIGGER}\n" in body
    for forbidden in ("ALTER TABLE", "CREATE TABLE", "CREATE INDEX", "DROP TABLE",
                      "DROP INDEX", "INSERT INTO", "DELETE FROM"):
        assert forbidden not in body, forbidden
    assert re.search(r"\bUPDATE\s+\w+\s+SET\b", body) is None


def test_the_schema_names_exactly_what_0035_head_already_named(
    tmp_path: Path,
) -> None:
    """0036 adds no object and removes none; only one trigger's text changes."""
    def named(path: Path) -> dict[str, set[str]]:
        holder = m1.take_ownership(path)
        try:
            return {
                kind: {
                    str(row[0])
                    for row in holder.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
                    )
                }
                for kind in ("table", "index", "trigger")
            }
        finally:
            holder.connection.close()

    at_35 = tmp_path / "at-35.sqlite"
    materialise_phase0_baseline(at_35)
    with m1.migration_catalogue_through(PREDECESSOR_VERSION):
        m1.bootstrap_and_migrate(at_35, workspace_id=WORKSPACE_ID)
        before = named(at_35)

    at_head = tmp_path / "at-head.sqlite"
    materialise_phase0_baseline(at_head)
    m1.bootstrap_and_migrate(at_head, workspace_id=WORKSPACE_ID)

    assert named(at_head) == before
    assert TRIGGER in before["trigger"]


def test_0036_restates_every_0015_refusal_and_widens_exactly_one() -> None:
    """Statement by statement, so a copy that edited a second rule fails here."""
    assert len(DEFINED_BY_0015) == 9
    assert [_message(rule) for rule in DEFINED_BY_0036] == [
        _message(rule) for rule in DEFINED_BY_0015
    ]
    widened = []
    for old, new in zip(DEFINED_BY_0015, DEFINED_BY_0036, strict=True):
        if old == new:
            continue
        widened.append(_message(new))
        assert new.startswith(old), _message(new)
        assert "omnivia_runtime_stop_outcomes" in new
        assert "'workflow.control'" in new
    assert widened == [CANCELLED_CONTROL_MESSAGE]


# --- the widened branch: every part of the lineage is required ----------------------


def test_a_workflow_control_cancellation_admits_its_terminal_observation(
    owned: m1.Owned,
) -> None:
    """The positive control: the exact lineage 0036 was added for is accepted."""
    run_id = started(owned)
    settled_stop(owned, run_id, stop_request_id="stop-ok", at_us=WALL_US + 10)

    cancel_the_job(owned, run_id, at_us=WALL_US + 10)

    assert owned.connection.execute(
        f"SELECT terminal_state, attempt_number, cancellation_reason FROM {TERMINALS}"
    ).fetchall() == [("cancelled", None, "operator.cancelled")]
    assert owned.connection.execute(f"SELECT COUNT(*) FROM {CONTROLS}").fetchone() == (
        0,
    )


def test_a_cancellation_with_no_stop_ledger_entry_at_all_is_refused(
    owned: m1.Owned,
) -> None:
    run_id = started(owned)

    with pytest.raises(sqlite3.IntegrityError, match=CANCELLED_CONTROL_MESSAGE):
        cancel_the_job(owned, run_id, at_us=WALL_US + 10)

    assert owned.connection.execute(f"SELECT COUNT(*) FROM {TERMINALS}").fetchone() == (
        0,
    )


#: One broken part of the lineage each, over the settlement the positive control uses:
#: everything else about each of these is the lineage 0036 admits, so the refusal is
#: attributable to the single clause the case breaks.
BROKEN = {
    "audit-for-another-operation": {"audit_operation": "job.cancel"},
    "audit-for-an-unrelated-operation": {"audit_operation": "import.start"},
    "audit-under-another-purpose": {"purpose": "job.control"},
    "audit-by-another-principal": {"principal": "someone-else"},
    "outcome-not-accepted": {"outcome": "rejected"},
    "outcome-names-another-audit": {"settled_by_another_audit": True},
    "outcome-settles-another-reason": {"outcome_reason": "operator.superseded"},
    "reason-the-observation-does-not-record": {"reason": "operator.superseded"},
    "no-claim-at-all": {"claim_operation": None},
    "claim-for-another-operation": {"claim_operation": "job.cancel"},
    "claim-by-another-principal": {"claim_principal": "someone-else"},
    "audit-recorded-at-another-instant": {"audit_at_us": WALL_US + 11},
    "requested-at-another-instant": {"requested_at_us": WALL_US + 9},
    "completed-at-another-instant": {"completed_at_us": WALL_US + 11},
    "a-stale-earlier-settled-stop": {"at_us": WALL_US + 5},
    "a-stop-settled-after-the-observation": {"at_us": WALL_US + 20},
}


@pytest.mark.parametrize("broken", BROKEN.values(), ids=BROKEN.keys())
def test_a_lineage_missing_any_one_part_is_refused(
    owned: m1.Owned, broken: dict[str, Any]
) -> None:
    """Every clause of the widened branch is required, and each is required alone."""
    run_id = started(owned)
    settled_stop(
        owned,
        run_id,
        stop_request_id="stop-broken",
        **{"at_us": WALL_US + 10, **broken},
    )

    with pytest.raises(sqlite3.IntegrityError, match=CANCELLED_CONTROL_MESSAGE):
        cancel_the_job(owned, run_id, at_us=WALL_US + 10)

    assert owned.connection.execute(f"SELECT COUNT(*) FROM {TERMINALS}").fetchone() == (
        0,
    )


def test_one_runs_accepted_stop_does_not_cancel_another_runs_job(
    owned: m1.Owned,
) -> None:
    """The stop must name the run *this* job carries, not merely some cancelled run."""
    cancelled = started(owned)
    other = app.run_id_of(
        app.start(
            app.dispatcher(owned, tag="second", releases=(app.release(),)),
            request_id="req-start-2",
            idempotency_key="idem-start-2",
        )
    )
    settled_stop(owned, cancelled, stop_request_id="stop-other", at_us=WALL_US + 10)

    with pytest.raises(sqlite3.IntegrityError, match=CANCELLED_CONTROL_MESSAGE):
        cancel_the_job(owned, other, at_us=WALL_US + 10)

    assert owned.connection.execute(f"SELECT COUNT(*) FROM {TERMINALS}").fetchone() == (
        0,
    )


# --- the invariants 0036 carried over unchanged -------------------------------------


def test_the_legacy_job_cancel_lineage_is_admitted_exactly_as_before(
    owned: m1.Owned,
) -> None:
    """No stop ledger anywhere: a `job.cancel` control is still the whole authority."""
    run_id = started(owned)
    at_us = WALL_US + 10
    with guarded(owned):
        audit(owned, audit_ref="audit-cancel", operation="job.cancel", at_us=at_us)
        # `cancellation_requested` is recorded against the queued job, exactly as
        # `job.cancel` records it, and the scheduler row moves afterwards.
        insert(
            owned.connection,
            CONTROLS,
            {
                "workspace_id": WORKSPACE_ID,
                "control_id": "control-cancel",
                "job_id": run_id,
                "control_kind": "user",
                "operation": "job.cancel",
                "disposition": "cancellation_requested",
                "source_state": "queued",
                "resulting_state": "queued",
                "source_terminal_observation_number": None,
                "audit_ref": "audit-cancel",
                "fencing_generation": owned.generation,
                "control_json": '{"job_id":"job"}',
                "control_digest": "sha256:" + "0" * 64,
                "control_byte_length": len('{"job_id":"job"}'),
                "settled_at_us": at_us,
            },
        )
        owned.connection.execute(
            "UPDATE omnivia_durable_jobs SET state = 'cancelled', updated_at = ? "
            "WHERE job_id = ?",
            ("2026-09-03T00:00:01Z", run_id),
        )
        insert(
            owned.connection,
            "omnivia_job_events",
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": run_id,
                "sequence": 1,
                "occurred_at_us": at_us,
                "state": "cancelled",
                "message": "cancelled",
                "details_json": "{}",
            },
        )
        insert(
            owned.connection,
            TERMINALS,
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": run_id,
                "terminal_observation_number": 1,
                "attempt_number": None,
                "terminal_state": "cancelled",
                "finished_at_us": at_us,
                "result_kind": None,
                "result_json": None,
                "error_json": None,
                "cancellation_reason": "requested",
                "provenance_kind": "service_committed",
                "fencing_generation": owned.generation,
            },
        )

    assert owned.connection.execute(
        f"SELECT terminal_state, cancellation_reason FROM {TERMINALS}"
    ).fetchall() == [("cancelled", "requested")]
    assert owned.connection.execute(
        f"SELECT COUNT(*) FROM {STOP_REQUESTS}"
    ).fetchone() == (0,)


def test_a_cancelled_observation_still_needs_the_job_and_its_final_event(
    owned: m1.Owned,
) -> None:
    """The scheduler row and the job's last event must both say `cancelled`."""
    run_id = started(owned)
    settled_stop(owned, run_id, stop_request_id="stop-no-event", at_us=WALL_US + 10)

    with (
        pytest.raises(
            sqlite3.IntegrityError,
            match="does not match scheduler and final event",
        ),
        guarded(owned),
    ):
        insert(
            owned.connection,
            TERMINALS,
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": run_id,
                "terminal_observation_number": 1,
                "attempt_number": None,
                "terminal_state": "cancelled",
                "finished_at_us": WALL_US + 10,
                "result_kind": None,
                "result_json": None,
                "error_json": None,
                "cancellation_reason": "operator.cancelled",
                "provenance_kind": "service_committed",
                "fencing_generation": owned.generation,
            },
        )


def test_a_cancelled_observation_may_not_name_an_attempt_the_job_never_had(
    owned: m1.Owned,
) -> None:
    run_id = started(owned)
    settled_stop(owned, run_id, stop_request_id="stop-attempt", at_us=WALL_US + 10)

    with pytest.raises(
        sqlite3.IntegrityError, match="attempt history is inconsistent"
    ):
        cancel_the_job(owned, run_id, at_us=WALL_US + 10, attempt_number=1)


@pytest.mark.parametrize(
    ("provenance", "generation"),
    (("legacy_unrecorded", None), ("service_committed", 999)),
    ids=("legacy-provenance", "another-generation"),
)
def test_the_copied_guard_preamble_still_refuses_a_forged_provenance(
    owned: m1.Owned, provenance: str, generation: int | None
) -> None:
    """Even with the whole cancellation lineage present, the preamble decides first."""
    run_id = started(owned)
    settled_stop(owned, run_id, stop_request_id="stop-guard", at_us=WALL_US + 10)

    with (
        pytest.raises(sqlite3.IntegrityError, match="unguarded INSERT"),
        guarded(owned),
    ):
        insert(
            owned.connection,
            TERMINALS,
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": run_id,
                "terminal_observation_number": 1,
                "attempt_number": None,
                "terminal_state": "cancelled",
                "finished_at_us": WALL_US + 10,
                "result_kind": None,
                "result_json": None,
                "error_json": None,
                "cancellation_reason": "operator.cancelled",
                "provenance_kind": provenance,
                "fencing_generation": (
                    owned.generation if generation is None else generation
                ),
            },
        )

    assert owned.connection.execute(f"SELECT COUNT(*) FROM {TERMINALS}").fetchone() == (
        0,
    )


def test_a_second_cancellation_may_not_reuse_the_first_observations_number(
    owned: m1.Owned,
) -> None:
    """Contiguity is 0015's rule and 0036 restates it verbatim."""
    run_id = started(owned)
    settled_stop(owned, run_id, stop_request_id="stop-once", at_us=WALL_US + 10)
    cancel_the_job(owned, run_id, at_us=WALL_US + 10)

    with (
        pytest.raises(sqlite3.IntegrityError, match="must be contiguous"),
        guarded(owned),
    ):
        insert(
            owned.connection,
            TERMINALS,
            {
                "workspace_id": WORKSPACE_ID,
                "job_id": run_id,
                "terminal_observation_number": 1,
                "attempt_number": None,
                "terminal_state": "cancelled",
                "finished_at_us": WALL_US + 10,
                "result_kind": None,
                "result_json": None,
                "error_json": None,
                "cancellation_reason": "operator.cancelled",
                "provenance_kind": "service_committed",
                "fencing_generation": owned.generation,
            },
        )

    assert owned.connection.execute(f"SELECT COUNT(*) FROM {TERMINALS}").fetchone() == (
        1,
    )


def test_the_runs_own_sequence_is_what_the_accepted_stop_named(
    owned: m1.Owned,
) -> None:
    """A sanity check on the forged lineage itself, so the refusals above mean what
    they say: 0025 admits the accepted outcome only against the run's real event."""
    run_id = started(owned)
    settled_stop(owned, run_id, stop_request_id="stop-seq", at_us=WALL_US + 10)

    assert owned.connection.execute(
        f"SELECT runtime_event_sequence FROM {STOP_OUTCOMES}"
    ).fetchone() == (
        read_run_sequence(owned.connection, workspace_id=WORKSPACE_ID, run_id=run_id),
    )
