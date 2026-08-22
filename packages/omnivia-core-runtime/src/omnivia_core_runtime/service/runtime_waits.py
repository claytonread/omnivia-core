"""The two durable Wait transitions, each settled as one runtime command (RT-107).

A run suspends and a run resumes. Both are runtime commands in the RT-104 sense and
nothing else: :func:`open_runtime_wait` and :func:`resolve_runtime_wait` each compose
:func:`~service.runtime_command.execute_runtime_command` around the RT-102 writes, so
the wait row, the step's status entry and the run's event land with 0007's audit, claim
and outcome or none of them do.

**This is not job recovery.** There is no `job.resume`, nothing is requeued, no job is
named, no public operation is registered and the frozen catalogue is only read from. A
resolution restores *the same step* to `running` under its existing attempt: resuming is
a state transition on a record, not a second scheduling decision. RT-106 owns
scheduling and does not run here.

**Opening.** One `open_wait`, one `record_step_status` of `waiting`, one run event
carrying run status `waiting`, in that order because the migration refuses a `waiting`
step that names no unresolved wait. Whether the run may reach `waiting` at all is the
event stream's own transition rule, enforced by 0018 rather than restated here.

**Resolving.** The accepted contract decides what may resolve what:
:func:`~omnivia_core.contracts.v1.validate_resolve_wait` is handed the *stored* `Wait`,
the `Approval` the policy seam identified and the run's current status, and it is what
proves the wait is this wait, is still pending, admits this resolution for its kind, and
published the resume digest the command quotes. This module adds the three facts a pure
validator cannot know: the workspace the grant covers, the deadline in stored
microseconds, and whether this wait was already resolved.

**The policy seam is mandatory and fail-closed.** :data:`WaitResolutionPolicy` has no
default and no permissive fallback: every resolution runs it, it is handed the
authorized context, the command and the stored wait, and it returns the recorded
`Approval` for an `approval_decision` and `None` for every other resolution. A seam that
returns `None` where an approval was required -- or an approval where none belongs -- is
a refusal, not a shrug. It creates no approval record: RT-203 owns approval persistence,
and this seam only *identifies* what that store already holds.

**Two idempotency layers, and they answer different questions.** The same caller-scoped
idempotency key is answered by the mutation seam from its stored outcome: the command
callback never runs, so the policy is not re-consulted and no row is appended. A *second
key* carrying the same resolution is answered here, from the resolution the wait already
holds -- identical in status, reason and approval means the same accepted answer, served
without a second write, because a wait stops being pending exactly once. A second key
carrying a *different* resolution is :class:`WaitResolutionConflict`, raised before any
write, and the transaction rolls back with it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Final, Literal

from omnivia_core.contracts.v1 import (
    DEFAULT_RETRY_CLASSIFICATION,
    ERROR_CODE_CONFLICT,
    ERROR_CODE_NOT_FOUND,
    RUN_STATUS_RUNNING,
    RUN_STATUS_WAITING,
    WAIT_RESOLUTION_CANCELLED,
    WAIT_RESOLUTION_FOR_KIND,
    WAIT_STATUS_PENDING,
    Approval,
    ContractSemanticError,
    IdempotencyEquivalence,
    ResolveWait,
    Wait,
    validate_resolve_wait,
    validate_resolve_wait_shape,
)
from omnivia_core_runtime.ownership.identity import Clock, ServiceInstanceIdentity
from omnivia_core_runtime.service.authorization import AuthorizedApplicationContext
from omnivia_core_runtime.service.mutation import (
    MutationDenied,
    MutationGrant,
    MutationOutcome,
    MutationSettlementContext,
    ResultValidator,
)
from omnivia_core_runtime.service.operations import OperationError
from omnivia_core_runtime.service.runtime_command import (
    RuntimeAggregateExpectation,
    execute_runtime_command,
)
from omnivia_core_runtime.storage.agent_runtime import RuntimeWriter, read_run

#: The `WaitStatus` each `WaitResolution` settles the wait in. The contract already fixes
#: which resolution may resolve which kind of wait; this is the one thing left to say --
#: what the wait *becomes*. A timer that reached its deadline expired, a cancelled wait
#: was released, and the two resolutions that carry an outcome resolve it.
WAIT_STATUS_FOR_RESOLUTION: Final[Mapping[str, str]] = MappingProxyType(
    {
        "approval_decision": "resolved",
        "external_signal": "resolved",
        "timer_expiry": "expired",
        "cancelled": "cancelled",
    }
)

_APPROVAL_DECISION: Final = "approval_decision"
_STEP_STATUS_WAITING: Final = "waiting"
_STEP_STATUS_RUNNING: Final = "running"

_EVENT_KIND_WAIT_OPENED: Final = "wait_opened"
_EVENT_KIND_WAIT_RESOLVED: Final = "wait_resolved"

#: The wait's raw deadline, in the microseconds it was stored in. Read rather than parsed
#: back out of the contract record: `Wait.expires_at` is rendered to millisecond precision,
#: and a deadline compared at a coarser resolution than it was recorded at is a deadline
#: this module would sometimes decide the wrong side of.
_WAIT_DEADLINE: Final = (
    "SELECT expires_at_us FROM omnivia_runtime_waits "
    "WHERE workspace_id = ? AND wait_id = ?"
)


class WaitNotFound(OperationError):
    """The workspace holds no such run, or that run holds no such wait."""

    def __init__(self, message: str) -> None:
        super().__init__(
            ERROR_CODE_NOT_FOUND,
            message,
            retry_class=DEFAULT_RETRY_CLASSIFICATION[ERROR_CODE_NOT_FOUND],
        )


class WaitResolutionConflict(OperationError):
    """The wait cannot be resolved this way: wrong state, wrong instant, or a duplicate.

    `conflict` rather than `mutation_precondition_failed`, for the reason
    :class:`~service.runtime_command.RuntimeSequenceConflict` gives: this is not a record
    version a caller refreshes and retries, it is a statement about what the wait and its
    run are currently in -- something the caller has to re-read and re-decide against.
    """

    def __init__(self, message: str) -> None:
        super().__init__(
            ERROR_CODE_CONFLICT,
            message,
            retry_class=DEFAULT_RETRY_CLASSIFICATION[ERROR_CODE_CONFLICT],
        )


class WaitPolicyDenied(MutationDenied):
    """The policy seam did not authorize this resolution.

    A :class:`~service.mutation.MutationDenied` subclass, so it renders in the contract's
    own `authorization_denied` vocabulary exactly as a refused grant does, and is still
    distinguishable from one without reading the message.
    """


#: The mandatory fail-closed decision seam for one resolution, handed the authorized
#: context, the command and the *stored* wait -- never a wait the request supplied.
#:
#: It returns the `Approval` already recorded for an `approval_decision`, and `None` for
#: every other resolution. It may refuse by raising :class:`WaitPolicyDenied`; returning
#: `None` where an approval was required is equally a refusal, because a decision nobody
#: recorded is not a decision. It persists nothing: RT-203 owns the approval store.
WaitResolutionPolicy = Callable[
    [AuthorizedApplicationContext, ResolveWait, Wait], Approval | None
]


@dataclass(frozen=True, slots=True)
class WaitOpening:
    """Everything one durable suspension states when it is opened.

    `run_step_id` is the step that suspends and the step a later resolution restores;
    `resume_digest` is what binds the resolution to the state suspended here. There is no
    `status`: a wait that exists and has no resolution is pending, which is the one state
    an opening can produce.
    """

    wait_id: str
    run_id: str
    run_step_id: str
    kind: str
    resume_digest: str
    runtime_event_id: str
    expires_at_us: int | None = None
    event_kind: str = _EVENT_KIND_WAIT_OPENED
    message: str | None = None


def open_runtime_wait(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    grant: MutationGrant,
    context: AuthorizedApplicationContext,
    equivalence: IdempotencyEquivalence,
    opening: WaitOpening,
    validate_result: ResultValidator,
    clock: Clock,
    expected: RuntimeAggregateExpectation,
) -> MutationOutcome:
    """Suspend one run on one wait, atomically, as a single runtime command.

    The wait, the step's `waiting` status entry and the run's `waiting` event commit
    together with the command's settlement or not at all. The order is fixed by the
    migration rather than chosen here: a `waiting` step must already have an unresolved
    wait naming it.
    """

    _require_expected_run(expected, opening.run_id)

    def command(
        writer: RuntimeWriter, settlement: MutationSettlementContext
    ) -> Mapping[str, Any]:
        _require_active_attempt(
            writer.connection,
            writer.workspace_id,
            opening.run_id,
            opening.run_step_id,
            run_status=RUN_STATUS_RUNNING,
            step_status=_STEP_STATUS_RUNNING,
        )
        writer.open_wait(
            wait_id=opening.wait_id,
            run_id=opening.run_id,
            run_step_id=opening.run_step_id,
            kind=opening.kind,
            created_at_us=settlement.settled_at_us,
            resume_digest=opening.resume_digest,
            expires_at_us=opening.expires_at_us,
        )
        writer.record_step_status(
            run_step_id=opening.run_step_id,
            status=_STEP_STATUS_WAITING,
            observed_at_us=settlement.settled_at_us,
        )
        sequence = writer.append_run_event(
            run_id=opening.run_id,
            runtime_event_id=opening.runtime_event_id,
            occurred_at_us=settlement.settled_at_us,
            event_kind=opening.event_kind,
            run_status=RUN_STATUS_WAITING,
            run_step_id=opening.run_step_id,
            message=opening.message,
        )
        return {
            "wait_id": opening.wait_id,
            "run_id": opening.run_id,
            "run_step_id": opening.run_step_id,
            "status": WAIT_STATUS_PENDING,
            "sequence": sequence,
        }

    return execute_runtime_command(
        connection,
        identity,
        grant=grant,
        context=context,
        equivalence=equivalence,
        command=command,
        validate_result=validate_result,
        clock=clock,
        expected=expected,
    )


def resolve_runtime_wait(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    grant: MutationGrant,
    context: AuthorizedApplicationContext,
    equivalence: IdempotencyEquivalence,
    command: ResolveWait,
    policy: WaitResolutionPolicy,
    runtime_event_id: str,
    validate_result: ResultValidator,
    clock: Clock,
    expected: RuntimeAggregateExpectation,
    event_kind: str = _EVENT_KIND_WAIT_RESOLVED,
    message: str | None = None,
) -> MutationOutcome:
    """Resolve one durable wait and resume the step it held, atomically.

    Refuses, leaving nothing behind, when: the command names a workspace this grant does
    not cover; the run or the wait does not exist here; the policy seam denies it or
    hands back an approval that contradicts the resolution; the accepted contract refuses
    the resolution against the stored wait and the run's status; a timer expiry arrives
    before its deadline, or an outcome arrives after one; or the wait already holds a
    *different* resolution. The one case that is not a refusal is a second key carrying
    the *same* resolution, which is answered from the stored one without a second write.
    """

    _require_expected_run(expected, command.run_id)

    def settle(
        writer: RuntimeWriter, settlement: MutationSettlementContext
    ) -> Mapping[str, Any]:
        workspace_id = writer.workspace_id
        if command.workspace_id != workspace_id:
            raise MutationDenied(
                f"the command names workspace {command.workspace_id!r}, which this "
                f"grant does not cover"
            )
        wait = _stored_wait(writer.connection, workspace_id, command)
        status = WAIT_STATUS_FOR_RESOLUTION.get(command.resolution)
        if status is None:
            raise WaitResolutionConflict(
                f"{command.resolution!r} is not a resolution this build can settle a "
                "wait in"
            )

        with _semantic_refusal():
            validate_resolve_wait_shape(command)
        _require_resolution_identity(command, wait)
        if wait.status != WAIT_STATUS_PENDING:
            result = _replayed_resolution(command, wait, status)
            approval = _policy_approval(policy, context, command, wait)
            # Re-check the policy's recorded approval against the immutable identity of
            # the original wait. The stored terminal fields are cleared only in this
            # in-memory validation view; no canonical record is changed or invented.
            pending_view = replace(
                wait,
                status=WAIT_STATUS_PENDING,
                resolved_at=None,
                resolution_reason=None,
                approval_id=None,
            )
            with _semantic_refusal():
                validate_resolve_wait(
                    command,
                    wait=pending_view,
                    approval=approval,
                    run_status=RUN_STATUS_WAITING,
                )
            return result

        _require_active_attempt(
            writer.connection,
            workspace_id,
            command.run_id,
            wait.run_step_id,
            run_status=RUN_STATUS_WAITING,
            step_status=_STEP_STATUS_WAITING,
        )
        _require_deadline_honoured(
            writer.connection, workspace_id, command, status, settlement.settled_at_us
        )
        # Fail-closed, after the stored identity and command shape are known to be valid:
        # every accepted resolution consults the seam, while a malformed or conflicting
        # duplicate cannot use policy as an identifier oracle.
        approval = _policy_approval(policy, context, command, wait)

        with _semantic_refusal():
            validate_resolve_wait(
                command,
                wait=wait,
                approval=approval,
                run_status=_run_status(writer.connection, workspace_id, command.run_id),
            )
        writer.close_wait(
            wait_id=command.wait_id,
            status=status,
            resolved_at_us=settlement.settled_at_us,
            resolution_reason=command.reason,
            approval_id=command.approval_id,
        )
        # The same step, under the attempt it already had. Nothing is requeued and no
        # retry is scheduled: this is the resumption the wait suspended.
        writer.record_step_status(
            run_step_id=wait.run_step_id,
            status=_STEP_STATUS_RUNNING,
            observed_at_us=settlement.settled_at_us,
        )
        writer.append_run_event(
            run_id=command.run_id,
            runtime_event_id=runtime_event_id,
            occurred_at_us=settlement.settled_at_us,
            event_kind=event_kind,
            run_status=RUN_STATUS_RUNNING,
            run_step_id=wait.run_step_id,
            message=message,
        )
        return _resolution_result(
            wait, status=status, reason=command.reason, approval_id=command.approval_id
        )

    return execute_runtime_command(
        connection,
        identity,
        grant=grant,
        context=context,
        equivalence=equivalence,
        command=settle,
        validate_result=validate_result,
        clock=clock,
        expected=expected,
    )


# --- the parts the command is assembled from ------------------------------------


class _semantic_refusal:
    """Render the accepted contract's own refusal as this seam's typed conflict."""

    def __enter__(self) -> None:
        return None

    def __exit__(
        self, kind: object, error: BaseException | None, trace: object
    ) -> Literal[False]:
        if isinstance(error, ContractSemanticError):
            raise WaitResolutionConflict(str(error)) from error
        return False


def _require_expected_run(expected: RuntimeAggregateExpectation, run_id: str) -> None:
    """Keep the optimistic sequence check bound to the run being changed."""
    if expected.run_id != run_id:
        raise WaitResolutionConflict(
            f"the aggregate expectation names run {expected.run_id!r}, not "
            f"the run {run_id!r} this wait command changes"
        )


def _require_resolution_identity(command: ResolveWait, wait: Wait) -> None:
    """Reject a stale or kind-mismatched resolution before consulting policy."""
    if command.resume_digest != wait.resume_digest:
        raise WaitResolutionConflict(
            f"resume_digest does not match the digest wait {wait.wait_id!r} published"
        )
    expected = WAIT_RESOLUTION_FOR_KIND.get(wait.kind)
    if (
        command.resolution != WAIT_RESOLUTION_CANCELLED
        and command.resolution != expected
    ):
        raise WaitResolutionConflict(
            f"resolution {command.resolution!r} does not resolve a {wait.kind!r} wait"
        )


def _require_active_attempt(
    connection: sqlite3.Connection,
    workspace_id: str,
    run_id: str,
    run_step_id: str,
    *,
    run_status: str,
    step_status: str,
) -> None:
    """Prove the wait suspends and resumes one currently active attempt.

    The RT-101 contract does not yet persist an attempt identifier on ``Wait``. The
    strongest honest invariant available in M1 is therefore that its exact step has one
    latest, non-terminal attempt both when the wait opens and when it resumes. Attempt
    histories cannot overlap, so this identifies a single active attempt without adding
    RT-203/worker-schema fields early.
    """
    snapshot = read_run(connection, workspace_id=workspace_id, run_id=run_id)
    if snapshot is None:
        raise WaitNotFound(f"workspace {workspace_id!r} holds no run {run_id!r}")
    if snapshot.status != run_status:
        raise WaitResolutionConflict(
            f"run {run_id!r} is {snapshot.status!r}, not {run_status!r}"
        )
    step = next(
        (
            candidate
            for candidate in snapshot.steps
            if candidate.run_step_id == run_step_id
        ),
        None,
    )
    if step is None:
        raise WaitNotFound(f"run {run_id!r} holds no step {run_step_id!r}")
    if step.status != step_status:
        raise WaitResolutionConflict(
            f"step {run_step_id!r} is {step.status!r}, not {step_status!r}"
        )
    if not step.attempts or step.attempts[-1].status != "running":
        raise WaitResolutionConflict(
            f"step {run_step_id!r} has no active attempt to suspend or resume"
        )


def _policy_approval(
    policy: WaitResolutionPolicy,
    context: AuthorizedApplicationContext,
    command: ResolveWait,
    wait: Wait,
) -> Approval | None:
    """Consult the mandatory policy seam and fail closed on a shape contradiction."""
    approval = policy(context, command, wait)
    if (approval is not None) != (command.resolution == _APPROVAL_DECISION):
        raise WaitPolicyDenied(
            f"the resolution policy identified no recorded approval for a "
            f"{command.resolution!r} resolution of wait {command.wait_id!r}"
            if approval is None
            else f"the resolution policy supplied an approval for a "
            f"{command.resolution!r} resolution, which carries none"
        )
    return approval


def _stored_wait(
    connection: sqlite3.Connection, workspace_id: str, command: ResolveWait
) -> Wait:
    """The wait this command names, read from the run that actually holds it.

    Looked up through the run rather than by identifier alone, which is what makes the
    run in the command a checked fact: a wait of another run is not found here, and the
    workspace was fixed by the grant before this ran.
    """
    snapshot = read_run(connection, workspace_id=workspace_id, run_id=command.run_id)
    if snapshot is None:
        raise WaitNotFound(
            f"workspace {workspace_id!r} holds no run {command.run_id!r}"
        )
    for wait in snapshot.waits:
        if wait.wait_id == command.wait_id:
            return wait
    raise WaitNotFound(f"run {command.run_id!r} holds no wait {command.wait_id!r}")


def _run_status(connection: sqlite3.Connection, workspace_id: str, run_id: str) -> str:
    snapshot = read_run(connection, workspace_id=workspace_id, run_id=run_id)
    if snapshot is None:  # pragma: no cover - the wait read already proved the run
        raise WaitNotFound(f"workspace {workspace_id!r} holds no run {run_id!r}")
    return snapshot.status


def _require_deadline_honoured(
    connection: sqlite3.Connection,
    workspace_id: str,
    command: ResolveWait,
    status: str,
    settled_at_us: int,
) -> None:
    """Refuse an expiry before its deadline, and an outcome after one.

    Both directions, because they are the same rule read from either side: a wait expires
    when its deadline passes and not before, so a wait whose deadline has passed has
    expired rather than been resolved. The migration refuses both too; raising here is
    what makes each a typed refusal instead of a database error.
    """
    row = connection.execute(_WAIT_DEADLINE, (workspace_id, command.wait_id)).fetchone()
    deadline = None if row is None or row[0] is None else int(row[0])
    if status == "expired":
        if deadline is None:
            raise WaitResolutionConflict(
                f"wait {command.wait_id!r} states no deadline, so it cannot expire"
            )
        if settled_at_us < deadline:
            raise WaitResolutionConflict(
                f"wait {command.wait_id!r} has not reached its deadline; a timer expiry "
                "resolves a deadline that passed"
            )
    elif status == "resolved" and deadline is not None and settled_at_us > deadline:
        raise WaitResolutionConflict(
            f"wait {command.wait_id!r} passed its deadline; it has expired, not resolved"
        )


def _replayed_resolution(
    command: ResolveWait, wait: Wait, status: str
) -> Mapping[str, Any]:
    """The answer a wait that is already resolved gives a second command.

    Identical in every part a resolution states -- the status it settled in, the reason
    recorded for it and the approval it named -- is the same resolution arriving under a
    second idempotency key, and is answered from the stored one without a second write. A
    difference in any of them is two resolutions of one wait, which is the state the
    single-resolution key exists to prevent, so it is refused here rather than left to
    the migration.

    The run's status is deliberately not re-checked: the run this command resolved has
    since resumed, and holding a replay to the state it was answered from would refuse
    the very answer it is entitled to.
    """
    with _semantic_refusal():
        validate_resolve_wait_shape(command)
    if command.resume_digest != wait.resume_digest:
        raise WaitResolutionConflict(
            f"resume_digest does not match the digest wait {wait.wait_id!r} published"
        )
    if (wait.status, wait.resolution_reason, wait.approval_id) != (
        status,
        command.reason,
        command.approval_id,
    ):
        raise WaitResolutionConflict(
            f"wait {wait.wait_id!r} is already {wait.status!r} for "
            f"{wait.resolution_reason!r}; a wait is resolved exactly once"
        )
    return _resolution_result(
        wait,
        status=wait.status,
        reason=wait.resolution_reason,
        approval_id=wait.approval_id,
    )


def _resolution_result(
    wait: Wait, *, status: str, reason: str | None, approval_id: str | None
) -> Mapping[str, Any]:
    """The command's answer, built the same way on a first settlement and on a replay.

    It states the resolution and nothing that varies between two commands settling it --
    no sequence, no instant -- because a second key is entitled to the same answer, and
    an answer carrying this command's own settlement identities could not be one.
    """
    result: dict[str, Any] = {
        "wait_id": wait.wait_id,
        "run_id": wait.run_id,
        "run_step_id": wait.run_step_id,
        "status": status,
        "resolution_reason": reason,
    }
    if approval_id is not None:
        result["approval_id"] = approval_id
    return result


__all__ = [
    "WAIT_STATUS_FOR_RESOLUTION",
    "WaitNotFound",
    "WaitOpening",
    "WaitPolicyDenied",
    "WaitResolutionConflict",
    "WaitResolutionPolicy",
    "open_runtime_wait",
    "resolve_runtime_wait",
]
