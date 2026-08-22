"""RT-104 acceptance for the idempotent runtime command and its append transaction.

`service/runtime_command.py` is a composition, so these tests are about what the
composition guarantees rather than about re-proving the seam underneath it. Three
properties, and every test here is one of them.

*One transaction, or none of it.* A command's runtime writes -- the event it appends,
the step it opens -- land with 0007's audit event, claim and terminal outcome and
0013's execution record, or nothing lands at all. Every failure this file can reach is
checked against the same complete row tally: the callback raising after it wrote, a
result the server refuses to serve, a grant outside its window, a fencing generation
that moved, and an expected aggregate sequence the run is no longer at.

*Command replay is the idempotency authority, and there is only one.* An equal request
under the same caller-scoped key returns the stored answer without running the command
again and without appending a second event; the same key under a different canonical
request is the typed conflict, again without running it. Nothing here dedupes on a
runtime event identifier -- a repeated one is a conflict the migration raises and this
transaction rolls back on, which is the opposite of silently idempotent.

*The workspace is the grant's.* The writer handed to a command is bound to the
workspace the server issued the grant for, not to anything the request said, and a
grant that does not cover the request's workspace is refused before a transaction is
opened.

The fixtures are the accepted ones: `m1` owns the workspace, `m18` owns the durable
job and the claim a run is admitted against, and `s0` owns the request, session,
authorization and equivalence a mutation is served under. No public wire surface is
touched, no operation is registered, and the frozen catalogue is only read from.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_rt102_agent_runtime_migration as m18
import test_v06_5_s0_mutation_foundation as s0
from omnivia_core_runtime.ownership.fencing import (
    MutationGuard,
    StaleGeneration,
    read_guard,
)
from omnivia_core_runtime.ownership.identity import FakeClock
from omnivia_core_runtime.service.authorization import (
    AuthorizedApplicationContext,
    ServiceBinding,
    authorize_application_request,
)
from omnivia_core_runtime.service.mutation import (
    MutationDenied,
    MutationGrant,
    MutationIdempotencyConflict,
    MutationSeamFault,
    MutationSettlementContext,
    issue_mutation_grant,
)
from omnivia_core_runtime.service.runtime_command import (
    NO_RUNTIME_EVENTS,
    RuntimeAggregateExpectation,
    RuntimeSequenceConflict,
    execute_runtime_command,
)
from omnivia_core_runtime.storage.agent_runtime import (
    RunAdmission,
    RuntimeWriter,
    admit_run,
    append_run_event,
    read_run,
    read_run_events,
    read_run_sequence,
)
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    ERROR_CODE_CONFLICT,
    OPERATION_CATALOGUE,
    RETRY_CLASS_NON_RETRYABLE,
    RequestEnvelope,
    RunDefinitionRef,
    get_operation_metadata,
    idempotency_equivalence,
    validate_runtime_event_stream,
)

# --- the command this file issues ----------------------------------------------
#
# The seam names no operation: the catalogue is frozen, RT-111 owns the public
# surface, and a caller brings its own authorized context and grant. So the worked
# example here is chosen for its posture rather than its meaning -- `memory.create` is
# workspace-scoped and side-effecting, takes an idempotency key, requires no mutation
# precondition, and is not one of the three operations 0015 requires an application
# bridge settlement from. What is under test is therefore the command envelope and the
# append, with no second precondition or bridge mechanism standing in front of them.

OPERATION = "memory.create"
ENTRY = get_operation_metadata(OPERATION)

WORKSPACE_ID = m1.WORKSPACE_ID
OTHER_WORKSPACE_ID = m18.OTHER_WORKSPACE_ID
JOB_ID = m18.JOB_ID
RUN_ID = m18.RUN_ID

#: The run is admitted at `m18`'s own instant, and the command settles one second
#: later. Two distinct instants, because 0018 refuses an event that predates its run
#: and this file would rather the ordering be true than merely not checked.
ADMITTED_US = m18.BASE_US
COMMAND_WALL = datetime.fromtimestamp((ADMITTED_US + 1_000_000) / 1_000_000, tz=UTC)
SETTLED_US = int(COMMAND_WALL.timestamp() * 1_000_000)

STEP_ID = "step-rt104-0001"
STARTED_EVENT_ID = "evt-rt104-started"
ADMITTED_EVENT_ID = "evt-rt104-admitted"

OPERATION_INPUT: Mapping[str, Any] = {"content": "the run this command starts"}
OTHER_INPUT: Mapping[str, Any] = {"content": "a different request under one key"}

IDEMPOTENCY_KEY = "rt104-idem-0001"

DEFINITION = RunDefinitionRef(
    definition_kind="agent_component",
    definition_id="component.triage",
    definition_version="1.4.0",
)

#: Everything one accepted runtime command may write: the application's four durable
#: settlement relations and every canonical runtime relation a command could reach.
#: Counted as a whole rather than one table at a time, because "no partial
#: materialisation" is a statement about the set.
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
COUNTED_TABLES = s0.DURABLE_TABLES + RUNTIME_TABLES


# --- fixtures -------------------------------------------------------------------


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m1.Owned]:
    """A workspace at the full canonical schema, owned by this service instance."""
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    holder = m1.take_ownership(path)
    yield holder
    holder.connection.close()


@pytest.fixture
def admitted(owned: m1.Owned) -> m1.Owned:
    """The same workspace with one run admitted, its stream at sequence zero.

    Admitted through RT-102's own standalone write, in its own fenced transaction and
    against the claim `m18.seed_job` recorded -- so what RT-104 is tested appending to
    is a run this repository already knows how to produce.
    """
    m18.seed_job(owned, job_id=JOB_ID)
    admit_run(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        admission=RunAdmission(
            run_id=RUN_ID,
            job_id=JOB_ID,
            claim_id=m18.claim_id_for(JOB_ID),
            definition=DEFINITION,
            logical_key=m18.logical_key_for(JOB_ID),
            originating_operation="runtime.admit",
            audit_ref=m18.audit_ref_for(JOB_ID),
            admitted_at_us=ADMITTED_US,
            runtime_event_id=ADMITTED_EVENT_ID,
            message="run admitted",
        ),
    )
    return owned


# --- the request, the grant and the command -------------------------------------


def clock(monotonic: float = s0.MONOTONIC_BASE) -> FakeClock:
    return FakeClock(monotonic=monotonic, wall=COMMAND_WALL)


def authorize(
    *,
    operation_input: Mapping[str, Any] = OPERATION_INPUT,
    **overrides: Any,
) -> AuthorizedApplicationContext:
    """The real authorization seam, for this file's operation."""
    overrides.setdefault("idempotency_key", IDEMPOTENCY_KEY)
    return s0.authorize(ENTRY, operation_input=operation_input, **overrides)


def equivalence_for(
    *,
    operation_input: Mapping[str, Any] = OPERATION_INPUT,
    **overrides: Any,
) -> Any:
    """The accepted contract function's own equivalence value for this request."""
    overrides.setdefault("idempotency_key", IDEMPOTENCY_KEY)
    return s0.equivalence_for(ENTRY, operation_input=operation_input, **overrides)


def issue(
    holder: m1.Owned,
    context: AuthorizedApplicationContext,
    *,
    equivalence: Any = None,
    guard: MutationGuard | None = None,
    clock_: FakeClock | None = None,
) -> MutationGrant:
    """Issue a grant from the live guard row, exactly as a server wiring would.

    `guard` is an override rather than a value this file normally states: the fencing
    generation is read from the live row for every test but the one that is about a
    generation which moved.
    """
    live = read_guard(holder.connection)
    assert live is not None
    return issue_mutation_grant(
        context,
        session=s0.session_for(ENTRY),
        binding=s0.BINDING,
        guard=live if guard is None else guard,
        equivalence=equivalence_for() if equivalence is None else equivalence,
        clock=clock() if clock_ is None else clock_,
    )


class StartRunCommand:
    """One runtime command: open a step and append the event that starts the run.

    Counts its own invocations, which is what makes "a replay does not run the command
    again" a measured fact rather than an inference from row counts. `fail` writes both
    rows and *then* raises, so the rollback it proves is a rollback of real runtime
    materialisation rather than of a callback that never got started.
    """

    def __init__(
        self,
        *,
        event_id: str = STARTED_EVENT_ID,
        step_id: str | None = STEP_ID,
        fail: bool = False,
    ) -> None:
        self.event_id = event_id
        self.step_id = step_id
        self.fail = fail
        self.calls = 0
        self.workspaces: list[str] = []

    def __call__(
        self, writer: RuntimeWriter, settlement: MutationSettlementContext
    ) -> Mapping[str, Any]:
        self.calls += 1
        self.workspaces.append(writer.workspace_id)
        if self.step_id is not None:
            writer.append_run_step(
                run_id=RUN_ID,
                run_step_id=self.step_id,
                ordinal=1,
                step_kind="worker_invocation",
                created_at_us=settlement.settled_at_us,
            )
        sequence = writer.append_run_event(
            run_id=RUN_ID,
            runtime_event_id=self.event_id,
            occurred_at_us=settlement.settled_at_us,
            event_kind="run_started",
            run_status="running",
            run_step_id=self.step_id,
            message="run started",
        )
        if self.fail:
            raise CommandFailure("the runtime command refused after appending")
        return {"run_id": RUN_ID, "sequence": sequence}


class CommandFailure(RuntimeError):
    """Injected command failure, distinguishable from any refusal the seam raises."""


def run_command(
    holder: m1.Owned,
    *,
    command: StartRunCommand,
    context: AuthorizedApplicationContext | None = None,
    grant: MutationGrant | None = None,
    equivalence: Any = None,
    expected: RuntimeAggregateExpectation | None = None,
    validate_result: Any = None,
    clock_: FakeClock | None = None,
) -> Any:
    """One command through the seam, defaulting every part a test is not about.

    The expectation defaults to the sequence a freshly admitted run is at, because
    that is what nearly every test here starts from. A test that is about *not*
    stating one calls `execute_runtime_command` directly rather than asking this
    helper for an absence it cannot express.
    """
    resolved = authorize() if context is None else context
    return execute_runtime_command(
        holder.connection,
        holder.identity,
        grant=issue(holder, resolved) if grant is None else grant,
        context=resolved,
        equivalence=equivalence_for() if equivalence is None else equivalence,
        command=command,
        validate_result=s0.accept_any if validate_result is None else validate_result,
        clock=clock() if clock_ is None else clock_,
        expected=(
            RuntimeAggregateExpectation(run_id=RUN_ID, sequence=0)
            if expected is None
            else expected
        ),
    )


def counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {table: m1.count(connection, table) for table in COUNTED_TABLES}


# --- 1: one transaction ---------------------------------------------------------


def test_rt104_first_execution_commits_the_append_with_the_settlement(
    admitted: m1.Owned,
) -> None:
    """The event, the step, the audit, the claim, the outcome and the spend, together.

    The command's own runtime writes and the four durable settlement rows are counted
    as one set, because that is what "one transaction" means here. The stream is then
    read back through RT-102's reader and held to the accepted contract's own event
    validator, so what committed is a legal history rather than merely some rows.
    """
    before = counts(admitted.connection)
    command = StartRunCommand()

    outcome = run_command(admitted, command=command)

    assert command.calls == 1
    assert outcome.replayed is False
    assert outcome.result == {"run_id": RUN_ID, "sequence": 1}
    assert counts(admitted.connection) == {
        **before,
        "omnivia_application_audit_events": before["omnivia_application_audit_events"]
        + 1,
        "omnivia_idempotency_claims": before["omnivia_idempotency_claims"] + 1,
        "omnivia_idempotency_outcomes": before["omnivia_idempotency_outcomes"] + 1,
        s0.EXECUTIONS_TABLE: before[s0.EXECUTIONS_TABLE] + 1,
        "omnivia_runtime_run_steps": 1,
        "omnivia_runtime_run_step_states": 1,
        "omnivia_runtime_events": 2,
    }

    snapshot = read_run(admitted.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
    assert snapshot is not None
    assert snapshot.status == "running"
    assert [event.sequence for event in snapshot.events] == [0, 1]
    assert snapshot.events[1].runtime_event_id == STARTED_EVENT_ID
    assert snapshot.events[1].run_step_id == STEP_ID
    validate_runtime_event_stream(
        snapshot.events, run_id=RUN_ID, workspace_id=WORKSPACE_ID
    )

    # The command's own rows and the settlement's name the same transaction: the claim
    # this command settled is the one the execution record spent its grant against.
    settled = admitted.connection.execute(
        "SELECT c.claim_id, o.claim_id, e.claim_id, e.execution_kind "
        "FROM omnivia_idempotency_claims c "
        "JOIN omnivia_idempotency_outcomes o ON o.claim_id = c.claim_id "
        f"JOIN {s0.EXECUTIONS_TABLE} e ON e.claim_id = c.claim_id "
        "WHERE c.idempotency_key = ?",
        (IDEMPOTENCY_KEY,),
    ).fetchone()
    assert settled == (
        outcome.claim_id,
        outcome.claim_id,
        outcome.claim_id,
        "executed",
    )
    assert admitted.connection.in_transaction is False


# --- 2: an equal duplicate ------------------------------------------------------


def test_rt104_equal_duplicate_replays_without_a_second_append(
    admitted: m1.Owned,
) -> None:
    """The stored answer, the same claim, no second event and no second command run.

    The replay is issued a *fresh* grant, as a redelivered request would be, and the
    expected sequence it states is the stale one from before the first execution --
    deliberately, because a replay answers for the command that already ran and must
    not be re-checked against a run that has since moved.
    """
    first_command = StartRunCommand()
    first = run_command(admitted, command=first_command)
    settled = counts(admitted.connection)

    second_command = StartRunCommand()
    second = run_command(admitted, command=second_command)

    assert second_command.calls == 0
    assert second.replayed is True
    assert second.result == first.result
    assert second.claim_id == first.claim_id
    assert second.audit_ref == first.audit_ref
    assert [event.sequence for event in read_run_events(
        admitted.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )] == [0, 1]
    # One new row in the whole database: the durable spend of the second grant, which
    # is what stops a fresh grant from later authorizing a write.
    assert counts(admitted.connection) == {
        **settled,
        s0.EXECUTIONS_TABLE: settled[s0.EXECUTIONS_TABLE] + 1,
    }
    assert [
        str(row[0])
        for row in admitted.connection.execute(
            f"SELECT execution_kind FROM {s0.EXECUTIONS_TABLE} ORDER BY recorded_at_us"
        ).fetchall()
    ] == ["executed", "replayed"]


# --- 3: the same key, a different request ---------------------------------------


def test_rt104_same_key_with_a_different_request_is_the_typed_conflict(
    admitted: m1.Owned,
) -> None:
    """A different canonical request under one key raises rather than appends."""
    run_command(admitted, command=StartRunCommand())
    settled = counts(admitted.connection)

    conflicting = authorize(operation_input=OTHER_INPUT)
    equivalence = equivalence_for(operation_input=OTHER_INPUT)
    command = StartRunCommand(event_id="evt-rt104-conflict")

    with pytest.raises(MutationIdempotencyConflict) as raised:
        run_command(
            admitted,
            command=command,
            context=conflicting,
            grant=issue(admitted, conflicting, equivalence=equivalence),
            equivalence=equivalence,
        )

    assert command.calls == 0
    assert raised.value.audit_reference is not None
    assert counts(admitted.connection) == settled
    assert admitted.connection.in_transaction is False


# --- 4: an expected sequence the run is no longer at ----------------------------


def test_rt104_stale_expected_sequence_leaves_nothing_behind(
    admitted: m1.Owned,
) -> None:
    """The command never runs, and its audit event rolls back with the transaction."""
    run_command(admitted, command=StartRunCommand())
    settled = counts(admitted.connection)

    # The run is at sequence 1 now, and this command states the sequence it was at
    # before the first one committed.
    context = authorize(idempotency_key="rt104-idem-stale")
    equivalence = equivalence_for(idempotency_key="rt104-idem-stale")
    command = StartRunCommand(event_id="evt-rt104-stale", step_id=None)

    with pytest.raises(RuntimeSequenceConflict) as raised:
        run_command(
            admitted,
            command=command,
            context=context,
            grant=issue(admitted, context, equivalence=equivalence),
            equivalence=equivalence,
            expected=RuntimeAggregateExpectation(run_id=RUN_ID, sequence=0),
        )

    assert command.calls == 0
    assert raised.value.code == ERROR_CODE_CONFLICT
    assert raised.value.retry_class == RETRY_CLASS_NON_RETRYABLE
    assert counts(admitted.connection) == settled
    assert admitted.connection.in_transaction is False


def no_runtime_writes(
    _writer: RuntimeWriter, _settlement: MutationSettlementContext
) -> Mapping[str, Any]:
    """A command whose whole effect is its answer. Nothing runtime-shaped is written."""
    return {"appended": False}


def test_rt104_admission_and_sequence_zero_stay_compatible_with_rt102(
    owned: m1.Owned,
) -> None:
    """An empty stream is `NO_RUNTIME_EVENTS`, and RT-102's admission moves it to zero.

    The compatibility RT-102 is owed, stated as a command rather than as a read. A run
    this workspace does not hold is at the sequence a stream about to be opened is at,
    so a command that means to open one expects exactly that and is admitted; once
    RT-102's own `admit_run` has opened the stream at zero, the same expectation is
    stale and the same command is refused.
    """
    assert (
        read_run_sequence(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
        == NO_RUNTIME_EVENTS
    )
    context = authorize()
    outcome = execute_runtime_command(
        owned.connection,
        owned.identity,
        grant=issue(owned, context),
        context=context,
        equivalence=equivalence_for(),
        command=no_runtime_writes,
        validate_result=s0.accept_any,
        clock=clock(),
        expected=RuntimeAggregateExpectation(
            run_id=RUN_ID, sequence=NO_RUNTIME_EVENTS
        ),
    )
    assert outcome.result == {"appended": False}
    assert m1.count(owned.connection, "omnivia_runtime_events") == 0

    m18.seed_job(owned, job_id=JOB_ID)
    admit_run(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        admission=RunAdmission(
            run_id=RUN_ID,
            job_id=JOB_ID,
            claim_id=m18.claim_id_for(JOB_ID),
            definition=DEFINITION,
            logical_key=m18.logical_key_for(JOB_ID),
            originating_operation="runtime.admit",
            audit_ref=m18.audit_ref_for(JOB_ID),
            admitted_at_us=ADMITTED_US,
            runtime_event_id=ADMITTED_EVENT_ID,
        ),
    )
    assert (
        read_run_sequence(owned.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
        == 0
    )

    stale = authorize(idempotency_key="rt104-idem-after-admission")
    with pytest.raises(RuntimeSequenceConflict):
        execute_runtime_command(
            owned.connection,
            owned.identity,
            grant=issue(
                owned,
                stale,
                equivalence=equivalence_for(
                    idempotency_key="rt104-idem-after-admission"
                ),
            ),
            context=stale,
            equivalence=equivalence_for(
                idempotency_key="rt104-idem-after-admission"
            ),
            command=no_runtime_writes,
            validate_result=s0.accept_any,
            clock=clock(),
            expected=RuntimeAggregateExpectation(
                run_id=RUN_ID, sequence=NO_RUNTIME_EVENTS
            ),
        )


def test_rt104_an_unstated_expectation_reads_no_sequence(admitted: m1.Owned) -> None:
    """A command with nothing to expect is not held to one it never stated.

    The run is at sequence zero here and stays there; the command states no
    expectation, writes no runtime row, and settles. `expected` is optional because a
    command whose runtime writes touch no existing stream has no aggregate version to
    be optimistic about -- not because the check is advisory.
    """
    context = authorize()
    outcome = execute_runtime_command(
        admitted.connection,
        admitted.identity,
        grant=issue(admitted, context),
        context=context,
        equivalence=equivalence_for(),
        command=no_runtime_writes,
        validate_result=s0.accept_any,
        clock=clock(),
        expected=None,
    )
    assert outcome.replayed is False
    assert outcome.result == {"appended": False}
    assert [
        event.sequence
        for event in read_run_events(
            admitted.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
        )
    ] == [0]


# --- 5: every failure leaves nothing --------------------------------------------


def test_rt104_a_failing_command_rolls_back_what_it_already_wrote(
    admitted: m1.Owned,
) -> None:
    """The step and the event it appended before raising do not survive."""
    before = counts(admitted.connection)
    command = StartRunCommand(fail=True)

    with pytest.raises(CommandFailure, match="after appending"):
        run_command(admitted, command=command)

    assert command.calls == 1
    assert counts(admitted.connection) == before
    assert admitted.connection.in_transaction is False


def test_rt104_a_refused_result_rolls_back_the_append(admitted: m1.Owned) -> None:
    """A result the server will not serve takes the runtime rows with it."""
    before = counts(admitted.connection)
    command = StartRunCommand()

    def refuse_everything(_result: Mapping[str, Any]) -> bool:
        return False

    with pytest.raises(MutationSeamFault):
        run_command(admitted, command=command, validate_result=refuse_everything)

    assert command.calls == 1
    assert counts(admitted.connection) == before
    assert admitted.connection.in_transaction is False


def test_rt104_an_expired_grant_never_reaches_the_command(admitted: m1.Owned) -> None:
    """A grant outside its window is refused before a transaction is opened."""
    before = counts(admitted.connection)
    context = authorize()
    grant = issue(admitted, context)
    command = StartRunCommand()

    with pytest.raises(MutationDenied):
        run_command(
            admitted,
            command=command,
            context=context,
            grant=grant,
            clock_=clock(monotonic=s0.MONOTONIC_BASE + 61.0),
        )

    assert command.calls == 0
    assert counts(admitted.connection) == before


def test_rt104_a_generation_that_moved_never_reaches_the_command(
    admitted: m1.Owned,
) -> None:
    """A grant pinning a fencing generation this instance no longer holds is refused."""
    before = counts(admitted.connection)
    live = read_guard(admitted.connection)
    assert live is not None
    context = authorize()
    command = StartRunCommand()

    with pytest.raises(StaleGeneration):
        run_command(
            admitted,
            command=command,
            context=context,
            grant=issue(
                admitted,
                context,
                guard=MutationGuard(
                    workspace_id=live.workspace_id,
                    service_instance_id=live.service_instance_id,
                    fencing_generation=live.fencing_generation + 1,
                ),
            ),
        )

    assert command.calls == 0
    assert counts(admitted.connection) == before
    assert admitted.connection.in_transaction is False


# --- 6: the workspace is the grant's --------------------------------------------


def test_rt104_the_command_writer_is_bound_to_the_granted_workspace(
    admitted: m1.Owned,
) -> None:
    """The writer names the grant's workspace, and a crossing attempt rolls back.

    Two halves. The workspace the command is handed is the server-issued one, and a
    command that reaches past its writer to name another workspace directly is refused
    by the migration's own guard -- so the binding is not merely a convention the
    writer follows.
    """
    command = StartRunCommand()
    run_command(admitted, command=command)
    assert command.workspaces == [WORKSPACE_ID]

    settled = counts(admitted.connection)
    context = authorize(idempotency_key="rt104-idem-crossing")
    equivalence = equivalence_for(idempotency_key="rt104-idem-crossing")

    def cross_the_workspace(
        writer: RuntimeWriter, settlement: MutationSettlementContext
    ) -> Mapping[str, Any]:
        assert writer.workspace_id == WORKSPACE_ID
        writer.connection.execute(
            "INSERT INTO omnivia_runtime_events "
            "(workspace_id, run_id, sequence, runtime_event_id, occurred_at_us, "
            "event_kind, run_status) VALUES (?, ?, 2, 'evt-rt104-crossed', ?, "
            "'run_started', 'running')",
            (OTHER_WORKSPACE_ID, RUN_ID, settlement.settled_at_us),
        )
        return {"crossed": True}

    with pytest.raises(sqlite3.Error, match="unguarded INSERT"):
        execute_runtime_command(
            admitted.connection,
            admitted.identity,
            grant=issue(admitted, context, equivalence=equivalence),
            context=context,
            equivalence=equivalence,
            command=cross_the_workspace,
            validate_result=s0.accept_any,
            clock=clock(),
            expected=RuntimeAggregateExpectation(run_id=RUN_ID, sequence=1),
        )

    assert counts(admitted.connection) == settled
    assert admitted.connection.in_transaction is False


def foreign_workspace_grant(holder: m1.Owned) -> MutationGrant:
    """A grant honestly issued for the *other* workspace, to present against this one.

    Issued through the real issuer against a real authorization of a request naming
    that workspace, rather than fabricated: a value this seam can only refuse because
    it does not cover the request presented, not because it is malformed.
    """
    live = read_guard(holder.connection)
    assert live is not None
    session = s0.session_for(
        ENTRY, workspaces=frozenset({WORKSPACE_ID, OTHER_WORKSPACE_ID})
    )
    binding = ServiceBinding(installation_id=s0.INSTALLATION_ID, workspace_id=None)
    metadata = s0.metadata_for(
        ENTRY, idempotency_key=IDEMPOTENCY_KEY, workspace_id=OTHER_WORKSPACE_ID
    )
    return issue_mutation_grant(
        authorize_application_request(
            RequestEnvelope(
                operation=OPERATION, metadata=metadata, input=dict(OPERATION_INPUT)
            ),
            session=session,
            binding=binding,
            supported_capabilities=s0.SUPPORTED,
        ),
        session=session,
        binding=binding,
        guard=MutationGuard(
            workspace_id=OTHER_WORKSPACE_ID,
            service_instance_id=live.service_instance_id,
            fencing_generation=live.fencing_generation,
        ),
        equivalence=idempotency_equivalence(
            OPERATION,
            metadata,
            dict(OPERATION_INPUT),
            principal_id=s0.PRINCIPAL,
            workspace_id=OTHER_WORKSPACE_ID,
        ),
        clock=clock(),
    )


def test_rt104_a_grant_for_another_workspace_is_refused(admitted: m1.Owned) -> None:
    """A grant the request's workspace is not covered by never opens a transaction."""
    before = counts(admitted.connection)
    command = StartRunCommand()
    foreign = foreign_workspace_grant(admitted)
    assert foreign.workspace_id == OTHER_WORKSPACE_ID

    with pytest.raises(MutationDenied):
        run_command(admitted, command=command, context=authorize(), grant=foreign)

    assert command.calls == 0
    assert counts(admitted.connection) == before


# --- 7: sequences stay contiguous, and RT-102 still holds -----------------------


def test_rt104_sequence_allocation_stays_contiguous_across_both_routes(
    admitted: m1.Owned,
) -> None:
    """Commands and RT-102's standalone append allocate from the same stream.

    The two routes into `append_run_event` -- a command inside the mutation seam's
    transaction and RT-102's own fenced write -- read the same sequence and must not
    disagree about the next one. Interleaving them is what proves that.
    """
    run_command(admitted, command=StartRunCommand())

    append_run_event(
        admitted.connection,
        admitted.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=admitted.generation,
        run_id=RUN_ID,
        runtime_event_id="evt-rt104-standalone",
        occurred_at_us=SETTLED_US,
        event_kind="worker_progress",
        run_status="running",
    )

    for index in (3, 4):
        key = f"rt104-idem-{index}"
        context = authorize(idempotency_key=key)
        equivalence = equivalence_for(idempotency_key=key)
        outcome = run_command(
            admitted,
            command=StartRunCommand(
                event_id=f"evt-rt104-{index}", step_id=None
            ),
            context=context,
            grant=issue(admitted, context, equivalence=equivalence),
            equivalence=equivalence,
            expected=RuntimeAggregateExpectation(run_id=RUN_ID, sequence=index - 1),
        )
        assert outcome.result["sequence"] == index

    events = read_run_events(
        admitted.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID
    )
    assert [event.sequence for event in events] == [0, 1, 2, 3, 4]
    validate_runtime_event_stream(events, run_id=RUN_ID, workspace_id=WORKSPACE_ID)
    assert (
        read_run_sequence(admitted.connection, workspace_id=WORKSPACE_ID, run_id=RUN_ID)
        == 4
    )


def test_rt104_a_repeated_event_identity_is_a_conflict_not_a_replay(
    admitted: m1.Owned,
) -> None:
    """Command replay is the idempotency authority; an event identity is not.

    A second command under its own key, appending an event whose identifier the stream
    already holds, is refused and rolled back rather than quietly accepted as the same
    thing happening twice.
    """
    run_command(admitted, command=StartRunCommand())
    settled = counts(admitted.connection)

    context = authorize(idempotency_key="rt104-idem-repeat")
    equivalence = equivalence_for(idempotency_key="rt104-idem-repeat")
    command = StartRunCommand(event_id=STARTED_EVENT_ID, step_id=None)

    with pytest.raises(sqlite3.Error, match="identifier is used at most once"):
        run_command(
            admitted,
            command=command,
            context=context,
            grant=issue(admitted, context, equivalence=equivalence),
            equivalence=equivalence,
            expected=RuntimeAggregateExpectation(run_id=RUN_ID, sequence=1),
        )

    assert command.calls == 1
    assert counts(admitted.connection) == settled
    assert admitted.connection.in_transaction is False


def test_rt104_adds_no_public_wire_surface(admitted: m1.Owned) -> None:
    """The contract version is unchanged and no operation was registered here.

    RT-111 owns the product-safe public surface. This slice composes two existing
    seams and must not have grown a third: the catalogue is read from and never added
    to, and this module names no operation of its own.
    """
    import omnivia_core_runtime.service.runtime_command as module

    assert CONTRACT_VERSION == "1.3"
    named = {entry.name for entry in OPERATION_CATALOGUE}
    assert not named & {
        value for value in vars(module).values() if isinstance(value, str)
    }
    assert set(module.__all__) == {
        "NO_RUNTIME_EVENTS",
        "RuntimeAggregateExpectation",
        "RuntimeCommand",
        "RuntimeSequenceConflict",
        "execute_runtime_command",
    }
