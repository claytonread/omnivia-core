"""Workflow Runtime scheduling bridge over canonical Runtime step storage.

This is the first M3 seam, not the whole scheduler. It takes the durable M2 workflow
plan and opens the canonical runtime step rows a workflow-owned scheduler can claim.
It records branch-gated readiness, selected-connection-gated readiness, durable loop
iterations, and the suspension and resolution of a route-level wait, but no effect and
no dispatcher result; those remain downstream scheduler/executor/recovery work.

The connection gate is narrow on purpose. 0030 stores what one *run* recorded as
required and as selected for a step, and this scheduler compares the two sets. It does
not read a connection's condition, evaluate one, or know what the connection joins:
DOC-005 readiness over a definition graph is upstream work that produces these facts,
not something derived here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from hashlib import sha256
from types import MappingProxyType
from typing import Final, Literal

from omnivia_core.contracts.v1 import (
    ATTEMPT_STATUS_FAILED,
    ATTEMPT_STATUS_RUNNING,
    ATTEMPT_STATUS_SUCCEEDED,
    ATTEMPT_STATUS_UNCERTAIN,
    EFFECT_OUTCOME_COMMITTED,
    EFFECT_OUTCOME_NOT_COMMITTED,
    EFFECT_OUTCOME_UNKNOWN,
    RUN_STATUS_FAILED,
    RUN_STATUS_PARTIALLY_COMPLETED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_SUCCEEDED,
    RUN_STATUS_UNCERTAIN,
    RUN_STATUS_WAITING,
    RUN_STEP_TERMINAL_STATUSES,
    RUN_TERMINAL_STATUSES,
    WAIT_RESOLUTION_FOR_KIND,
    WAIT_STATUS_PENDING,
    ApiError,
    Approval,
    ContractSemanticError,
    EffectIntent,
    EffectSettlement,
    ExternalReference,
    IdempotencyEquivalence,
    ResolveWait,
    RunStep,
    Wait,
    is_error_retryable,
    to_canonical_json,
    validate_resolve_wait,
    validate_resolve_wait_shape,
)
from omnivia_core_runtime.execution.workflow import (
    BRANCH_BLOCKED,
    BRANCH_MATCHED,
    BRANCH_UNMATCHED,
    OUTCOME_FAILED,
    ROUTE_EFFECT,
    ROUTE_WAIT,
    WorkflowDispatchPlanner,
    WorkResultEnvelope,
    WorkUnitEnvelope,
)
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import Clock, ServiceInstanceIdentity
from omnivia_core_runtime.service.authorization import AuthorizedApplicationContext
from omnivia_core_runtime.service.effect_reconciliation import (
    reconcile_effect_transaction,
)
from omnivia_core_runtime.service.effect_transaction import (
    DispatchRequest,
    decide_settlement,
    publish_dispatch_request,
)
from omnivia_core_runtime.service.mutation import (
    MutationDenied,
    MutationGrant,
    MutationOutcome,
    MutationSettlementContext,
    ResultValidator,
)
from omnivia_core_runtime.service.runtime_command import (
    RuntimeAggregateExpectation,
    execute_runtime_command,
)
from omnivia_core_runtime.service.runtime_stop import (
    RUNNING_WORK_RELEASE,
    STOP_REASON_CANCELLED,
    request_run_stop,
    settle_run_stop,
)
from omnivia_core_runtime.service.runtime_waits import (
    WAIT_STATUS_FOR_RESOLUTION,
    WaitPolicyDenied,
    WaitResolutionConflict,
    WaitResolutionPolicy,
    require_deadline_honoured,
)
from omnivia_core_runtime.storage.agent_runtime import (
    RuntimeWriter,
    read_effect_dispatch_count,
    read_effect_intent,
    read_effect_receipt_for_intent,
    read_effect_reconciliation_for_intent,
    read_run,
    read_run_sequence,
    read_run_steps,
    read_run_waits,
    runtime_timestamp,
    transaction_local_writer,
)
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.workflow_runs import (
    CONNECTION_INCOMING,
    CONNECTION_OUTGOING,
    CONNECTION_REQUIRED,
    CONNECTION_SELECTED,
    LOOP_EXIT_MAX_ITERATIONS,
    LOOP_EXIT_REQUESTED,
    LOOP_EXIT_TOTAL_BUDGET,
    LOOP_ITERATION_FAILED,
    LOOP_ITERATION_SUCCEEDED,
    OBSERVATION_BRANCH,
    READINESS_CAPABILITY_GRANTED,
    READINESS_CAPABILITY_REQUIRED,
    READINESS_MAPPED_INPUT_READY,
    READINESS_MAPPED_INPUT_REQUIRED,
    WorkflowPlan,
    WorkflowPlanStep,
    read_workflow_loop_iterations,
    read_workflow_plan,
    read_workflow_run_binding,
    read_workflow_run_observations,
    read_workflow_run_step_connections,
    read_workflow_run_step_readiness_facts,
    transaction_local_workflow_writer,
)

#: Why a workflow step is durably suspended, and the canonical RT-101 `WaitKind` each
#: purpose is honestly expressible as. Closed and total: a purpose outside this map is
#: refused rather than defaulted, and no purpose invents a `WaitKind` -- the accepted
#: contract publishes three, and a fourth would be a contract change wearing a
#: scheduler's clothes. Several purposes share `external_signal` on purpose: they differ
#: in *why* the run stopped, which is an event-detail fact, not in *what* resumes it,
#: which is the only thing a kind states.
WORKFLOW_WAIT_PURPOSE_KINDS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "human_task": "approval",
        "timer": "timer",
        "event": "external_signal",
        "asynchronous_operation": "external_signal",
        "child_workflow_run": "external_signal",
        "retry_backoff": "external_signal",
        "effect_reconciliation": "external_signal",
        "capacity_or_quota": "external_signal",
        "administrative_suspension": "approval",
    }
)

#: The wait kind a ROUTE_WAIT step suspends on when its purpose is the default. The M2
#: plan record carries no declared purpose, kind or deadline, so the only honest reading
#: of it is `event`: something outside this run resolves the step, and nothing more.
WORKFLOW_WAIT_KIND: Final = WORKFLOW_WAIT_PURPOSE_KINDS["event"]

#: What the scheduler records as the reason a workflow wait stopped being pending. The
#: signal that resolved it is the caller's fact, not this scheduler's: what it can state
#: is that the wait was resolved by workflow scheduling rather than expiry or cancellation.
WORKFLOW_WAIT_RESOLUTION_REASON: Final = "workflow.wait_resolved"

#: The `WaitStatus` a resolved workflow wait settles in. This scheduler only ever
#: resolves; expiry needs the deadline no plan record carries yet, and cancellation is
#: run-stop work.
_WAIT_STATUS_RESOLVED: Final = "resolved"

#: The purpose a queued step's capacity wait is opened for, and the kind that purpose
#: maps to. The signal is "a slot in this pool came free", which is exactly a fact from
#: outside the step, and `WaitKind` already has that member: a `capacity` kind would
#: broaden the contract and force a migration to say nothing new.
CAPACITY_WAIT_PURPOSE: Final = "capacity_or_quota"
CAPACITY_WAIT_KIND: Final = WORKFLOW_WAIT_PURPOSE_KINDS[CAPACITY_WAIT_PURPOSE]

#: Why a capacity wait stopped being pending. Only ever this: capacity waits are
#: resolved by the grant that claims the step, expired by nothing (they carry no
#: deadline) and cancelled only by run stop, which writes its own reason.
CAPACITY_WAIT_RESOLUTION_REASON: Final = "workflow.capacity_granted"

#: The purpose a failed-but-retryable step is suspended for between attempts, and the
#: kind that purpose already maps to. The signal is "the backoff for this step elapsed",
#: which is a fact from outside the step, so `external_signal` states it exactly and no
#: new `WaitKind` is owed.
RETRY_WAIT_PURPOSE: Final = "retry_backoff"
RETRY_WAIT_KIND: Final = WORKFLOW_WAIT_PURPOSE_KINDS[RETRY_WAIT_PURPOSE]

#: Why a retry wait stopped being pending. Only ever this: a retry wait is released by
#: :meth:`WorkflowScheduler.release_retry_wait`, expired by nothing (it carries no
#: deadline) and cancelled only by run stop, which writes its own reason.
RETRY_WAIT_RESOLUTION_REASON: Final = "workflow.retry_ready"

#: The purpose an effect step is suspended for once its effect settled `unknown`, and
#: the kind that purpose already maps to. The signal is "somebody established what became
#: of this effect", which is a fact from outside the step -- and outside this runtime --
#: so `external_signal` states it exactly and no new `WaitKind` is owed.
RECONCILIATION_WAIT_PURPOSE: Final = "effect_reconciliation"
RECONCILIATION_WAIT_KIND: Final = WORKFLOW_WAIT_PURPOSE_KINDS[RECONCILIATION_WAIT_PURPOSE]

#: Why a reconciliation wait stopped being pending. Only ever this: it is resolved by
#: :meth:`WorkflowScheduler.reconcile_effect_step` once RT-206 has answered, expired by
#: nothing (it carries no deadline) and cancelled only by run stop, which writes its own
#: reason.
RECONCILIATION_WAIT_RESOLUTION_REASON: Final = "workflow.effect_reconciled"

#: The two audit events one governed compensation ever writes, and the event a governed
#: repair writes. They are the whole of what this build can honestly record about either:
#: 0018 has no compensation table, a compensating action is not a materialised workflow
#: step, and inventing a run step or an effect for one would state a plan fact the plan
#: never made. So the record is forward-only audit -- what was ordered, by whom, against
#: which source step or effect, and what it was finally reported to have done -- and the
#: transport that would carry the action out is deliberately not here.
COMPENSATION_STARTED_EVENT: Final = "workflow.compensation.started"
COMPENSATION_SETTLED_EVENT: Final = "workflow.compensation.terminal"
GOVERNED_REPAIR_EVENT: Final = "workflow.recovery.classified"

#: What a compensation may be reported to have done. Deliberately RT-205's three effect
#: outcomes rather than a fourth vocabulary: a compensating action is an effect somebody
#: else carried out, so it either committed, never committed, or nobody can say -- and
#: `unknown` is the answer that matters most, because a compensation that may or may not
#: have landed is exactly the state a governed repair is later asked about.
COMPENSATION_OUTCOMES: Final[tuple[str, ...]] = (
    EFFECT_OUTCOME_COMMITTED,
    EFFECT_OUTCOME_NOT_COMMITTED,
    EFFECT_OUTCOME_UNKNOWN,
)

#: The correlation kind a governed repair's evidence is filed under, and the claim it
#: never makes. A reviewer's attachment is somebody's account of what happened, not the
#: runtime's own observation, so it is `external_log` and never authoritative -- 0019
#: refuses an authoritative claim from anything but `runtime`, and this seam does not
#: try to make one.
GOVERNED_REPAIR_EVIDENCE_SOURCE: Final = "external_log"
GOVERNED_REPAIR_EVIDENCE_KIND: Final = "workflow.governed_repair"

#: The two lanes a run's work runs in. The recovery lane is protected: it may take the
#: slots reserved for it, which the default lane may never touch, and it outranks the
#: default lane for the slots both may take.
CAPACITY_LANE_DEFAULT: Final = "default"
CAPACITY_LANE_RECOVERY: Final = "recovery"

#: Every workflow run step in this workspace whose latest state is `running`, with the
#: run that owns it. This is pool occupancy: a claimed step holds its slot until it
#: leaves `running`, and the query is deliberately not filtered by run, because a pool
#: is shared across every workflow run in the workspace.
_RUNNING_WORKFLOW_STEPS: Final = """
SELECT s.run_id, s.step_kind
FROM omnivia_runtime_run_steps s
JOIN omnivia_runtime_run_step_states st
  ON st.workspace_id = s.workspace_id AND st.run_step_id = s.run_step_id
WHERE s.workspace_id = ? AND s.step_kind LIKE 'workflow.%' AND st.status = 'running'
  AND st.state_sequence = (
    SELECT MAX(x.state_sequence) FROM omnivia_runtime_run_step_states x
    WHERE x.workspace_id = s.workspace_id AND x.run_step_id = s.run_step_id)
"""

#: Every unresolved wait on a workflow run step in this workspace. Which of them are
#: capacity waits is decided in Python by recomputing each step's capacity
#: `resume_digest`: a route wait and a capacity wait share a kind, and the digest is
#: the value that already distinguishes what each one resumes.
_PENDING_WORKFLOW_WAITS: Final = """
SELECT w.wait_id, w.run_id, w.run_step_id, w.created_at_us, s.step_kind, w.resume_digest
FROM omnivia_runtime_waits w
JOIN omnivia_runtime_run_steps s
  ON s.workspace_id = w.workspace_id AND s.run_step_id = w.run_step_id
LEFT JOIN omnivia_runtime_wait_resolutions r
  ON r.workspace_id = w.workspace_id AND r.wait_id = w.wait_id
WHERE w.workspace_id = ? AND w.kind = ? AND r.wait_id IS NULL
  AND s.step_kind LIKE 'workflow.%'
"""


@dataclass(frozen=True, slots=True)
class WorkflowCapacityPolicy:
    """How many workflow steps may run at once, and who gets the next free slot.

    Not durable, and deliberately so: nothing in 0018 or 0027 stores a pool, a priority
    or a lane, and inventing columns for them would claim a decision the runtime has
    not made. What *is* durable is the consequence -- the capacity wait a queued step
    is suspended on, and the run event that names the pool it is queued for.

    `pools` maps a pool to how many steps may hold it at once, shared across every
    workflow run in the workspace. `routes` maps a workflow step route to the pool it
    draws from; a route with no pool is uncapped and never queues.
    """

    pools: Mapping[str, int]
    routes: Mapping[str, str]
    #: Run priority, higher first. A run with none is `0`.
    priorities: Mapping[str, int] = field(default_factory=dict)
    #: The runs on the protected recovery lane.
    recovery_runs: frozenset[str] = frozenset()
    #: Slots per pool only the recovery lane may take.
    recovery_reserved: Mapping[str, int] = field(default_factory=dict)
    #: Queue age at which a waiter outranks priority, so a low-priority waiter cannot
    #: be starved forever by a stream of higher-priority arrivals. `None` disables it.
    escalation_us: int | None = None

    def pool_for(self, route: str) -> str | None:
        return self.routes.get(route)

    def lane_for(self, run_id: str) -> str:
        return (
            CAPACITY_LANE_RECOVERY
            if run_id in self.recovery_runs
            else CAPACITY_LANE_DEFAULT
        )


@dataclass(frozen=True, slots=True)
class _CapacityWaiter:
    """One step already suspended on a capacity wait, and where it sits in the queue."""

    wait_id: str
    run_id: str
    run_step_id: str
    pool: str
    lane: str
    enqueued_at_us: int


class _semantic_refusal:
    """Render accepted RT-107 contract refusals as typed wait conflicts."""

    def __enter__(self) -> None:
        return None

    def __exit__(
        self, kind: object, error: BaseException | None, trace: object
    ) -> Literal[False]:
        if isinstance(error, ContractSemanticError):
            raise WaitResolutionConflict(str(error)) from error
        return False


def _route_of(step_kind: str) -> str:
    """The plan route one canonical workflow step kind was opened for."""
    return step_kind.removeprefix("workflow.").upper()


class WorkflowSchedulingError(StorageError):
    """A workflow run cannot be opened into canonical runtime steps safely."""


class WorkflowGovernanceDenied(WorkflowSchedulingError):
    """A governed recovery command was issued without the authority it requires."""


@dataclass(frozen=True, slots=True)
class WorkflowGovernedAuthority:
    """Who ordered one governed recovery command, and under what approval.

    Every field is required. A governed command is an out-of-band intervention in a run
    that is already moving, so who ordered it, in what role, under which recorded
    approval and for what stated reason is the whole of its audit trail; an optional
    field here would be a hole in exactly the record the command exists to leave.
    """

    actor_id: str
    actor_role: str
    approval_id: str
    reason: str

    def __post_init__(self) -> None:
        for name in ("actor_id", "actor_role", "approval_id", "reason"):
            if not getattr(self, name):
                raise WorkflowGovernanceDenied(
                    f"a governed workflow command states its {name}"
                )

    def facts(self) -> dict[str, object]:
        """The governed facts one command's audit event records verbatim."""
        return {
            "actor_id": self.actor_id,
            "actor_role": self.actor_role,
            "approval_id": self.approval_id,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class WorkflowGovernancePolicy:
    """Which roles may order a compensation, and which may record a governed repair.

    Empty by default, and that default authorises nothing: a scheduler declaring no
    governance refuses every governed command, so the seam is opt-in and fail-closed
    rather than open until somebody remembers to close it. Not durable, for the reason
    :class:`WorkflowCapacityPolicy` is not -- nothing in 0018 or 0027 stores a role, and
    a column invented for one would claim a decision the runtime has not made. What *is*
    durable is the consequence: the audit event naming the actor, the role and the
    approval the command was ordered under.
    """

    compensation_roles: frozenset[str] = frozenset()
    repair_roles: frozenset[str] = frozenset()

    def permits_compensation(self, authority: WorkflowGovernedAuthority) -> bool:
        """Return whether this authority may order compensation."""
        return authority.actor_role in self.compensation_roles

    def permits_repair(self, authority: WorkflowGovernedAuthority) -> bool:
        """Return whether this authority may record governed repair."""
        return authority.actor_role in self.repair_roles


@dataclass(frozen=True, slots=True)
class WorkflowRetryPolicy:
    """How many attempts one workflow step gets before a retryable failure is final.

    `max_attempts` counts attempts, not retries, and defaults to one: without a policy of
    its own a scheduler retries nothing, which is exactly what it did before retries
    existed. Not durable, for the reason :class:`WorkflowCapacityPolicy` is not: nothing
    in 0018 or 0027 stores a budget, and a column invented for one would claim a decision
    the runtime has not made. What *is* durable is the consequence -- the failed attempt
    with its `ApiError`, the `retry_backoff` wait the step is suspended on, and the run
    event naming the attempt that wait backs off for.
    """

    max_attempts: int = 1

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise WorkflowSchedulingError(
                "a workflow retry policy allows at least the first attempt"
            )

    def permits_retry(self, attempt_number: int) -> bool:
        """Whether a step that just failed its `attempt_number`th attempt gets another."""
        return attempt_number < self.max_attempts


@dataclass(frozen=True, slots=True)
class WorkflowWaitPurpose:
    """Why one workflow step suspends, checked against what the durable schema can say.

    Fail-closed in both directions. A purpose outside
    :data:`WORKFLOW_WAIT_PURPOSE_KINDS` is refused, and a purpose whose canonical kind
    needs a fact the M2 plan record does not carry is refused unless the caller states
    that fact here:

    * an `approval` kind -- `human_task` and `administrative_suspension` -- is only
      openable with an `approver_role`, because the decision that resolves it is
      recorded against a role, and the wait row has nowhere to keep one. Without it the
      wait would be an approval nobody could honestly approve, so it is refused rather
      than downgraded to an `external_signal` that quietly drops the human out of the
      loop;
    * a `timer` kind is only openable with an `expires_at_us`, because a timer expiry
      resolves a deadline that passed, and a timer wait carrying no deadline can never
      be resolved at all.

    `checkpoint` is the deterministic resume point this suspension names -- a loop
    iteration, a retry attempt, a reconciliation round. Wait identity is
    `(step, purpose, checkpoint)`, so the same three open one durable wait however many
    times the scheduler re-advances the run, and a different checkpoint is a different
    suspension rather than a silent reuse of the last one.
    """

    purpose: str
    checkpoint: str = ""
    approver_role: str | None = None
    expires_at_us: int | None = None

    def __post_init__(self) -> None:
        if self.purpose not in WORKFLOW_WAIT_PURPOSE_KINDS:
            raise WorkflowSchedulingError(
                f"{self.purpose!r} is not a workflow wait purpose this runtime opens"
            )
        if (self.approver_role is not None) != (self.wait_kind == "approval"):
            raise WorkflowSchedulingError(
                f"a {self.purpose!r} workflow wait is an {self.wait_kind!r} wait; it is "
                "opened with an approver_role and no other purpose carries one"
            )
        if (self.expires_at_us is not None) != (self.wait_kind == "timer"):
            raise WorkflowSchedulingError(
                f"a {self.purpose!r} workflow wait is a {self.wait_kind!r} wait; it is "
                "opened with an expires_at_us deadline and no other purpose carries one"
            )

    @property
    def wait_kind(self) -> str:
        """The canonical `WaitKind` this purpose is durably expressed as."""
        return WORKFLOW_WAIT_PURPOSE_KINDS[self.purpose]

    @property
    def resolution(self) -> str:
        """The one `WaitResolution` that settles a wait opened for this purpose."""
        return WAIT_RESOLUTION_FOR_KIND[self.wait_kind]


#: What a wait step whose purpose the caller did not declare is opened for. `event` maps
#: to the `external_signal` kind route waits already opened, so an undeclared purpose
#: keeps exactly the behaviour this scheduler had before purposes existed.
_DEFAULT_WAIT_PURPOSE: Final = WorkflowWaitPurpose("event")


def _wait_lineage(
    *,
    prefix: str,
    workspace_id: str,
    run_id: str,
    run_step_id: str,
    purpose: WorkflowWaitPurpose,
) -> tuple[str, str]:
    """One wait identifier or resume digest, and the exact preimage it was taken over.

    The preimage is returned rather than only the digest so the run event can state it:
    a digest nobody can reproduce is an opaque token, and the point of recording one is
    that a later resolution can be checked against the suspension it claims to resume.
    """
    preimage = (
        f"{prefix}|{workspace_id}|{run_id}|{run_step_id}"
        f"|{purpose.purpose}|{purpose.checkpoint}"
    )
    return _lineage_id(preimage), preimage


def _capacity_wait_id(
    *, workspace_id: str, run_id: str, run_step_id: str, pool: str
) -> str:
    return _wait_lineage(
        prefix="workflow_capacity_wait",
        workspace_id=workspace_id,
        run_id=run_id,
        run_step_id=run_step_id,
        purpose=WorkflowWaitPurpose(CAPACITY_WAIT_PURPOSE, checkpoint=pool),
    )[0]


def _capacity_resume_digest(
    *, workspace_id: str, run_id: str, run_step_id: str, pool: str
) -> tuple[str, str]:
    return _wait_lineage(
        prefix="workflow_capacity_resume",
        workspace_id=workspace_id,
        run_id=run_id,
        run_step_id=run_step_id,
        purpose=WorkflowWaitPurpose(CAPACITY_WAIT_PURPOSE, checkpoint=pool),
    )


@dataclass(frozen=True, slots=True)
class WorkflowRuntimeStepOpening:
    """The canonical runtime-step rows opened for one workflow run."""

    workspace_id: str
    run_id: str
    run_step_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkflowStepClaim:
    """The exact workflow step attempt opened by one scheduler claim."""

    workspace_id: str
    run_id: str
    run_step_id: str
    workflow_step_id: str
    runtime_attempt_id: str
    runtime_attempt_number: int
    service_instance_id: str
    fencing_generation: int
    claimed_at_us: int


@dataclass(frozen=True, slots=True)
class WorkflowLoopIterationClaim(WorkflowStepClaim):
    """One durable loop iteration claimed under a loop step's single attempt."""

    loop_iteration_id: str
    loop_iteration_number: int


def _lineage_id(*parts: str) -> str:
    digest = sha256("|".join(parts).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _run_step_id(
    *, workspace_id: str, run_id: str, plan_hash: str, step_id: str
) -> str:
    return _lineage_id("workflow_runtime_step", workspace_id, run_id, plan_hash, step_id)


def _step_kind(step: WorkflowPlanStep) -> str:
    return f"workflow.{step.route.lower()}"


def _expected_runtime_steps(
    *, workspace_id: str, run_id: str, plan_hash: str, steps: tuple[WorkflowPlanStep, ...]
) -> tuple[tuple[str, int, str], ...]:
    return tuple(
        (
            _run_step_id(
                workspace_id=workspace_id,
                run_id=run_id,
                plan_hash=plan_hash,
                step_id=step.step_id,
            ),
            step.sequence_index + 1,
            _step_kind(step),
        )
        for step in steps
    )


def _stored_runtime_steps(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> tuple[tuple[str, int, str], ...]:
    rows = connection.execute(
        "SELECT run_step_id, ordinal, step_kind FROM omnivia_runtime_run_steps "
        "WHERE workspace_id = ? AND run_id = ? ORDER BY ordinal",
        (workspace_id, run_id),
    ).fetchall()
    return tuple((str(row[0]), int(row[1]), str(row[2])) for row in rows)


def _connection_facts(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str, fact_kind: str
) -> dict[str, frozenset[tuple[str, str]]]:
    """One run's connection facts of one kind, as `(direction, connection_id)` per step."""
    facts: dict[str, set[tuple[str, str]]] = {}
    for fact in read_workflow_run_step_connections(
        connection, workspace_id=workspace_id, run_id=run_id, fact_kind=fact_kind
    ):
        facts.setdefault(fact.step_id, set()).add((fact.direction, fact.connection_id))
    return {step_id: frozenset(pairs) for step_id, pairs in facts.items()}


def _readiness_facts(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str, fact_kind: str
) -> dict[str, frozenset[str]]:
    """One run's mapped-input or capability facts, keyed by workflow step id."""
    facts: dict[str, set[str]] = {}
    for fact in read_workflow_run_step_readiness_facts(
        connection, workspace_id=workspace_id, run_id=run_id, fact_kind=fact_kind
    ):
        facts.setdefault(fact.step_id, set()).add(fact.fact_id)
    return {step_id: frozenset(values) for step_id, values in facts.items()}


def _selected_connection_details(
    selected: frozenset[tuple[str, str]],
) -> dict[str, object]:
    """The selected connection ids a claim event states, split by direction."""
    return {
        key: sorted(
            connection_id
            for direction, connection_id in selected
            if direction == wanted
        )
        for key, wanted in (
            ("selected_incoming_connections", CONNECTION_INCOMING),
            ("selected_outgoing_connections", CONNECTION_OUTGOING),
        )
    }


def _readiness_details(
    *, mapped_inputs: frozenset[str], capabilities: frozenset[str]
) -> dict[str, object]:
    details: dict[str, object] = {}
    if mapped_inputs:
        details["mapped_inputs_ready"] = sorted(mapped_inputs)
    if capabilities:
        details["capability_grants"] = sorted(capabilities)
    return details


def _now_us(clock: Clock) -> int:
    return int(clock.wall_time().timestamp() * 1_000_000)


def _run_status_for_steps(statuses: tuple[str, ...]) -> str:
    if any(status == "failed" for status in statuses):
        return RUN_STATUS_FAILED
    if all(status in RUN_STEP_TERMINAL_STATUSES for status in statuses):
        if all(status == "succeeded" for status in statuses):
            return RUN_STATUS_SUCCEEDED
        return RUN_STATUS_PARTIALLY_COMPLETED
    return RUN_STATUS_RUNNING


def _loop_bound(step: WorkflowPlanStep, key: str) -> int:
    if step.loop is None:
        raise WorkflowSchedulingError(f"workflow step {step.step_id!r} is not a loop")
    value = step.loop.get(key)
    if not isinstance(value, int):
        raise WorkflowSchedulingError(
            f"workflow step {step.step_id!r} has no integer loop {key!r}"
        )
    return value


def open_workflow_runtime_steps(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    run_id: str,
    opened_at_us: int,
) -> WorkflowRuntimeStepOpening:
    """Open one admitted workflow run into canonical pending runtime steps.

    Replaying the same opening returns the stored rows and replays only idempotent plan
    observations. Existing rows that do not match the workflow plan are refused rather
    than repaired, because changing runtime-step lineage would rewrite history.
    """
    with fenced_transaction(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ):
        binding = read_workflow_run_binding(
            connection, workspace_id=workspace_id, run_id=run_id
        )
        if binding is None:
            raise WorkflowSchedulingError(
                f"run {run_id!r} is not admitted as a workflow run"
            )
        plan = read_workflow_plan(
            connection,
            workspace_id=workspace_id,
            workflow_id=binding.workflow_id,
            workflow_version=binding.workflow_version,
        )
        if plan is None:  # pragma: no cover - 0027's foreign key makes it exist
            raise WorkflowSchedulingError(
                f"run {run_id!r} is bound to a workflow plan this workspace lost"
            )
        expected = _expected_runtime_steps(
            workspace_id=workspace_id,
            run_id=run_id,
            plan_hash=binding.plan_hash,
            steps=plan.steps,
        )
        stored = _stored_runtime_steps(connection, workspace_id=workspace_id, run_id=run_id)
        if stored:
            if stored != expected:
                raise WorkflowSchedulingError(
                    f"run {run_id!r} already has runtime steps that do not match its "
                    "workflow plan"
                )
        else:
            runtime = transaction_local_writer(connection, workspace_id=workspace_id)
            for step, (run_step_id, ordinal, kind) in zip(plan.steps, expected, strict=True):
                runtime.append_run_step(
                    run_id=run_id,
                    run_step_id=run_step_id,
                    ordinal=ordinal,
                    step_kind=kind,
                    created_at_us=opened_at_us,
                )
        workflow = transaction_local_workflow_writer(connection, workspace_id=workspace_id)
        for step in plan.steps:
            workflow.observe_plan_record(
                run_id=run_id,
                step=step,
                observed_at_us=opened_at_us,
            )
        # Force the canonical reader to prove the rows are usable before commit returns.
        if len(read_run_steps(connection, workspace_id=workspace_id, run_id=run_id)) != len(
            plan.steps
        ):
            raise WorkflowSchedulingError(
                f"run {run_id!r} did not open every workflow step"
            )
        return WorkflowRuntimeStepOpening(
            workspace_id=workspace_id,
            run_id=run_id,
            run_step_ids=tuple(row[0] for row in expected),
        )


@dataclass
class WorkflowScheduler:
    """Dependency-aware scheduler for Workflow Runs.

    This mutating slice handles deterministic dependency readiness over stored workflow
    plan steps, branch outcomes, the durable suspension and resolution of a wait step,
    durable loop iterations, and shared-pool capacity. Executor dispatch remains later
    M4 work, but generic runtime scheduling no longer claims workflow runs.

    `capacity` is optional. Without it every ready step is claimable, which is what the
    scheduler did before pools existed; with it a step that cannot have a slot is
    suspended on a durable capacity wait rather than claimed.

    `wait_purposes` is optional too. It declares, per workflow step, why that step
    suspends; a step with no declaration suspends for `event`, which is the whole of
    what an M2 plan record states about a wait step. It is not durable, for the reason
    :class:`WorkflowCapacityPolicy` is not: nothing in 0018 or 0027 stores a purpose,
    and a column invented for one would claim a plan-shape decision the runtime has not
    made. What *is* durable is the consequence -- the wait's kind, its deadline, the
    resume digest the purpose is folded into, and the run event naming all of it.
    """

    connection: sqlite3.Connection
    identity: ServiceInstanceIdentity
    workspace_id: str
    fencing_generation: int
    clock: Clock
    capacity: WorkflowCapacityPolicy | None = None
    wait_purposes: Mapping[str, WorkflowWaitPurpose] = field(default_factory=dict)
    #: How many attempts a step gets before a retryable failure is final. The default
    #: retries nothing, so a scheduler that states no policy behaves exactly as it did
    #: before retries existed.
    retry: WorkflowRetryPolicy = WorkflowRetryPolicy()
    #: Which roles may order a governed compensation or repair. The default authorises
    #: neither, so a scheduler that states no governance refuses both.
    governance: WorkflowGovernancePolicy = WorkflowGovernancePolicy()

    def cancel_run(self, *, run_id: str, stop_request_id: str) -> str:
        """Cancel one workflow run through RT-207's stop request/outcome ledger."""
        current_status = self._current_run_status(run_id)
        if current_status in RUN_TERMINAL_STATUSES:
            return current_status
        now_us = _now_us(self.clock)
        request_run_stop(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
            run_id=run_id,
            run_stop_id=stop_request_id,
            stop_reason=STOP_REASON_CANCELLED,
            running_work=RUNNING_WORK_RELEASE,
            requested_at_us=now_us,
            audit_ref=self._audit_ref(run_id),
        )
        return settle_run_stop(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
            run_id=run_id,
            runtime_event_id=_lineage_id(
                "workflow_cancelled", self.workspace_id, run_id, stop_request_id
            ),
            settled_at_us=now_us,
        )

    def claim_next_ready_step(self, *, run_id: str) -> WorkflowStepClaim | None:
        """Start the next pending workflow step whose dependencies succeeded."""
        with fenced_transaction(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
        ):
            plan, expected = self._plan_and_expected(run_id)
            if self._current_run_status(run_id) in RUN_TERMINAL_STATUSES:
                return None
            runtime_steps = {
                step.run_step_id: step
                for step in read_run_steps(
                    self.connection, workspace_id=self.workspace_id, run_id=run_id
                )
            }
            succeeded_step_ids = {
                step.step_id
                for step, (run_step_id, _, _) in zip(
                    plan.steps, expected, strict=True
                )
                if run_step_id in runtime_steps
                and runtime_steps[run_step_id].status == "succeeded"
            }
            branch_outcomes = {
                observation.step_id: observation.branch_outcome
                for observation in read_workflow_run_observations(
                    self.connection,
                    workspace_id=self.workspace_id,
                    run_id=run_id,
                    observation_kind=OBSERVATION_BRANCH,
                )
            }
            required_connections = _connection_facts(
                self.connection,
                workspace_id=self.workspace_id,
                run_id=run_id,
                fact_kind=CONNECTION_REQUIRED,
            )
            selected_connections = _connection_facts(
                self.connection,
                workspace_id=self.workspace_id,
                run_id=run_id,
                fact_kind=CONNECTION_SELECTED,
            )
            required_inputs = _readiness_facts(
                self.connection,
                workspace_id=self.workspace_id,
                run_id=run_id,
                fact_kind=READINESS_MAPPED_INPUT_REQUIRED,
            )
            ready_inputs = _readiness_facts(
                self.connection,
                workspace_id=self.workspace_id,
                run_id=run_id,
                fact_kind=READINESS_MAPPED_INPUT_READY,
            )
            required_capabilities = _readiness_facts(
                self.connection,
                workspace_id=self.workspace_id,
                run_id=run_id,
                fact_kind=READINESS_CAPABILITY_REQUIRED,
            )
            granted_capabilities = _readiness_facts(
                self.connection,
                workspace_id=self.workspace_id,
                run_id=run_id,
                fact_kind=READINESS_CAPABILITY_GRANTED,
            )
            queued = {waiter.run_step_id for waiter in self._capacity_waiters()}
            for source_step, (run_step_id, _, _) in zip(
                plan.steps, expected, strict=True
            ):
                runtime_step = runtime_steps.get(run_step_id)
                if runtime_step is None:
                    raise WorkflowSchedulingError(
                        f"run {run_id!r} has not opened workflow runtime steps"
                    )
                if runtime_step.status not in ("pending", "running", "waiting"):
                    continue
                if runtime_step.status == "waiting" and run_step_id not in queued:
                    # A route wait holds its step until `resolve_wait_step` lifts it. A
                    # capacity wait is this scheduler's own queue entry, so a step
                    # holding one is reconsidered on every pass.
                    continue
                if runtime_step.status == "running" and source_step.loop is None:
                    continue
                if any(dep not in succeeded_step_ids for dep in source_step.depends_on):
                    continue
                if source_step.branch is not None:
                    branch_outcome = branch_outcomes.get(source_step.step_id)
                    if branch_outcome in (None, BRANCH_BLOCKED):
                        continue
                    if branch_outcome == BRANCH_UNMATCHED:
                        self._skip_step(
                            run_id=run_id,
                            run_step_id=run_step_id,
                            workflow_step_id=source_step.step_id,
                        )
                        continue
                    if branch_outcome != BRANCH_MATCHED:
                        raise WorkflowSchedulingError(
                            f"workflow step {source_step.step_id!r} has unknown "
                            f"branch outcome {branch_outcome!r}"
                        )
                required = required_connections.get(source_step.step_id, frozenset())
                selected = selected_connections.get(source_step.step_id, frozenset())
                if not required <= selected:
                    # A readiness gate, in the same family as dependencies and the
                    # branch: DOC-005 makes a component ready only once every selected
                    # connection condition holds, and this run has not recorded that
                    # yet. Not ready is not a failure, so nothing is written and the
                    # step stays `pending`. A step this run declared no requirement for
                    # is unaffected: the empty set is a subset of everything.
                    continue
                mapped_required = required_inputs.get(
                    source_step.step_id, frozenset()
                )
                mapped_ready = ready_inputs.get(source_step.step_id, frozenset())
                if not mapped_required <= mapped_ready:
                    continue
                capability_required = required_capabilities.get(
                    source_step.step_id, frozenset()
                )
                capability_granted = granted_capabilities.get(
                    source_step.step_id, frozenset()
                )
                if not capability_required <= capability_granted:
                    continue
                if (
                    runtime_step.status != "running"
                    and source_step.route != ROUTE_WAIT
                    and not self._grant_capacity(
                        run_id=run_id,
                        run_step_id=run_step_id,
                        workflow_step_id=source_step.step_id,
                        route=source_step.route,
                    )
                ):
                    # No slot, or a better-ranked waiter holds the queue ahead of this
                    # step. A step already `running` is not re-gated: it is the thing
                    # occupying the slot, and asking it to queue for one would deadlock
                    # a continuing loop against itself.
                    return None
                if source_step.loop is not None:
                    return self._claim_loop_iteration(
                        run_id=run_id,
                        run_step_id=run_step_id,
                        source_step=source_step,
                        runtime_step=runtime_step,
                        required=required,
                        selected=selected,
                        mapped_inputs=mapped_ready,
                        capabilities=capability_granted,
                    )
                if source_step.route == ROUTE_WAIT:
                    # A wait step is suspended, never dispatched: it opens one durable
                    # wait and leaves `waiting`, so no worker attempt is claimed for it.
                    # Replaying is idempotent because the step is no longer `pending`.
                    self._open_step_wait(
                        run_id=run_id,
                        run_step_id=run_step_id,
                        workflow_step_id=source_step.step_id,
                    )
                    continue
                if any(
                    attempt.status == ATTEMPT_STATUS_RUNNING
                    for attempt in runtime_step.attempts
                ):
                    continue
                now_us = _now_us(self.clock)
                attempt_number = len(runtime_step.attempts) + 1
                attempt_id = _lineage_id(
                    "workflow_attempt",
                    self.workspace_id,
                    run_id,
                    run_step_id,
                    str(attempt_number),
                )
                event_sequence = (
                    read_run_sequence(
                        self.connection, workspace_id=self.workspace_id, run_id=run_id
                    )
                    + 1
                )
                event_id = _lineage_id(
                    "workflow_event",
                    self.workspace_id,
                    run_id,
                    str(event_sequence),
                    run_step_id,
                )
                writer = transaction_local_writer(
                    self.connection, workspace_id=self.workspace_id
                )
                writer.record_step_status(
                    run_step_id=run_step_id,
                    status="running",
                    observed_at_us=now_us,
                )
                writer.start_attempt(
                    attempt_id=attempt_id,
                    run_id=run_id,
                    run_step_id=run_step_id,
                    attempt_number=attempt_number,
                    started_at_us=now_us,
                )
                details: dict[str, object] = {
                    "workspace_id": self.workspace_id,
                    "run_id": run_id,
                    "run_step_id": run_step_id,
                    "workflow_step_id": source_step.step_id,
                    "runtime_attempt_id": attempt_id,
                    "runtime_attempt_number": attempt_number,
                    "service_instance_id": self.identity.service_instance_id,
                    "fencing_generation": self.fencing_generation,
                }
                if required:
                    # Only a gated step states its selections. A pure step has none to
                    # report, and an empty pair of lists on it would read as "nothing
                    # was selected" rather than "nothing applies". What is reported is
                    # what this run actually recorded as selected, which the gate has
                    # just proved covers everything the step required.
                    details.update(_selected_connection_details(selected))
                details.update(
                    _readiness_details(
                        mapped_inputs=mapped_ready,
                        capabilities=capability_granted,
                    )
                )
                writer.append_run_event(
                    run_id=run_id,
                    runtime_event_id=event_id,
                    occurred_at_us=now_us,
                    event_kind="workflow_step_claimed",
                    run_status=RUN_STATUS_RUNNING,
                    run_step_id=run_step_id,
                    message="workflow scheduler claimed the next ready step",
                    details=details,
                )
                return WorkflowStepClaim(
                    workspace_id=self.workspace_id,
                    run_id=run_id,
                    run_step_id=run_step_id,
                    workflow_step_id=source_step.step_id,
                    runtime_attempt_id=attempt_id,
                    runtime_attempt_number=attempt_number,
                    service_instance_id=self.identity.service_instance_id,
                    fencing_generation=self.fencing_generation,
                    claimed_at_us=now_us,
                )
            return None

    def _claim_loop_iteration(
        self,
        *,
        run_id: str,
        run_step_id: str,
        source_step: WorkflowPlanStep,
        runtime_step: RunStep,
        required: frozenset[tuple[str, str]],
        selected: frozenset[tuple[str, str]],
        mapped_inputs: frozenset[str],
        capabilities: frozenset[str],
    ) -> WorkflowLoopIterationClaim | None:
        """Claim the next durable iteration for a loop step.

        The runtime attempt is the loop step's lifetime. It starts once, on the first
        iteration, and remains open while the append-only loop ledger records each
        iteration beside it.
        """
        now_us = _now_us(self.clock)
        if runtime_step.status in ("pending", "waiting"):
            attempt_number = 1
            attempt_id = _lineage_id(
                "workflow_attempt",
                self.workspace_id,
                run_id,
                run_step_id,
                str(attempt_number),
            )
            writer = transaction_local_writer(
                self.connection, workspace_id=self.workspace_id
            )
            writer.record_step_status(
                run_step_id=run_step_id,
                status="running",
                observed_at_us=now_us,
            )
            writer.start_attempt(
                attempt_id=attempt_id,
                run_id=run_id,
                run_step_id=run_step_id,
                attempt_number=attempt_number,
                started_at_us=now_us,
            )
        else:
            running_attempts = tuple(
                attempt
                for attempt in runtime_step.attempts
                if attempt.status == ATTEMPT_STATUS_RUNNING
            )
            if len(running_attempts) != 1:
                raise WorkflowSchedulingError(
                    f"workflow loop step {source_step.step_id!r} is running without "
                    "one open runtime attempt"
                )
            attempt = running_attempts[0]
            attempt_id = attempt.attempt_id
            attempt_number = attempt.attempt_number
        iterations = read_workflow_loop_iterations(
            self.connection,
            workspace_id=self.workspace_id,
            run_id=run_id,
            step_id=source_step.step_id,
        )
        if any(iteration.is_open for iteration in iterations):
            return None
        if any(iteration.exit_reason is not None for iteration in iterations):
            return None
        loop_iteration_id = _lineage_id(
            "workflow_loop_iteration",
            self.workspace_id,
            run_id,
            source_step.step_id,
            attempt_id,
            str(len(iterations) + 1),
        )
        try:
            loop_iteration_number = transaction_local_workflow_writer(
                self.connection, workspace_id=self.workspace_id
            ).open_loop_iteration(
                run_id=run_id,
                step_id=source_step.step_id,
                loop_iteration_id=loop_iteration_id,
                runtime_attempt_id=attempt_id,
                opened_at_us=now_us,
            )
        except sqlite3.IntegrityError as error:
            raise WorkflowSchedulingError(
                f"workflow loop step {source_step.step_id!r} could not claim an "
                "iteration"
            ) from error
        details: dict[str, object] = {
            "workspace_id": self.workspace_id,
            "run_id": run_id,
            "run_step_id": run_step_id,
            "workflow_step_id": source_step.step_id,
            "runtime_attempt_id": attempt_id,
            "runtime_attempt_number": attempt_number,
            "loop_iteration_id": loop_iteration_id,
            "loop_iteration_number": loop_iteration_number,
            "service_instance_id": self.identity.service_instance_id,
            "fencing_generation": self.fencing_generation,
        }
        if required:
            details.update(_selected_connection_details(selected))
        details.update(
            _readiness_details(mapped_inputs=mapped_inputs, capabilities=capabilities)
        )
        self._append_event(
            run_id=run_id,
            run_step_id=run_step_id,
            event_kind="workflow_loop_iteration_claimed",
            run_status=RUN_STATUS_RUNNING,
            message="workflow scheduler claimed the next loop iteration",
            details=details,
            now_us=now_us,
        )
        return WorkflowLoopIterationClaim(
            workspace_id=self.workspace_id,
            run_id=run_id,
            run_step_id=run_step_id,
            workflow_step_id=source_step.step_id,
            runtime_attempt_id=attempt_id,
            runtime_attempt_number=attempt_number,
            loop_iteration_id=loop_iteration_id,
            loop_iteration_number=loop_iteration_number,
            service_instance_id=self.identity.service_instance_id,
            fencing_generation=self.fencing_generation,
            claimed_at_us=now_us,
        )

    def _grant_capacity(
        self, *, run_id: str, run_step_id: str, workflow_step_id: str, route: str
    ) -> bool:
        """Return whether this step may take its configured shared-capacity slot."""
        if self.capacity is None:
            return True
        pool = self.capacity.pool_for(route)
        if pool is None:
            return True
        limit = self.capacity.pools.get(pool)
        if limit is None or limit < 1:
            raise WorkflowSchedulingError(
                f"workflow capacity pool {pool!r} is not available"
            )
        lane = self.capacity.lane_for(run_id)
        now_us = _now_us(self.clock)
        waiters = self._capacity_waiters()
        queued = next(
            (waiter for waiter in waiters if waiter.run_step_id == run_step_id),
            None,
        )
        candidate = queued or _CapacityWaiter(
            wait_id=_capacity_wait_id(
                workspace_id=self.workspace_id,
                run_id=run_id,
                run_step_id=run_step_id,
                pool=pool,
            ),
            run_id=run_id,
            run_step_id=run_step_id,
            pool=pool,
            lane=lane,
            enqueued_at_us=now_us,
        )
        pool_waiters = tuple(
            waiter for waiter in (*waiters, candidate) if waiter.pool == pool
        )
        best = min(pool_waiters, key=lambda waiter: self._capacity_rank(waiter, now_us))
        running = self._running_capacity_by_pool()
        if best != candidate or not self._capacity_has_slot(pool=pool, lane=lane, running=running):
            if queued is None:
                self._open_capacity_wait(
                    run_id=run_id,
                    run_step_id=run_step_id,
                    workflow_step_id=workflow_step_id,
                    pool=pool,
                    lane=lane,
                    priority=self.capacity.priorities.get(run_id, 0),
                    now_us=now_us,
                )
            return False
        if queued is not None:
            transaction_local_writer(
                self.connection, workspace_id=self.workspace_id
            ).close_wait(
                wait_id=queued.wait_id,
                status=_WAIT_STATUS_RESOLVED,
                resolved_at_us=now_us,
                resolution_reason=CAPACITY_WAIT_RESOLUTION_REASON,
            )
        return True

    def _capacity_rank(self, waiter: _CapacityWaiter, now_us: int) -> tuple[int, int, int, str, str]:
        if self.capacity is None:  # pragma: no cover - callers guard this path
            return (0, 0, waiter.enqueued_at_us, waiter.run_id, waiter.run_step_id)
        priority = self.capacity.priorities.get(waiter.run_id, 0)
        if self.capacity.escalation_us is not None and self.capacity.escalation_us > 0:
            priority += max(0, now_us - waiter.enqueued_at_us) // self.capacity.escalation_us
        recovery_rank = 0 if waiter.lane == CAPACITY_LANE_RECOVERY else 1
        return (-priority, recovery_rank, waiter.enqueued_at_us, waiter.run_id, waiter.run_step_id)

    def _capacity_has_slot(
        self, *, pool: str, lane: str, running: dict[str, dict[str, int]]
    ) -> bool:
        if self.capacity is None:  # pragma: no cover - callers guard this path
            return True
        limit = self.capacity.pools[pool]
        pool_running = running.get(pool, {})
        default_running = pool_running.get(CAPACITY_LANE_DEFAULT, 0)
        recovery_running = pool_running.get(CAPACITY_LANE_RECOVERY, 0)
        total_running = default_running + recovery_running
        if total_running >= limit:
            return False
        reserved = self.capacity.recovery_reserved.get(pool, 0)
        if lane == CAPACITY_LANE_RECOVERY:
            return True
        return default_running < max(0, limit - reserved)

    def _running_capacity_by_pool(self) -> dict[str, dict[str, int]]:
        if self.capacity is None:
            return {}
        counts: dict[str, dict[str, int]] = {}
        for row in self.connection.execute(_RUNNING_WORKFLOW_STEPS, (self.workspace_id,)):
            run_id, step_kind = str(row[0]), str(row[1])
            pool = self.capacity.pool_for(_route_of(step_kind))
            if pool is None:
                continue
            lane = self.capacity.lane_for(run_id)
            counts.setdefault(pool, {}).setdefault(lane, 0)
            counts[pool][lane] += 1
        return counts

    def _capacity_waiters(self) -> tuple[_CapacityWaiter, ...]:
        if self.capacity is None:
            return ()
        waiters: list[_CapacityWaiter] = []
        for row in self.connection.execute(
            _PENDING_WORKFLOW_WAITS, (self.workspace_id, CAPACITY_WAIT_KIND)
        ):
            wait_id, run_id, run_step_id, created_at_us, step_kind, resume_digest = (
                str(row[0]),
                str(row[1]),
                str(row[2]),
                int(row[3]),
                str(row[4]),
                str(row[5]),
            )
            pool = self.capacity.pool_for(_route_of(step_kind))
            if pool is None:
                continue
            expected_resume, _ = _capacity_resume_digest(
                workspace_id=self.workspace_id,
                run_id=run_id,
                run_step_id=run_step_id,
                pool=pool,
            )
            if resume_digest != expected_resume:
                continue
            waiters.append(
                _CapacityWaiter(
                    wait_id=wait_id,
                    run_id=run_id,
                    run_step_id=run_step_id,
                    pool=pool,
                    lane=self.capacity.lane_for(run_id),
                    enqueued_at_us=created_at_us,
                )
            )
        return tuple(waiters)

    def _open_capacity_wait(
        self,
        *,
        run_id: str,
        run_step_id: str,
        workflow_step_id: str,
        pool: str,
        lane: str,
        priority: int,
        now_us: int,
    ) -> None:
        wait_id = _capacity_wait_id(
            workspace_id=self.workspace_id,
            run_id=run_id,
            run_step_id=run_step_id,
            pool=pool,
        )
        resume_digest, resume_preimage = _capacity_resume_digest(
            workspace_id=self.workspace_id,
            run_id=run_id,
            run_step_id=run_step_id,
            pool=pool,
        )
        purpose = WorkflowWaitPurpose(CAPACITY_WAIT_PURPOSE, checkpoint=pool)
        writer = transaction_local_writer(self.connection, workspace_id=self.workspace_id)
        writer.open_wait(
            wait_id=wait_id,
            run_id=run_id,
            run_step_id=run_step_id,
            kind=CAPACITY_WAIT_KIND,
            created_at_us=now_us,
            resume_digest=resume_digest,
        )
        writer.record_step_status(
            run_step_id=run_step_id,
            status="waiting",
            observed_at_us=now_us,
        )
        self._append_event(
            run_id=run_id,
            run_step_id=run_step_id,
            event_kind="workflow_step_capacity_waiting",
            run_status=RUN_STATUS_RUNNING,
            message="workflow scheduler queued a step for shared capacity",
            details={
                "workspace_id": self.workspace_id,
                "run_id": run_id,
                "run_step_id": run_step_id,
                "workflow_step_id": workflow_step_id,
                "wait_id": wait_id,
                "wait_kind": CAPACITY_WAIT_KIND,
                "wait_purpose": purpose.purpose,
                "wait_checkpoint": purpose.checkpoint,
                "expected_resolution": purpose.resolution,
                "resume_digest": resume_digest,
                "resume_digest_preimage": resume_preimage,
                "capacity_pool": pool,
                "capacity_lane": lane,
                "capacity_priority": priority,
                "service_instance_id": self.identity.service_instance_id,
                "fencing_generation": self.fencing_generation,
            },
            now_us=now_us,
        )

    def _open_step_wait(
        self, *, run_id: str, run_step_id: str, workflow_step_id: str
    ) -> None:
        """Suspend one dependency-ready wait step on one durable runtime wait.

        The order is the migration's, not a choice: a `waiting` step must already have an
        unresolved wait naming it. :meth:`resolve_wait_step` lifts it again, and the
        `resume_digest` recorded here is what a later RT-107 `ResolveWait` has to quote.

        The step's declared :class:`WorkflowWaitPurpose` decides the canonical kind, the
        deadline and the resolution this suspension admits, and is part of both the wait
        identifier and the resume digest. That is what makes the suspension replay-safe
        without a purpose column: the same step at the same purpose and checkpoint
        derives the same wait, and a wait opened for a different purpose derives a
        different digest and is refused by :meth:`_require_workflow_resolution_identity`
        rather than resolved as if it were this one.
        """
        purpose = self._wait_purpose(workflow_step_id)
        writer = transaction_local_writer(self.connection, workspace_id=self.workspace_id)
        now_us = _now_us(self.clock)
        wait_id, _ = _wait_lineage(
            prefix="workflow_wait",
            workspace_id=self.workspace_id,
            run_id=run_id,
            run_step_id=run_step_id,
            purpose=purpose,
        )
        resume_digest, resume_preimage = _wait_lineage(
            prefix="workflow_resume",
            workspace_id=self.workspace_id,
            run_id=run_id,
            run_step_id=run_step_id,
            purpose=purpose,
        )
        writer.open_wait(
            wait_id=wait_id,
            run_id=run_id,
            run_step_id=run_step_id,
            kind=purpose.wait_kind,
            created_at_us=now_us,
            resume_digest=resume_digest,
            expires_at_us=purpose.expires_at_us,
        )
        writer.record_step_status(
            run_step_id=run_step_id,
            status="waiting",
            observed_at_us=now_us,
        )
        event_sequence = (
            read_run_sequence(
                self.connection, workspace_id=self.workspace_id, run_id=run_id
            )
            + 1
        )
        writer.append_run_event(
            run_id=run_id,
            runtime_event_id=_lineage_id(
                "workflow_event",
                self.workspace_id,
                run_id,
                str(event_sequence),
                run_step_id,
            ),
            occurred_at_us=now_us,
            event_kind="workflow_step_waiting",
            run_status=RUN_STATUS_WAITING,
            run_step_id=run_step_id,
            message="workflow scheduler suspended a wait step on a durable wait",
            details=self._wait_purpose_details(
                run_id=run_id,
                run_step_id=run_step_id,
                workflow_step_id=workflow_step_id,
                wait_id=wait_id,
                purpose=purpose,
                resume_digest=resume_digest,
                resume_preimage=resume_preimage,
            ),
        )

    def _wait_purpose(self, workflow_step_id: str) -> WorkflowWaitPurpose:
        """What one wait step is suspended for, or the honest default for an M2 plan."""
        return self.wait_purposes.get(workflow_step_id, _DEFAULT_WAIT_PURPOSE)

    def _wait_purpose_details(
        self,
        *,
        run_id: str,
        run_step_id: str,
        workflow_step_id: str,
        wait_id: str,
        purpose: WorkflowWaitPurpose,
        resume_digest: str,
        resume_preimage: str,
    ) -> dict[str, object]:
        """Everything a suspension states about why it happened and what ends it.

        The purpose, its canonical kind, the checkpoint it names, the digest preimage
        and the resolution shape a resumption must arrive in. The wait row carries only
        the kind and the digest, so this event is the sole durable record of the other
        four -- and of why two `external_signal` waits on one run are different waits.
        """
        details: dict[str, object] = {
            "workspace_id": self.workspace_id,
            "run_id": run_id,
            "run_step_id": run_step_id,
            "workflow_step_id": workflow_step_id,
            "wait_id": wait_id,
            "wait_kind": purpose.wait_kind,
            "wait_purpose": purpose.purpose,
            "wait_checkpoint": purpose.checkpoint,
            "expected_resolution": purpose.resolution,
            "resume_digest": resume_digest,
            "resume_digest_preimage": resume_preimage,
            "service_instance_id": self.identity.service_instance_id,
            "fencing_generation": self.fencing_generation,
        }
        if purpose.approver_role is not None:
            details["approver_role"] = purpose.approver_role
        if purpose.expires_at_us is not None:
            details["wait_expires_at_us"] = purpose.expires_at_us
        return details

    def resolve_wait_step(self, *, run_id: str, workflow_step_id: str) -> str:
        """Resolve the wait one suspended workflow wait step is held by, and return the
        run's status.

        The wait's whole work is the waiting, so resolving it succeeds the step: there is
        no attempt to resume, because the suspension never dispatched one. The run's event
        stream still has to pass through `running` before it may reach a terminal status,
        so a wait that ends the run appends two events rather than jumping `waiting` ->
        `succeeded`, which 0018 refuses.

        Replaying a resolution of an already succeeded wait step returns the run status
        and writes nothing. Every other non-`waiting` status is refused: a wait step this
        scheduler never suspended has no resolution to replay.
        """
        with fenced_transaction(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
        ):
            current_status = self._current_run_status(run_id)
            if current_status in RUN_TERMINAL_STATUSES:
                return current_status
            plan, expected = self._plan_and_expected(run_id)
            run_step_id = next(
                (
                    candidate
                    for step, (candidate, _, _) in zip(plan.steps, expected, strict=True)
                    if step.step_id == workflow_step_id and step.route == ROUTE_WAIT
                ),
                None,
            )
            if run_step_id is None:
                raise WorkflowSchedulingError(
                    f"run {run_id!r} has no workflow wait step {workflow_step_id!r}"
                )
            steps = read_run_steps(
                self.connection, workspace_id=self.workspace_id, run_id=run_id
            )
            step = next(item for item in steps if item.run_step_id == run_step_id)
            if step.status == "succeeded":
                return _run_status_for_steps(tuple(item.status for item in steps))
            if step.status != "waiting":
                raise WorkflowSchedulingError(
                    f"workflow step {workflow_step_id!r} is {step.status!r}, not waiting "
                    "on a resolvable wait"
                )
            wait = next(
                (
                    candidate
                    for candidate in read_run_waits(
                        self.connection, workspace_id=self.workspace_id, run_id=run_id
                    )
                    if candidate.run_step_id == run_step_id
                    and candidate.status == WAIT_STATUS_PENDING
                ),
                None,
            )
            if wait is None:  # pragma: no cover - 0018 refuses a waiting step with none
                raise WorkflowSchedulingError(
                    f"workflow step {workflow_step_id!r} is waiting on no pending wait"
                )
            if wait.kind != WORKFLOW_WAIT_KIND:
                # In-process resolution states only that workflow scheduling ended the
                # wait. That is the whole truth for an external signal and a lie for the
                # other two kinds: an approval wait is settled by a decision this
                # scheduler does not hold, and a timer wait by a deadline it does not
                # check. Both go through `resolve_wait_step_command`, which has the
                # policy seam and the deadline rule that make those settlements honest.
                raise WorkflowSchedulingError(
                    f"workflow step {workflow_step_id!r} is suspended on a "
                    f"{wait.kind!r} wait, which is resolved through the RT-107 command "
                    "rather than in-process"
                )
            writer = transaction_local_writer(
                self.connection, workspace_id=self.workspace_id
            )
            now_us = _now_us(self.clock)
            writer.close_wait(
                wait_id=wait.wait_id,
                status=_WAIT_STATUS_RESOLVED,
                resolved_at_us=now_us,
                resolution_reason=WORKFLOW_WAIT_RESOLUTION_REASON,
            )
            writer.record_step_status(
                run_step_id=run_step_id,
                status="succeeded",
                observed_at_us=now_us,
            )
            details = {
                "workspace_id": self.workspace_id,
                "run_id": run_id,
                "run_step_id": run_step_id,
                "workflow_step_id": workflow_step_id,
                "wait_id": wait.wait_id,
                "wait_kind": wait.kind,
                "resume_digest": wait.resume_digest,
                "service_instance_id": self.identity.service_instance_id,
                "fencing_generation": self.fencing_generation,
            }
            self._append_event(
                run_id=run_id,
                run_step_id=run_step_id,
                event_kind="workflow_step_wait_resolved",
                run_status=RUN_STATUS_RUNNING,
                message="workflow scheduler resolved the wait holding a step",
                details=details,
                now_us=now_us,
            )
            run_status = _run_status_for_steps(
                tuple(
                    item.status
                    for item in read_run_steps(
                        self.connection, workspace_id=self.workspace_id, run_id=run_id
                    )
                )
            )
            if run_status != RUN_STATUS_RUNNING:
                self._append_event(
                    run_id=run_id,
                    run_step_id=run_step_id,
                    event_kind=(
                        "workflow_run_succeeded"
                        if run_status == RUN_STATUS_SUCCEEDED
                        else "workflow_run_partially_completed"
                    ),
                    run_status=run_status,
                    message=(
                        "workflow scheduler completed the run"
                        if run_status == RUN_STATUS_SUCCEEDED
                        else "workflow scheduler completed the run with skipped work"
                    ),
                    details=details,
                    now_us=now_us,
                )
            return run_status

    def resolve_wait_step_command(
        self,
        *,
        grant: MutationGrant,
        context: AuthorizedApplicationContext,
        equivalence: IdempotencyEquivalence,
        command: ResolveWait,
        policy: WaitResolutionPolicy,
        runtime_event_id: str,
        validate_result: ResultValidator,
        expected: RuntimeAggregateExpectation,
    ) -> MutationOutcome:
        """Resolve a workflow route wait through RT-107 command/idempotency machinery.

        Generic RT-107 resumes a wait step to `running` because it suspended an active
        attempt. Workflow route waits deliberately have no attempt: the external signal
        is the step's result. This seam therefore reuses RT-107's command shape,
        authorization, policy, idempotency and sequence checks, then applies workflow's
        own transition: resolve the wait, succeed the wait step and terminalize only
        after the legal `waiting -> running -> terminal` event path.
        """

        def settle(
            writer: RuntimeWriter, settlement: MutationSettlementContext
        ) -> Mapping[str, object]:
            if writer.workspace_id != self.workspace_id:
                raise MutationDenied(
                    f"the grant covers workspace {writer.workspace_id!r}, not "
                    f"workflow scheduler workspace {self.workspace_id!r}"
                )
            if command.workspace_id != self.workspace_id:
                raise MutationDenied(
                    f"the command names workspace {command.workspace_id!r}, which this "
                    f"scheduler does not cover"
                )
            wait = self._stored_workflow_wait(command)
            status = self._require_workflow_resolution_identity(command, wait)
            if wait.status != WAIT_STATUS_PENDING:
                return self._replayed_workflow_wait_resolution(
                    command, wait, status, policy, context
                )
            run_status = self._current_run_status(command.run_id)
            if run_status != RUN_STATUS_WAITING:
                raise WaitResolutionConflict(
                    f"run {command.run_id!r} is {run_status!r}, not waiting, so it has "
                    "no workflow wait to resolve"
                )
            # A `timer` purpose is the only one that opens a wait with a deadline, and
            # the same rule RT-107 applies to a generic wait applies to this one: an
            # expiry before the deadline, or an outcome after it, is refused.
            require_deadline_honoured(
                self.connection,
                self.workspace_id,
                command,
                status,
                settlement.settled_at_us,
            )
            approval = self._policy_approval(policy, context, command, wait)
            with _semantic_refusal():
                validate_resolve_wait(
                    command,
                    wait=wait,
                    approval=approval,
                    run_status=run_status,
                )
            now_us = settlement.settled_at_us
            writer.close_wait(
                wait_id=wait.wait_id,
                status=status,
                resolved_at_us=now_us,
                resolution_reason=command.reason,
                approval_id=command.approval_id,
            )
            writer.record_step_status(
                run_step_id=wait.run_step_id,
                status="succeeded",
                observed_at_us=now_us,
            )
            workflow_step_id = self._workflow_wait_step_id(
                command.run_id, wait.run_step_id
            )
            details = {
                "workspace_id": self.workspace_id,
                "run_id": command.run_id,
                "run_step_id": wait.run_step_id,
                "workflow_step_id": workflow_step_id,
                "wait_id": wait.wait_id,
                "wait_kind": wait.kind,
                "resume_digest": wait.resume_digest,
                "service_instance_id": self.identity.service_instance_id,
                "fencing_generation": self.fencing_generation,
                "rt107_resolution": command.resolution,
            }
            self._append_event(
                run_id=command.run_id,
                run_step_id=wait.run_step_id,
                event_kind="workflow_step_wait_resolved",
                run_status=RUN_STATUS_RUNNING,
                message="workflow scheduler resolved a wait through RT-107",
                details=details,
                now_us=now_us,
            )
            run_status = _run_status_for_steps(
                tuple(
                    item.status
                    for item in read_run_steps(
                        self.connection,
                        workspace_id=self.workspace_id,
                        run_id=command.run_id,
                    )
                )
            )
            if run_status != RUN_STATUS_RUNNING:
                self._append_event(
                    run_id=command.run_id,
                    run_step_id=wait.run_step_id,
                    event_kind=(
                        "workflow_run_succeeded"
                        if run_status == RUN_STATUS_SUCCEEDED
                        else "workflow_run_partially_completed"
                    ),
                    run_status=run_status,
                    message=(
                        "workflow scheduler completed the run"
                        if run_status == RUN_STATUS_SUCCEEDED
                        else "workflow scheduler completed the run with skipped work"
                    ),
                    details=details,
                    now_us=now_us,
                )
            return self._wait_resolution_result(
                wait,
                status=status,
                reason=command.reason,
                approval_id=command.approval_id,
            )

        return execute_runtime_command(
            self.connection,
            self.identity,
            grant=grant,
            context=context,
            equivalence=equivalence,
            command=settle,
            validate_result=validate_result,
            clock=self.clock,
            expected=expected,
        )

    def _append_event(
        self,
        *,
        run_id: str,
        run_step_id: str | None,
        event_kind: str,
        run_status: str,
        message: str,
        details: dict[str, object],
        now_us: int,
        runtime_event_id: str | None = None,
    ) -> None:
        """Append one run event at the run's next sequence, under its own lineage.

        `runtime_event_id` is for the events whose identity is *not* the position they
        landed at: a governed command's audit event is identified by the command, so a
        replay of that command derives the same event id and is recognised rather than
        appended twice.
        """
        event_sequence = (
            read_run_sequence(
                self.connection, workspace_id=self.workspace_id, run_id=run_id
            )
            + 1
        )
        transaction_local_writer(
            self.connection, workspace_id=self.workspace_id
        ).append_run_event(
            run_id=run_id,
            runtime_event_id=runtime_event_id
            or _lineage_id(
                "workflow_event",
                self.workspace_id,
                run_id,
                str(event_sequence),
                run_step_id or run_id,
            ),
            occurred_at_us=now_us,
            event_kind=event_kind,
            run_status=run_status,
            run_step_id=run_step_id,
            message=message,
            details=details,
        )

    def _append_or_replay_event(
        self,
        *,
        run_id: str,
        run_step_id: str | None,
        runtime_event_id: str,
        event_kind: str,
        run_status: str,
        message: str,
        details: dict[str, object],
        now_us: int,
        allow_append: bool = True,
    ) -> bool:
        """Append a deterministic command event, or accept an identical replay."""
        details_json = to_canonical_json(details)
        stored = self.connection.execute(
            "SELECT run_id, run_step_id, event_kind, run_status, message, details_json "
            "FROM omnivia_runtime_events WHERE workspace_id = ? "
            "AND runtime_event_id = ?",
            (self.workspace_id, runtime_event_id),
        ).fetchone()
        if stored is not None:
            existing = (
                str(stored[0]),
                None if stored[1] is None else str(stored[1]),
                str(stored[2]),
                str(stored[3]),
                None if stored[4] is None else str(stored[4]),
                None if stored[5] is None else str(stored[5]),
            )
            requested = (
                run_id,
                run_step_id,
                event_kind,
                run_status,
                message,
                details_json,
            )
            if existing != requested:
                raise WorkflowSchedulingError(
                    f"workflow command event {runtime_event_id!r} already records a "
                    "different governed request"
                )
            return False
        if not allow_append:
            self._refuse_new_governed_history_on_terminal(
                run_id=run_id, current_status=run_status
            )
        self._append_event(
            run_id=run_id,
            run_step_id=run_step_id,
            event_kind=event_kind,
            run_status=run_status,
            message=message,
            details=details,
            now_us=now_us,
            runtime_event_id=runtime_event_id,
        )
        return True

    def _append_or_replay_evidence(
        self,
        *,
        run_id: str,
        run_step_id: str,
        evidence_item_id: str,
        evidence_kind: str,
        source: ExternalReference,
        content_checksum: str,
        captured_at_us: int,
        allow_append: bool = True,
    ) -> bool:
        """Append non-authoritative repair evidence, or accept an identical replay."""
        stored = self.connection.execute(
            "SELECT run_id, run_step_id, evidence_kind, source_kind, source_id, "
            "source_workspace_id, content_checksum, artifact_id, authoritative, retained "
            "FROM omnivia_runtime_evidence WHERE workspace_id = ? "
            "AND evidence_item_id = ?",
            (self.workspace_id, evidence_item_id),
        ).fetchone()
        if stored is not None:
            existing = (
                str(stored[0]),
                None if stored[1] is None else str(stored[1]),
                str(stored[2]),
                str(stored[3]),
                str(stored[4]),
                str(stored[5]),
                str(stored[6]),
                None if stored[7] is None else str(stored[7]),
                bool(stored[8]),
                bool(stored[9]),
            )
            requested = (
                run_id,
                run_step_id,
                evidence_kind,
                source.source_kind,
                source.source_id,
                source.workspace_id,
                content_checksum,
                None,
                False,
                True,
            )
            if existing != requested:
                raise WorkflowSchedulingError(
                    f"workflow repair evidence {evidence_item_id!r} already records "
                    "different evidence"
                )
            return False
        if not allow_append:
            self._refuse_new_governed_history_on_terminal(
                run_id=run_id, current_status=self._current_run_status(run_id)
            )
        transaction_local_writer(
            self.connection, workspace_id=self.workspace_id
        ).append_evidence_item(
            evidence_item_id=evidence_item_id,
            run_id=run_id,
            run_step_id=run_step_id,
            evidence_kind=evidence_kind,
            source=source,
            content_checksum=content_checksum,
            captured_at_us=captured_at_us,
            authoritative=False,
            retained=True,
        )
        return True

    def _governed_run_step(
        self, *, run_id: str, workflow_step_id: str
    ) -> tuple[str, str]:
        """Return an opened runtime step id and current run status for governance."""
        plan, expected = self._plan_and_expected(run_id)
        run_step_id = next(
            (
                candidate
                for step, (candidate, _, _) in zip(plan.steps, expected, strict=True)
                if step.step_id == workflow_step_id
            ),
            None,
        )
        if run_step_id is None:
            raise WorkflowSchedulingError(
                f"run {run_id!r} has no workflow step {workflow_step_id!r}"
            )
        current_status = self._current_run_status(run_id)
        return run_step_id, current_status

    def _refuse_new_governed_history_on_terminal(
        self, *, run_id: str, current_status: str
    ) -> None:
        if current_status in RUN_TERMINAL_STATUSES:
            raise WorkflowSchedulingError(
                f"run {run_id!r} is terminal and admits no governed recovery history"
            )

    def _require_digest(self, value: str, *, label: str) -> None:
        if (
            not value.startswith("sha256:")
            or len(value) != 71
            or any(character not in "0123456789abcdef" for character in value[7:])
        ):
            raise WorkflowSchedulingError(f"{label} must be a sha256 content digest")

    def _compensation_event_id(self, compensation_id: str, stage: str) -> str:
        return _lineage_id(
            "workflow_compensation", self.workspace_id, compensation_id, stage
        )

    def record_compensation(
        self,
        *,
        run_id: str,
        workflow_step_id: str,
        compensation_id: str,
        authority: WorkflowGovernedAuthority,
        outcome: str,
        result_digest: str,
    ) -> str:
        """Record forward-only compensation audit for a non-terminal workflow run.

        This first-release seam does not pretend to dispatch a compensating workflow
        step. It records the governed command and its reported result beside the run,
        under deterministic event identities, and leaves all prior step, attempt and
        effect truth untouched.
        """
        if not self.governance.permits_compensation(authority):
            raise WorkflowGovernanceDenied(
                f"role {authority.actor_role!r} may not order workflow compensation"
            )
        if outcome not in COMPENSATION_OUTCOMES:
            raise WorkflowSchedulingError(f"unsupported compensation outcome {outcome!r}")
        self._require_digest(result_digest, label="compensation result_digest")
        with fenced_transaction(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
        ):
            run_step_id, current_status = self._governed_run_step(
                run_id=run_id, workflow_step_id=workflow_step_id
            )
            now_us = _now_us(self.clock)
            started_details = {
                "workspace_id": self.workspace_id,
                "run_id": run_id,
                "run_step_id": run_step_id,
                "workflow_step_id": workflow_step_id,
                "compensation_id": compensation_id,
                "governed_authority": authority.facts(),
                "service_instance_id": self.identity.service_instance_id,
                "fencing_generation": self.fencing_generation,
            }
            settled_details = {
                **started_details,
                "compensation_outcome": outcome,
                "result_digest": result_digest,
            }
            self._append_or_replay_event(
                run_id=run_id,
                run_step_id=run_step_id,
                runtime_event_id=self._compensation_event_id(
                    compensation_id, "started"
                ),
                event_kind=COMPENSATION_STARTED_EVENT,
                run_status=current_status,
                message="workflow scheduler recorded governed compensation start",
                details=started_details,
                now_us=now_us,
                allow_append=current_status not in RUN_TERMINAL_STATUSES,
            )
            self._append_or_replay_event(
                run_id=run_id,
                run_step_id=run_step_id,
                runtime_event_id=self._compensation_event_id(
                    compensation_id, "settled"
                ),
                event_kind=COMPENSATION_SETTLED_EVENT,
                run_status=current_status,
                message="workflow scheduler recorded governed compensation result",
                details=settled_details,
                now_us=now_us,
                allow_append=current_status not in RUN_TERMINAL_STATUSES,
            )
            return current_status

    def _repair_event_id(self, repair_id: str) -> str:
        return _lineage_id("workflow_governed_repair", self.workspace_id, repair_id)

    def _repair_evidence_id(self, repair_id: str) -> str:
        return _lineage_id("workflow_governed_repair_evidence", self.workspace_id, repair_id)

    def record_governed_repair(
        self,
        *,
        run_id: str,
        workflow_step_id: str,
        repair_id: str,
        authority: WorkflowGovernedAuthority,
        evidence_source_id: str,
        evidence_checksum: str,
        summary: str,
    ) -> str:
        """Record a governed repair as forward-only non-authoritative evidence."""
        if not self.governance.permits_repair(authority):
            raise WorkflowGovernanceDenied(
                f"role {authority.actor_role!r} may not record governed workflow repair"
            )
        if not evidence_source_id:
            raise WorkflowSchedulingError("governed repair evidence_source_id is required")
        if not summary:
            raise WorkflowSchedulingError("governed repair summary is required")
        self._require_digest(evidence_checksum, label="repair evidence_checksum")
        with fenced_transaction(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
        ):
            run_step_id, current_status = self._governed_run_step(
                run_id=run_id, workflow_step_id=workflow_step_id
            )
            source = ExternalReference(
                source_kind=GOVERNED_REPAIR_EVIDENCE_SOURCE,
                source_id=evidence_source_id,
                workspace_id=self.workspace_id,
            )
            now_us = _now_us(self.clock)
            evidence_item_id = self._repair_evidence_id(repair_id)
            event_details = {
                "workspace_id": self.workspace_id,
                "run_id": run_id,
                "run_step_id": run_step_id,
                "workflow_step_id": workflow_step_id,
                "repair_id": repair_id,
                "evidence_item_id": evidence_item_id,
                "evidence_kind": GOVERNED_REPAIR_EVIDENCE_KIND,
                "evidence_source": source.to_wire(),
                "evidence_checksum": evidence_checksum,
                "summary": summary,
                "governed_authority": authority.facts(),
                "service_instance_id": self.identity.service_instance_id,
                "fencing_generation": self.fencing_generation,
            }
            self._append_or_replay_evidence(
                run_id=run_id,
                run_step_id=run_step_id,
                evidence_item_id=evidence_item_id,
                evidence_kind=GOVERNED_REPAIR_EVIDENCE_KIND,
                source=source,
                content_checksum=evidence_checksum,
                captured_at_us=now_us,
                allow_append=current_status not in RUN_TERMINAL_STATUSES,
            )
            self._append_or_replay_event(
                run_id=run_id,
                run_step_id=run_step_id,
                runtime_event_id=self._repair_event_id(repair_id),
                event_kind=GOVERNED_REPAIR_EVENT,
                run_status=current_status,
                message="workflow scheduler recorded governed repair evidence",
                details=event_details,
                now_us=now_us,
                allow_append=current_status not in RUN_TERMINAL_STATUSES,
            )
            return current_status

    def _skip_step(
        self, *, run_id: str, run_step_id: str, workflow_step_id: str
    ) -> None:
        writer = transaction_local_writer(self.connection, workspace_id=self.workspace_id)
        now_us = _now_us(self.clock)
        writer.record_step_status(
            run_step_id=run_step_id,
            status="skipped",
            observed_at_us=now_us,
        )
        steps = read_run_steps(
            self.connection, workspace_id=self.workspace_id, run_id=run_id
        )
        run_status = _run_status_for_steps(tuple(step.status for step in steps))
        event_sequence = (
            read_run_sequence(
                self.connection, workspace_id=self.workspace_id, run_id=run_id
            )
            + 1
        )
        event_id = _lineage_id(
            "workflow_event",
            self.workspace_id,
            run_id,
            str(event_sequence),
            run_step_id,
        )
        writer.append_run_event(
            run_id=run_id,
            runtime_event_id=event_id,
            occurred_at_us=now_us,
            event_kind=(
                "workflow_run_partially_completed"
                if run_status == RUN_STATUS_PARTIALLY_COMPLETED
                else "workflow_step_skipped"
            ),
            run_status=run_status,
            run_step_id=run_step_id,
            message=(
                "workflow scheduler completed the run with skipped work"
                if run_status == RUN_STATUS_PARTIALLY_COMPLETED
                else "workflow scheduler skipped an unmatched branch step"
            ),
            details={
                "workspace_id": self.workspace_id,
                "run_id": run_id,
                "run_step_id": run_step_id,
                "workflow_step_id": workflow_step_id,
                "service_instance_id": self.identity.service_instance_id,
                "fencing_generation": self.fencing_generation,
            },
        )

    def complete_step(self, claim: WorkflowStepClaim) -> str:
        """Complete one claimed workflow step, terminalizing the run only at the end."""
        return self._complete_step(claim)

    def complete_step_with_result(
        self,
        claim: WorkflowStepClaim,
        *,
        work_unit: WorkUnitEnvelope,
        result: WorkResultEnvelope,
        failure: ApiError | None = None,
    ) -> str:
        """Complete one claimed step only after its worker result envelope validates.

        `failure` is the typed reason a `FAILED` result failed, and belongs to no other
        outcome. It is durable in two ways: it is the attempt's recorded `ApiError`
        rather than the synthesized one, and its retry class is what decides whether the
        step gets another attempt under :attr:`retry` or fails the run here.
        """
        validated = self._require_result_for_claim(claim, work_unit, result)
        if failure is not None and validated.outcome != OUTCOME_FAILED:
            raise WorkflowSchedulingError(
                f"a {validated.outcome!r} workflow result carries no failure; only a "
                "FAILED one does"
            )
        return self._complete_step(claim, result=validated, failure=failure)

    def _complete_step(
        self,
        claim: WorkflowStepClaim,
        *,
        result: WorkResultEnvelope | None = None,
        failure: ApiError | None = None,
    ) -> str:
        """Complete one claimed workflow step after any caller-specific fencing checks."""
        if isinstance(claim, WorkflowLoopIterationClaim):
            raise WorkflowSchedulingError(
                "loop iteration claims must be completed with complete_loop_iteration"
            )
        self._require_scheduler_claim(claim)
        with fenced_transaction(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
        ):
            if self._current_run_status(claim.run_id) in RUN_TERMINAL_STATUSES:
                raise WorkflowSchedulingError(
                    f"run {claim.run_id!r} is terminal and cannot accept workflow "
                    "step completion"
                )
            self._require_open_attempt(claim)
            if self._claim_route(claim) == ROUTE_EFFECT:
                raise WorkflowSchedulingError(
                    f"workflow step {claim.workflow_step_id!r} is an EFFECT-route "
                    "step and must be completed through effect settlement"
                )
            writer = transaction_local_writer(
                self.connection, workspace_id=self.workspace_id
            )
            now_us = _now_us(self.clock)
            # A validated result is the step's truth, whichever way it went: a FAILED
            # outcome fails the attempt and the step, and the run status that follows is
            # whatever the durable step states legally imply.
            failed = result is not None and result.outcome == OUTCOME_FAILED
            if (
                failed
                and failure is not None
                and is_error_retryable(failure)
                and self.retry.permits_retry(claim.runtime_attempt_number)
            ):
                return self._schedule_retry(claim, failure=failure, now_us=now_us)
            writer.finish_attempt(
                attempt_id=claim.runtime_attempt_id,
                status=ATTEMPT_STATUS_FAILED if failed else ATTEMPT_STATUS_SUCCEEDED,
                finished_at_us=now_us,
                failure=(
                    failure
                    or ApiError(
                        code="internal",
                        message="workflow step result reported failure",
                        retry_class="non_retryable",
                    )
                )
                if failed
                else None,
            )
            writer.record_step_status(
                run_step_id=claim.run_step_id,
                status="failed" if failed else "succeeded",
                observed_at_us=now_us,
            )
            steps = read_run_steps(
                self.connection, workspace_id=self.workspace_id, run_id=claim.run_id
            )
            run_status = _run_status_for_steps(tuple(step.status for step in steps))
            event_sequence = (
                read_run_sequence(
                    self.connection,
                    workspace_id=self.workspace_id,
                    run_id=claim.run_id,
                )
                + 1
            )
            event_id = _lineage_id(
                "workflow_event",
                self.workspace_id,
                claim.run_id,
                str(event_sequence),
                claim.run_step_id,
            )
            details = {
                "workspace_id": self.workspace_id,
                "run_id": claim.run_id,
                "run_step_id": claim.run_step_id,
                "workflow_step_id": claim.workflow_step_id,
                "runtime_attempt_id": claim.runtime_attempt_id,
                "runtime_attempt_number": claim.runtime_attempt_number,
                "service_instance_id": self.identity.service_instance_id,
                "fencing_generation": self.fencing_generation,
            }
            if result is not None:
                details.update(
                    {
                        "work_unit_hash": result.work_unit_hash,
                        "result_digest": result.result_digest,
                        "executor_source_id": result.source_id,
                        "executor_id": result.executor_id,
                        "executor_version": result.executor_version,
                        "executor_content_hash": result.executor_content_hash,
                        "result_fence": result.fence,
                        "result_outcome": result.outcome,
                    }
                )
            writer.append_run_event(
                run_id=claim.run_id,
                runtime_event_id=event_id,
                occurred_at_us=now_us,
                event_kind=(
                    "workflow_run_failed"
                    if run_status == RUN_STATUS_FAILED
                    else "workflow_run_succeeded"
                    if run_status == RUN_STATUS_SUCCEEDED
                    else "workflow_run_partially_completed"
                    if run_status == RUN_STATUS_PARTIALLY_COMPLETED
                    else "workflow_step_failed"
                    if failed
                    else "workflow_step_succeeded"
                ),
                run_status=run_status,
                run_step_id=claim.run_step_id,
                message=(
                    "workflow scheduler failed the run"
                    if run_status == RUN_STATUS_FAILED
                    else "workflow scheduler completed the run"
                    if run_status == RUN_STATUS_SUCCEEDED
                    else "workflow scheduler completed the run with skipped work"
                    if run_status == RUN_STATUS_PARTIALLY_COMPLETED
                    else "workflow scheduler failed a step"
                    if failed
                    else "workflow scheduler completed a step"
                ),
                details=details,
            )
            return run_status

    def _retry_wait_lineage(
        self, *, run_id: str, run_step_id: str, next_attempt_number: int
    ) -> tuple[WorkflowWaitPurpose, str, str, str]:
        """The purpose, wait id, resume digest and preimage of one step's retry wait.

        Derived, never stored: `(step, retry_backoff, attempt:N)` is the wait's identity,
        so the same failed attempt derives the same wait however many times the scheduler
        recomputes it, and the wait a *different* attempt backs off for is a different
        wait rather than a silent reuse of this one.
        """
        purpose = WorkflowWaitPurpose(
            RETRY_WAIT_PURPOSE, checkpoint=f"attempt:{next_attempt_number}"
        )
        wait_id, _ = _wait_lineage(
            prefix="workflow_retry_wait",
            workspace_id=self.workspace_id,
            run_id=run_id,
            run_step_id=run_step_id,
            purpose=purpose,
        )
        resume_digest, resume_preimage = _wait_lineage(
            prefix="workflow_retry_resume",
            workspace_id=self.workspace_id,
            run_id=run_id,
            run_step_id=run_step_id,
            purpose=purpose,
        )
        return purpose, wait_id, resume_digest, resume_preimage

    def _schedule_retry(
        self, claim: WorkflowStepClaim, *, failure: ApiError, now_us: int
    ) -> str:
        """Fail this attempt and suspend its step on a durable retry-backoff wait.

        The step is deliberately not recorded `failed`. 0018 makes a failed step state
        final, so terminalizing the step here would spend it to say "try this again".
        What is recorded instead is the truth of the attempt -- failed, with the caller's
        typed `ApiError` -- plus one `retry_backoff` wait naming the attempt it backs off
        for, and the step as `waiting`. Those are the same three facts a route wait
        records, which is why a restarted scheduler sees the suspension in storage rather
        than having to re-derive that a retry was owed.
        """
        purpose, wait_id, resume_digest, resume_preimage = self._retry_wait_lineage(
            run_id=claim.run_id,
            run_step_id=claim.run_step_id,
            next_attempt_number=claim.runtime_attempt_number + 1,
        )
        writer = transaction_local_writer(
            self.connection, workspace_id=self.workspace_id
        )
        writer.finish_attempt(
            attempt_id=claim.runtime_attempt_id,
            status=ATTEMPT_STATUS_FAILED,
            finished_at_us=now_us,
            failure=failure,
        )
        writer.open_wait(
            wait_id=wait_id,
            run_id=claim.run_id,
            run_step_id=claim.run_step_id,
            kind=purpose.wait_kind,
            created_at_us=now_us,
            resume_digest=resume_digest,
        )
        writer.record_step_status(
            run_step_id=claim.run_step_id,
            status="waiting",
            observed_at_us=now_us,
        )
        details = self._wait_purpose_details(
            run_id=claim.run_id,
            run_step_id=claim.run_step_id,
            workflow_step_id=claim.workflow_step_id,
            wait_id=wait_id,
            purpose=purpose,
            resume_digest=resume_digest,
            resume_preimage=resume_preimage,
        )
        details.update(
            {
                "runtime_attempt_id": claim.runtime_attempt_id,
                "runtime_attempt_number": claim.runtime_attempt_number,
                "next_attempt_number": claim.runtime_attempt_number + 1,
                "max_attempts": self.retry.max_attempts,
                "failure_code": failure.code,
                "failure_retry_class": failure.retry_class,
            }
        )
        if failure.retry_after_ms is not None:
            # The backoff itself is the caller's to serve: an `external_signal` wait
            # carries no deadline, so this is the delay the failure asked for, recorded
            # beside the suspension rather than a timer this runtime pretends to hold.
            details["retry_after_ms"] = failure.retry_after_ms
        self._append_event(
            run_id=claim.run_id,
            run_step_id=claim.run_step_id,
            event_kind="workflow_step_retry_scheduled",
            run_status=RUN_STATUS_WAITING,
            message="workflow scheduler suspended a failed step on a retry backoff wait",
            details=details,
            now_us=now_us,
        )
        return RUN_STATUS_WAITING

    def release_retry_wait(self, *, run_id: str, workflow_step_id: str) -> str:
        """Release the retry wait holding one failed step, back to `pending`.

        Only the wait is resolved here. The next attempt is opened by ordinary
        :meth:`claim_next_ready_step`, which numbers it from the attempts the step
        already carries -- so the release states "this step may run again" and nothing
        about what runs, and releasing twice cannot produce two attempts.

        Replay-safe by state rather than by a marker: a step already `pending` has no
        suspension left to lift, so a repeated release returns the run status and writes
        nothing. Any other non-`waiting` status, and any wait that is not this step's own
        retry wait, is refused rather than resolved as if it were one.
        """
        with fenced_transaction(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
        ):
            current_status = self._current_run_status(run_id)
            if current_status in RUN_TERMINAL_STATUSES:
                return current_status
            plan, expected = self._plan_and_expected(run_id)
            run_step_id = next(
                (
                    candidate
                    for step, (candidate, _, _) in zip(plan.steps, expected, strict=True)
                    if step.step_id == workflow_step_id
                ),
                None,
            )
            if run_step_id is None:
                raise WorkflowSchedulingError(
                    f"run {run_id!r} has no workflow step {workflow_step_id!r}"
                )
            steps = read_run_steps(
                self.connection, workspace_id=self.workspace_id, run_id=run_id
            )
            step = next(item for item in steps if item.run_step_id == run_step_id)
            if step.status == "pending":
                return _run_status_for_steps(tuple(item.status for item in steps))
            if step.status != "waiting":
                raise WorkflowSchedulingError(
                    f"workflow step {workflow_step_id!r} is {step.status!r}, not waiting "
                    "on a retry"
                )
            _, wait_id, resume_digest, _ = self._retry_wait_lineage(
                run_id=run_id,
                run_step_id=run_step_id,
                next_attempt_number=len(step.attempts) + 1,
            )
            wait = next(
                (
                    candidate
                    for candidate in read_run_waits(
                        self.connection, workspace_id=self.workspace_id, run_id=run_id
                    )
                    if candidate.wait_id == wait_id
                    and candidate.status == WAIT_STATUS_PENDING
                ),
                None,
            )
            if wait is None or wait.resume_digest != resume_digest:
                raise WorkflowSchedulingError(
                    f"workflow step {workflow_step_id!r} is not suspended on the retry "
                    f"wait for attempt {len(step.attempts) + 1}"
                )
            writer = transaction_local_writer(
                self.connection, workspace_id=self.workspace_id
            )
            now_us = _now_us(self.clock)
            writer.close_wait(
                wait_id=wait.wait_id,
                status=_WAIT_STATUS_RESOLVED,
                resolved_at_us=now_us,
                resolution_reason=RETRY_WAIT_RESOLUTION_REASON,
            )
            writer.record_step_status(
                run_step_id=run_step_id,
                status="pending",
                observed_at_us=now_us,
            )
            self._append_event(
                run_id=run_id,
                run_step_id=run_step_id,
                event_kind="workflow_step_retry_ready",
                run_status=RUN_STATUS_RUNNING,
                message="workflow scheduler released a step from its retry backoff wait",
                details={
                    "workspace_id": self.workspace_id,
                    "run_id": run_id,
                    "run_step_id": run_step_id,
                    "workflow_step_id": workflow_step_id,
                    "wait_id": wait.wait_id,
                    "wait_kind": wait.kind,
                    "wait_purpose": RETRY_WAIT_PURPOSE,
                    "resume_digest": wait.resume_digest,
                    "next_attempt_number": len(step.attempts) + 1,
                    "service_instance_id": self.identity.service_instance_id,
                    "fencing_generation": self.fencing_generation,
                },
                now_us=now_us,
            )
            return RUN_STATUS_RUNNING

    def declare_effect_intent(
        self, claim: WorkflowStepClaim, intent: EffectIntent
    ) -> EffectIntent:
        """Declare one durable effect for a claimed EFFECT-route workflow step.

        The scheduler adds no rule of its own to what RT-205 already enforces: the
        authority check, the running-run check and the idempotency-key classification are
        `RuntimeWriter.declare_effect_intent`'s, read from this database rather than taken
        from the caller. What is added here is *ownership* -- the intent must name the
        exact step and attempt this claim opened, on an EFFECT-route step this scheduler
        still holds -- so a claim cannot declare an effect against somebody else's work.
        """
        self._require_effect_claim(claim)
        with fenced_transaction(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
        ):
            if self._current_run_status(claim.run_id) in RUN_TERMINAL_STATUSES:
                raise WorkflowSchedulingError(
                    f"run {claim.run_id!r} is terminal and cannot declare a new effect"
                )
            self._require_open_attempt(claim)
            self._require_intent_of_claim(claim, intent)
            declared = transaction_local_writer(
                self.connection, workspace_id=self.workspace_id
            ).declare_effect_intent(intent)
            self._append_event(
                run_id=claim.run_id,
                run_step_id=claim.run_step_id,
                event_kind="workflow_effect_intended",
                run_status=RUN_STATUS_RUNNING,
                message="workflow scheduler declared an effect before anything acted on it",
                details=self._effect_details(
                    claim, effect_intent_id=declared.effect_intent_id
                ),
                now_us=_now_us(self.clock),
            )
            return declared

    def dispatch_effect_step(
        self, claim: WorkflowStepClaim, *, effect_intent_id: str
    ) -> DispatchRequest:
        """Hand out the dispatch request a claimed step's committed effect entitles.

        The ordering rule stays RT-205's, not this scheduler's: the request is produced by
        :func:`publish_dispatch_request`, which refuses a caller still holding a
        transaction, reads the intent in a fenced transaction of its own -- so an
        uncommitted declaration publishes nothing -- refuses an already settled effect, and
        numbers the dispatch in the same transaction it returns from. Nothing is duplicated
        here, because a second copy of that rule is a second place for it to drift.

        What the scheduler adds is the same *ownership* `declare_effect_intent` adds: an
        EFFECT-route step this scheduler still holds, a run that has not gone terminal, the
        attempt the claim opened still open, and an intent naming that exact step and
        attempt. Redelivery is the point rather than an edge case -- a replay of the same
        intent carries the same `idempotency_key` and `request_digest` and a higher
        `dispatch_number`, which is what makes the at-least-once channel safe to retry over.

        The return value is the request and nothing else. It asserts no delivery, no
        receipt and no success: a published request that is never receipted settles
        `unknown`, and the only thing that turns an effect `committed` is an observation
        recorded against it.

        The run event is appended after the outbox row commits, in a second transaction,
        because RT-205 will not produce a request while this caller holds one. That order
        is the safe one: a crash between them loses an event, never invents a dispatch.
        """
        self._require_effect_claim(claim)
        if self._current_run_status(claim.run_id) in RUN_TERMINAL_STATUSES:
            raise WorkflowSchedulingError(
                f"run {claim.run_id!r} is terminal and dispatches no further effect"
            )
        self._require_open_attempt(claim)
        intent = read_effect_intent(
            self.connection,
            workspace_id=self.workspace_id,
            effect_intent_id=effect_intent_id,
        )
        if intent is None:
            raise WorkflowSchedulingError(
                f"effect intent {effect_intent_id!r} is not declared in this workspace; "
                "there is no dispatch request without a durable intent"
            )
        self._require_intent_of_claim(claim, intent)
        now_us = _now_us(self.clock)
        request = publish_dispatch_request(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
            effect_intent_id=effect_intent_id,
            requested_at_us=now_us,
        )
        with fenced_transaction(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
        ):
            self._append_event(
                run_id=claim.run_id,
                run_step_id=claim.run_step_id,
                event_kind="workflow_effect_dispatch_requested",
                run_status=RUN_STATUS_RUNNING,
                message=(
                    "workflow scheduler published a dispatch request for a declared effect"
                ),
                details=self._effect_details(
                    claim,
                    effect_intent_id=request.effect_intent_id,
                    effect_kind=request.effect_kind,
                    idempotency_key=request.idempotency_key,
                    dispatch_number=request.dispatch_number,
                ),
                now_us=now_us,
            )
        return request

    def settle_effect_step(
        self,
        claim: WorkflowStepClaim,
        *,
        effect_intent_id: str,
        effect_settlement_id: str,
    ) -> str:
        """Settle a claimed EFFECT step from what RT-205 durably holds about its effect.

        The outcome is never an argument. :func:`decide_settlement` answers from the
        intent, its observation and its dispatch count, all read inside the same fenced
        transaction the attempt and run transition are written in -- so nothing can be
        observed or dispatched between the reading and the answering, and no path here can
        report a success the record does not hold.

        Each of its three answers has exactly one step and run consequence:

        * `committed` -- the attempt succeeded, the step succeeded, and the run status is
          whatever the durable step states imply;
        * `not_committed` -- the effect was never handed out, so the attempt and the step
          failed and the run failed with them;
        * `unknown` -- the attempt is `uncertain` and carries no failure, the step is
          suspended on a durable `effect_reconciliation` wait, and the run goes
          `uncertain`. Uncertainty is not failure: the reconciliation that answers it is
          RT-206's, not this scheduler's, and nothing here fabricates either half of the
          answer it could not establish. What the wait adds is a *recovery path* -- the
          suspension is in storage, so a restarted scheduler sees that an answer is owed
          rather than having to re-derive it, and :meth:`reconcile_effect_step` is the
          one command that lifts it.
        """
        self._require_effect_claim(claim)
        with fenced_transaction(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
        ):
            if self._current_run_status(claim.run_id) in RUN_TERMINAL_STATUSES:
                raise WorkflowSchedulingError(
                    f"run {claim.run_id!r} is terminal and cannot accept workflow "
                    "effect settlement"
                )
            self._require_open_attempt(claim)
            intent = read_effect_intent(
                self.connection,
                workspace_id=self.workspace_id,
                effect_intent_id=effect_intent_id,
            )
            if intent is None:
                raise WorkflowSchedulingError(
                    f"effect intent {effect_intent_id!r} is not declared in this "
                    "workspace; there is no effect settlement without an effect intent"
                )
            self._require_intent_of_claim(claim, intent)
            receipt = read_effect_receipt_for_intent(
                self.connection,
                workspace_id=self.workspace_id,
                effect_intent_id=effect_intent_id,
            )
            outcome, reason = decide_settlement(
                intent=intent,
                receipt=receipt,
                dispatch_count=read_effect_dispatch_count(
                    self.connection,
                    workspace_id=self.workspace_id,
                    effect_intent_id=effect_intent_id,
                ),
            )
            now_us = _now_us(self.clock)
            writer = transaction_local_writer(
                self.connection, workspace_id=self.workspace_id
            )
            writer.settle_effect(
                EffectSettlement(
                    workspace_id=self.workspace_id,
                    effect_settlement_id=effect_settlement_id,
                    run_id=claim.run_id,
                    effect_intent_id=effect_intent_id,
                    outcome=outcome,
                    settled_at=runtime_timestamp(now_us),
                    reason=reason,
                    audit_reference=self._audit_ref(claim.run_id),
                    effect_receipt_id=(
                        receipt.effect_receipt_id
                        if outcome == EFFECT_OUTCOME_COMMITTED and receipt is not None
                        else None
                    ),
                )
            )
            details = self._effect_details(
                claim,
                effect_intent_id=effect_intent_id,
                effect_settlement_id=effect_settlement_id,
                effect_outcome=outcome,
                effect_reason=reason,
            )
            if outcome == EFFECT_OUTCOME_UNKNOWN:
                writer.finish_attempt(
                    attempt_id=claim.runtime_attempt_id,
                    status=ATTEMPT_STATUS_UNCERTAIN,
                    finished_at_us=now_us,
                )
                purpose, wait_id, resume_digest, resume_preimage = (
                    self._reconciliation_wait_lineage(
                        run_id=claim.run_id,
                        run_step_id=claim.run_step_id,
                        attempt_number=claim.runtime_attempt_number,
                    )
                )
                writer.open_wait(
                    wait_id=wait_id,
                    run_id=claim.run_id,
                    run_step_id=claim.run_step_id,
                    kind=purpose.wait_kind,
                    created_at_us=now_us,
                    resume_digest=resume_digest,
                )
                writer.record_step_status(
                    run_step_id=claim.run_step_id,
                    status="waiting",
                    observed_at_us=now_us,
                )
                details.update(
                    self._wait_purpose_details(
                        run_id=claim.run_id,
                        run_step_id=claim.run_step_id,
                        workflow_step_id=claim.workflow_step_id,
                        wait_id=wait_id,
                        purpose=purpose,
                        resume_digest=resume_digest,
                        resume_preimage=resume_preimage,
                    )
                )
                self._append_event(
                    run_id=claim.run_id,
                    run_step_id=claim.run_step_id,
                    event_kind="workflow_effect_uncertain",
                    # The run stays `uncertain` rather than going `waiting`: it is
                    # holding an effect nobody can account for, which is what
                    # `uncertain` says and `waiting` would quietly drop -- and 0018
                    # refuses `uncertain -> waiting` for exactly that reason. The
                    # suspension is stated by the step and the wait beneath it.
                    run_status=RUN_STATUS_UNCERTAIN,
                    message=(
                        "workflow scheduler could not establish whether an effect "
                        "landed, and suspended the step for reconciliation"
                    ),
                    details=details,
                    now_us=now_us,
                )
                return RUN_STATUS_UNCERTAIN
            failed = outcome != EFFECT_OUTCOME_COMMITTED
            writer.finish_attempt(
                attempt_id=claim.runtime_attempt_id,
                status=ATTEMPT_STATUS_FAILED if failed else ATTEMPT_STATUS_SUCCEEDED,
                finished_at_us=now_us,
                failure=ApiError(
                    code="internal",
                    message=f"workflow effect settled {outcome!r} for {reason!r}",
                    retry_class="non_retryable",
                )
                if failed
                else None,
            )
            writer.record_step_status(
                run_step_id=claim.run_step_id,
                status="failed" if failed else "succeeded",
                observed_at_us=now_us,
            )
            run_status = _run_status_for_steps(
                tuple(
                    step.status
                    for step in read_run_steps(
                        self.connection,
                        workspace_id=self.workspace_id,
                        run_id=claim.run_id,
                    )
                )
            )
            self._append_event(
                run_id=claim.run_id,
                run_step_id=claim.run_step_id,
                event_kind=(
                    "workflow_run_failed"
                    if run_status == RUN_STATUS_FAILED
                    else "workflow_run_succeeded"
                    if run_status == RUN_STATUS_SUCCEEDED
                    else "workflow_run_partially_completed"
                    if run_status == RUN_STATUS_PARTIALLY_COMPLETED
                    else "workflow_effect_committed"
                ),
                run_status=run_status,
                message=(
                    "workflow scheduler failed the run"
                    if run_status == RUN_STATUS_FAILED
                    else "workflow scheduler completed the run"
                    if run_status == RUN_STATUS_SUCCEEDED
                    else "workflow scheduler completed the run with skipped work"
                    if run_status == RUN_STATUS_PARTIALLY_COMPLETED
                    else "workflow scheduler settled a committed effect"
                ),
                details=details,
                now_us=now_us,
            )
            return run_status

    def _reconciliation_wait_lineage(
        self, *, run_id: str, run_step_id: str, attempt_number: int
    ) -> tuple[WorkflowWaitPurpose, str, str, str]:
        """The purpose, wait id, resume digest and preimage of one reconciliation wait.

        Derived, never stored, exactly as :meth:`_retry_wait_lineage` is:
        `(step, effect_reconciliation, attempt:N)` is the wait's identity, so the same
        uncertain attempt derives the same wait however many times the scheduler
        recomputes it, and the suspension a *different* attempt is owed a reconciliation
        for is a different wait rather than a silent reuse of this one.
        """
        purpose = WorkflowWaitPurpose(
            RECONCILIATION_WAIT_PURPOSE, checkpoint=f"attempt:{attempt_number}"
        )
        wait_id, _ = _wait_lineage(
            prefix="workflow_reconciliation_wait",
            workspace_id=self.workspace_id,
            run_id=run_id,
            run_step_id=run_step_id,
            purpose=purpose,
        )
        resume_digest, resume_preimage = _wait_lineage(
            prefix="workflow_reconciliation_resume",
            workspace_id=self.workspace_id,
            run_id=run_id,
            run_step_id=run_step_id,
            purpose=purpose,
        )
        return purpose, wait_id, resume_digest, resume_preimage

    def reconcile_effect_step(
        self,
        *,
        run_id: str,
        workflow_step_id: str,
        effect_intent_id: str,
        effect_reconciliation_id: str,
    ) -> str:
        """Answer one suspended EFFECT step's uncertain effect, and lift its suspension.

        Addressed by step rather than by claim, for the reason
        :meth:`release_retry_wait` is: the attempt that went uncertain is finished and
        the scheduler that opened it may be gone, so the recovery path has to be
        reachable by whichever owner currently holds the fence. Ownership is still
        required -- the fenced transaction refuses a stale generation, and the step must
        be an EFFECT-route step of a plan this scheduler still matches.

        The final outcome is not derived here. :func:`reconcile_effect_transaction` reads
        the intent, its settlement, its retained receipt and its dispatch count in one
        fenced transaction of its own and answers from those four; this seam only checks
        that the suspension it is lifting is the one that effect is owed, and then
        records what RT-206 concluded:

        * `committed` -- a receipt arrived after the effect went uncertain, so the step
          succeeds and the run status is whatever the durable step states imply;
        * `not_committed` -- the effect was never handed out, so the step and the run
          fail.

        A dispatched effect with no retained receipt is neither. RT-206 refuses it, this
        command writes nothing, and the step stays suspended on the same wait -- which is
        the honest state for a question this database cannot answer, and leaves the
        reconciliation reachable again the moment evidence is retained.

        Replay-safe by state. A terminal run returns its status and writes nothing; a
        step already lifted off its wait by a stored reconciliation does the same; and a
        crash between RT-206's write and this seam's is recovered by calling again, since
        RT-206 answers an identical repeat from the store.
        """
        run_step_id = self._effect_step_id(run_id, workflow_step_id)
        current_status = self._current_run_status(run_id)
        if current_status in RUN_TERMINAL_STATUSES:
            return current_status
        steps = read_run_steps(
            self.connection, workspace_id=self.workspace_id, run_id=run_id
        )
        step = next(item for item in steps if item.run_step_id == run_step_id)
        stored = read_effect_reconciliation_for_intent(
            self.connection,
            workspace_id=self.workspace_id,
            effect_intent_id=effect_intent_id,
        )
        if (
            stored is not None
            and stored.effect_reconciliation_id != effect_reconciliation_id
        ):
            raise WorkflowSchedulingError(
                f"effect {effect_intent_id!r} is already reconciled as "
                f"{stored.effect_reconciliation_id!r}; a second, different final "
                "answer about one effect is a contradiction"
            )
        if step.status != "waiting":
            if step.status in RUN_STEP_TERMINAL_STATUSES and stored is not None:
                return _run_status_for_steps(tuple(item.status for item in steps))
            raise WorkflowSchedulingError(
                f"workflow step {workflow_step_id!r} is {step.status!r}, not suspended "
                "on an effect reconciliation wait"
            )
        attempt = step.attempts[-1] if step.attempts else None
        if attempt is None or attempt.status != ATTEMPT_STATUS_UNCERTAIN:
            raise WorkflowSchedulingError(
                f"workflow step {workflow_step_id!r} holds no uncertain attempt, so no "
                "effect of it is owed a reconciliation"
            )
        _, wait_id, resume_digest, _ = self._reconciliation_wait_lineage(
            run_id=run_id,
            run_step_id=run_step_id,
            attempt_number=attempt.attempt_number,
        )
        wait = next(
            (
                candidate
                for candidate in read_run_waits(
                    self.connection, workspace_id=self.workspace_id, run_id=run_id
                )
                if candidate.wait_id == wait_id
                and candidate.status == WAIT_STATUS_PENDING
            ),
            None,
        )
        if wait is None or wait.resume_digest != resume_digest:
            raise WorkflowSchedulingError(
                f"workflow step {workflow_step_id!r} is not suspended on the effect "
                f"reconciliation wait for attempt {attempt.attempt_number}"
            )
        intent = read_effect_intent(
            self.connection,
            workspace_id=self.workspace_id,
            effect_intent_id=effect_intent_id,
        )
        if intent is None:
            raise WorkflowSchedulingError(
                f"effect intent {effect_intent_id!r} is not declared in this workspace; "
                "there is no effect reconciliation without an effect intent"
            )
        if (intent.workspace_id, intent.run_id, intent.run_step_id, intent.attempt_id) != (
            self.workspace_id,
            run_id,
            run_step_id,
            attempt.attempt_id,
        ):
            raise WorkflowSchedulingError(
                f"effect intent {effect_intent_id!r} does not name the step and attempt "
                f"suspended on wait {wait_id!r}"
            )
        now_us = _now_us(self.clock)
        reconciliation = stored or reconcile_effect_transaction(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
            effect_intent_id=effect_intent_id,
            effect_reconciliation_id=effect_reconciliation_id,
            reconciled_at_us=now_us,
            audit_ref=self._audit_ref(run_id),
        )
        if reconciliation.outcome not in (
            EFFECT_OUTCOME_COMMITTED,
            EFFECT_OUTCOME_NOT_COMMITTED,
        ):  # pragma: no cover - RT-206 reaches no third outcome
            raise WorkflowSchedulingError(
                f"effect {effect_intent_id!r} reconciled {reconciliation.outcome!r}, "
                "which settles neither the step nor the run"
            )
        failed = reconciliation.outcome != EFFECT_OUTCOME_COMMITTED
        with fenced_transaction(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
        ):
            writer = transaction_local_writer(
                self.connection, workspace_id=self.workspace_id
            )
            writer.close_wait(
                wait_id=wait_id,
                status=_WAIT_STATUS_RESOLVED,
                resolved_at_us=now_us,
                resolution_reason=RECONCILIATION_WAIT_RESOLUTION_REASON,
            )
            writer.record_step_status(
                run_step_id=run_step_id,
                status="failed" if failed else "succeeded",
                observed_at_us=now_us,
            )
            run_status = _run_status_for_steps(
                tuple(
                    item.status
                    for item in read_run_steps(
                        self.connection, workspace_id=self.workspace_id, run_id=run_id
                    )
                )
            )
            self._append_event(
                run_id=run_id,
                run_step_id=run_step_id,
                event_kind=(
                    "workflow_run_failed"
                    if run_status == RUN_STATUS_FAILED
                    else "workflow_run_succeeded"
                    if run_status == RUN_STATUS_SUCCEEDED
                    else "workflow_run_partially_completed"
                    if run_status == RUN_STATUS_PARTIALLY_COMPLETED
                    else "workflow_effect_reconciled"
                ),
                run_status=run_status,
                message=(
                    "workflow scheduler failed the run"
                    if run_status == RUN_STATUS_FAILED
                    else "workflow scheduler completed the run"
                    if run_status == RUN_STATUS_SUCCEEDED
                    else "workflow scheduler completed the run with skipped work"
                    if run_status == RUN_STATUS_PARTIALLY_COMPLETED
                    else "workflow scheduler reconciled an uncertain effect"
                ),
                details={
                    "workspace_id": self.workspace_id,
                    "run_id": run_id,
                    "run_step_id": run_step_id,
                    "workflow_step_id": workflow_step_id,
                    "runtime_attempt_id": attempt.attempt_id,
                    "runtime_attempt_number": attempt.attempt_number,
                    "wait_id": wait_id,
                    "wait_kind": wait.kind,
                    "wait_purpose": RECONCILIATION_WAIT_PURPOSE,
                    "resume_digest": wait.resume_digest,
                    "effect_intent_id": effect_intent_id,
                    "effect_settlement_id": reconciliation.effect_settlement_id,
                    "effect_reconciliation_id": reconciliation.effect_reconciliation_id,
                    "effect_outcome": reconciliation.outcome,
                    "effect_reason": reconciliation.reason,
                    "effect_receipt_id": reconciliation.effect_receipt_id,
                    "service_instance_id": self.identity.service_instance_id,
                    "fencing_generation": self.fencing_generation,
                },
                now_us=now_us,
            )
            return run_status

    def _effect_step_id(self, run_id: str, workflow_step_id: str) -> str:
        """The runtime step one EFFECT-route workflow step of this run was opened as."""
        plan, expected = self._plan_and_expected(run_id)
        run_step_id = next(
            (
                candidate
                for step, (candidate, _, _) in zip(plan.steps, expected, strict=True)
                if step.step_id == workflow_step_id and step.route == ROUTE_EFFECT
            ),
            None,
        )
        if run_step_id is None:
            raise WorkflowSchedulingError(
                f"run {run_id!r} has no EFFECT-route workflow step "
                f"{workflow_step_id!r}, so nothing of it is owed an effect reconciliation"
            )
        return run_step_id

    def _require_effect_claim(self, claim: WorkflowStepClaim) -> None:
        """Refuse any claim that is not this scheduler's own EFFECT-route step."""
        self._require_scheduler_claim(claim)
        if self._claim_route(claim) != ROUTE_EFFECT:
            raise WorkflowSchedulingError(
                f"workflow step {claim.workflow_step_id!r} is not an EFFECT-route step "
                "of this run, so it declares and settles no effect"
            )

    def _claim_route(self, claim: WorkflowStepClaim) -> str | None:
        """Return the workflow route for the step this claim names, if it still matches."""
        plan, expected = self._plan_and_expected(claim.run_id)
        return next(
            (
                step.route
                for step, (candidate, _, _) in zip(plan.steps, expected, strict=True)
                if candidate == claim.run_step_id
                and step.step_id == claim.workflow_step_id
            ),
            None,
        )

    def _require_intent_of_claim(
        self, claim: WorkflowStepClaim, intent: EffectIntent
    ) -> None:
        if (
            intent.workspace_id,
            intent.run_id,
            intent.run_step_id,
            intent.attempt_id,
        ) != (
            self.workspace_id,
            claim.run_id,
            claim.run_step_id,
            claim.runtime_attempt_id,
        ):
            raise WorkflowSchedulingError(
                f"effect intent {intent.effect_intent_id!r} does not name the step and "
                f"attempt claim {claim.run_step_id!r} opened"
            )

    def _effect_details(
        self, claim: WorkflowStepClaim, **extra: object
    ) -> dict[str, object]:
        details: dict[str, object] = {
            "workspace_id": self.workspace_id,
            "run_id": claim.run_id,
            "run_step_id": claim.run_step_id,
            "workflow_step_id": claim.workflow_step_id,
            "runtime_attempt_id": claim.runtime_attempt_id,
            "runtime_attempt_number": claim.runtime_attempt_number,
            "service_instance_id": self.identity.service_instance_id,
            "fencing_generation": self.fencing_generation,
        }
        details.update(extra)
        return details

    def complete_loop_iteration(
        self,
        claim: WorkflowLoopIterationClaim,
        *,
        cost: int,
        continue_requested: bool,
        status: str = LOOP_ITERATION_SUCCEEDED,
        failure: ApiError | None = None,
    ) -> str:
        """Close one loop iteration and maybe the loop's single runtime attempt.

        A continuing iteration writes only the loop outcome and a run event; the runtime
        attempt remains open for the next iteration. An exit writes the iteration
        outcome, finishes the attempt exactly once, terminalizes the step and appends the
        aggregate event implied by the durable step states.
        """
        self._require_scheduler_claim(claim)
        with fenced_transaction(
            self.connection,
            self.identity,
            workspace_id=self.workspace_id,
            fencing_generation=self.fencing_generation,
        ):
            if self._current_run_status(claim.run_id) in RUN_TERMINAL_STATUSES:
                raise WorkflowSchedulingError(
                    f"run {claim.run_id!r} is terminal and cannot accept workflow "
                    "loop completion"
                )
            self._require_open_attempt(claim)
            plan, _ = self._plan_and_expected(claim.run_id)
            source_step = next(
                (
                    step
                    for step in plan.steps
                    if step.step_id == claim.workflow_step_id
                    and step.loop is not None
                ),
                None,
            )
            if source_step is None:
                raise WorkflowSchedulingError(
                    f"workflow step {claim.workflow_step_id!r} is not a loop"
                )
            iterations = read_workflow_loop_iterations(
                self.connection,
                workspace_id=self.workspace_id,
                run_id=claim.run_id,
                step_id=claim.workflow_step_id,
            )
            open_iteration = next(
                (
                    iteration
                    for iteration in iterations
                    if iteration.loop_iteration_id == claim.loop_iteration_id
                    and iteration.is_open
                ),
                None,
            )
            if open_iteration is None:
                raise WorkflowSchedulingError(
                    f"loop iteration {claim.loop_iteration_id!r} is not open"
                )
            if open_iteration.runtime_attempt_id != claim.runtime_attempt_id:
                raise WorkflowSchedulingError(
                    f"loop iteration {claim.loop_iteration_id!r} is not owned by "
                    f"attempt {claim.runtime_attempt_id!r}"
                )
            if status not in (LOOP_ITERATION_SUCCEEDED, LOOP_ITERATION_FAILED):
                raise WorkflowSchedulingError(
                    f"loop iteration status {status!r} is not supported"
                )
            total_after_completion = cost + sum(
                iteration.cost or 0 for iteration in iterations if not iteration.is_open
            )
            exit_reason: str | None
            if status == LOOP_ITERATION_FAILED:
                exit_reason = "failed"
                if failure is None:
                    failure = ApiError(
                        code="internal",
                        message="workflow loop iteration failed",
                        retry_class="non_retryable",
                    )
            elif not continue_requested:
                exit_reason = LOOP_EXIT_REQUESTED
            elif claim.loop_iteration_number >= _loop_bound(
                source_step, "max_iterations"
            ):
                exit_reason = LOOP_EXIT_MAX_ITERATIONS
            elif total_after_completion >= _loop_bound(source_step, "total_budget"):
                exit_reason = LOOP_EXIT_TOTAL_BUDGET
            else:
                exit_reason = None
            now_us = _now_us(self.clock)
            workflow = transaction_local_workflow_writer(
                self.connection, workspace_id=self.workspace_id
            )
            try:
                workflow.complete_loop_iteration(
                    loop_iteration_id=claim.loop_iteration_id,
                    cost=cost,
                    continue_requested=continue_requested,
                    exit_reason=exit_reason,
                    completed_at_us=now_us,
                    status=status,
                )
            except sqlite3.IntegrityError as error:
                raise WorkflowSchedulingError(
                    f"loop iteration {claim.loop_iteration_id!r} could not be "
                    "completed"
                ) from error
            details: dict[str, object] = {
                "workspace_id": self.workspace_id,
                "run_id": claim.run_id,
                "run_step_id": claim.run_step_id,
                "workflow_step_id": claim.workflow_step_id,
                "runtime_attempt_id": claim.runtime_attempt_id,
                "runtime_attempt_number": claim.runtime_attempt_number,
                "loop_iteration_id": claim.loop_iteration_id,
                "loop_iteration_number": claim.loop_iteration_number,
                "loop_iteration_status": status,
                "loop_iteration_cost": cost,
                "continue_requested": continue_requested,
            }
            if exit_reason is None:
                self._append_event(
                    run_id=claim.run_id,
                    run_step_id=claim.run_step_id,
                    event_kind="workflow_loop_iteration_succeeded",
                    run_status=RUN_STATUS_RUNNING,
                    message="workflow scheduler completed a loop iteration",
                    details=details,
                    now_us=now_us,
                )
                return RUN_STATUS_RUNNING
            details["loop_exit_reason"] = exit_reason
            writer = transaction_local_writer(
                self.connection, workspace_id=self.workspace_id
            )
            attempt_status = (
                ATTEMPT_STATUS_FAILED
                if status == LOOP_ITERATION_FAILED
                else ATTEMPT_STATUS_SUCCEEDED
            )
            writer.finish_attempt(
                attempt_id=claim.runtime_attempt_id,
                status=attempt_status,
                finished_at_us=now_us,
                failure=failure,
            )
            writer.record_step_status(
                run_step_id=claim.run_step_id,
                status="failed" if status == LOOP_ITERATION_FAILED else "succeeded",
                observed_at_us=now_us,
            )
            steps = read_run_steps(
                self.connection, workspace_id=self.workspace_id, run_id=claim.run_id
            )
            run_status = _run_status_for_steps(tuple(step.status for step in steps))
            self._append_event(
                run_id=claim.run_id,
                run_step_id=claim.run_step_id,
                event_kind=(
                    "workflow_run_failed"
                    if run_status == RUN_STATUS_FAILED
                    else "workflow_run_succeeded"
                    if run_status == RUN_STATUS_SUCCEEDED
                    else "workflow_run_partially_completed"
                    if run_status == RUN_STATUS_PARTIALLY_COMPLETED
                    else "workflow_loop_exited"
                ),
                run_status=run_status,
                message=(
                    "workflow scheduler failed the run"
                    if run_status == RUN_STATUS_FAILED
                    else "workflow scheduler completed the run"
                    if run_status == RUN_STATUS_SUCCEEDED
                    else "workflow scheduler completed the run with skipped work"
                    if run_status == RUN_STATUS_PARTIALLY_COMPLETED
                    else "workflow scheduler exited a loop step"
                ),
                details=details,
                now_us=now_us,
            )
            return run_status

    def _current_run_status(self, run_id: str) -> str:
        row = self.connection.execute(
            "SELECT run_status FROM omnivia_runtime_events "
            "WHERE workspace_id = ? AND run_id = ? ORDER BY sequence DESC LIMIT 1",
            (self.workspace_id, run_id),
        ).fetchone()
        if row is None:  # pragma: no cover - workflow binding already proves it
            raise WorkflowSchedulingError(f"run {run_id!r} has no runtime record")
        return str(row[0])

    def _audit_ref(self, run_id: str) -> str:
        row = self.connection.execute(
            "SELECT audit_ref FROM omnivia_runtime_runs "
            "WHERE workspace_id = ? AND run_id = ?",
            (self.workspace_id, run_id),
        ).fetchone()
        if row is None:
            raise WorkflowSchedulingError(f"run {run_id!r} is not admitted")
        return str(row[0])

    def _stored_workflow_wait(self, command: ResolveWait) -> Wait:
        snapshot = read_run(
            self.connection, workspace_id=self.workspace_id, run_id=command.run_id
        )
        if snapshot is None:
            raise WaitResolutionConflict(
                f"workspace {self.workspace_id!r} holds no run {command.run_id!r}"
            )
        wait = next(
            (candidate for candidate in snapshot.waits if candidate.wait_id == command.wait_id),
            None,
        )
        if wait is None:
            raise WaitResolutionConflict(
                f"run {command.run_id!r} holds no wait {command.wait_id!r}"
            )
        self._workflow_wait_step_id(command.run_id, wait.run_step_id)
        return wait

    def _require_workflow_resolution_identity(
        self, command: ResolveWait, wait: Wait
    ) -> str:
        """Reject a stale or mismatched resolution before policy is consulted.

        This is `runtime_waits._require_resolution_identity` stated for a workflow route
        wait, which has no active attempt to identify the resumption by. What identifies
        it instead is the digest the wait published and the purpose it opened on, so
        both are checked here -- fail-closed, before the policy seam, so a command that
        cannot resolve this wait can never use policy as an identifier oracle. Returns
        the `WaitStatus` the command settles the wait in.

        The purpose check is a recomputation, not a lookup: the wait row has no purpose
        column, so the digest it published is re-derived from the step's currently
        declared purpose and checkpoint. A wait opened for a different purpose, at a
        different checkpoint, or on a kind that purpose does not map to, does not
        reproduce its own digest and is refused rather than resolved as if it were this
        suspension.
        """
        with _semantic_refusal():
            validate_resolve_wait_shape(command)
        if command.resume_digest != wait.resume_digest:
            raise WaitResolutionConflict(
                f"resume_digest does not match the digest wait {wait.wait_id!r} published"
            )
        purpose = self._wait_purpose(
            self._workflow_wait_step_id(command.run_id, wait.run_step_id)
        )
        expected_digest, _ = _wait_lineage(
            prefix="workflow_resume",
            workspace_id=self.workspace_id,
            run_id=command.run_id,
            run_step_id=wait.run_step_id,
            purpose=purpose,
        )
        if wait.resume_digest != expected_digest or wait.kind != purpose.wait_kind:
            raise WaitResolutionConflict(
                f"wait {wait.wait_id!r} was not opened for purpose {purpose.purpose!r} "
                f"at checkpoint {purpose.checkpoint!r}"
            )
        status = WAIT_STATUS_FOR_RESOLUTION.get(command.resolution)
        if status is None:
            raise WaitResolutionConflict(
                f"{command.resolution!r} is not a resolution this build can settle a wait in"
            )
        if command.resolution != purpose.resolution:
            raise WaitResolutionConflict(
                f"a {purpose.purpose!r} workflow wait resolves through "
                f"{purpose.resolution!r}; cancellation is settled by the stop ledger"
            )
        return status

    def _workflow_wait_step_id(self, run_id: str, run_step_id: str) -> str:
        plan, expected = self._plan_and_expected(run_id)
        workflow_step_id = next(
            (
                step.step_id
                for step, (candidate, _, _) in zip(plan.steps, expected, strict=True)
                if candidate == run_step_id and step.route == ROUTE_WAIT
            ),
            None,
        )
        if workflow_step_id is None:
            raise WaitResolutionConflict(
                f"wait step {run_step_id!r} is not a scheduler-owned workflow wait step"
            )
        return workflow_step_id

    def _replayed_workflow_wait_resolution(
        self,
        command: ResolveWait,
        wait: Wait,
        status: str,
        policy: WaitResolutionPolicy,
        context: AuthorizedApplicationContext,
    ) -> Mapping[str, object]:
        if (wait.status, wait.resolution_reason, wait.approval_id) != (
            status,
            command.reason,
            command.approval_id,
        ):
            raise WaitResolutionConflict(
                f"wait {wait.wait_id!r} is already {wait.status!r} for "
                f"{wait.resolution_reason!r}; a wait is resolved exactly once"
            )
        approval = self._policy_approval(policy, context, command, wait)
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
        return self._wait_resolution_result(
            wait,
            status=wait.status,
            reason=wait.resolution_reason,
            approval_id=wait.approval_id,
        )

    def _policy_approval(
        self,
        policy: WaitResolutionPolicy,
        context: AuthorizedApplicationContext,
        command: ResolveWait,
        wait: Wait,
    ) -> Approval | None:
        approval = policy(context, command, wait)
        if (approval is not None) != (command.resolution == "approval_decision"):
            raise WaitPolicyDenied(
                f"the resolution policy identified no recorded approval for a "
                f"{command.resolution!r} resolution of wait {command.wait_id!r}"
                if approval is None
                else f"the resolution policy supplied an approval for a "
                f"{command.resolution!r} resolution, which carries none"
            )
        return approval

    def _wait_resolution_result(
        self, wait: Wait, *, status: str, reason: str | None, approval_id: str | None
    ) -> Mapping[str, object]:
        result: dict[str, object] = {
            "wait_id": wait.wait_id,
            "run_id": wait.run_id,
            "run_step_id": wait.run_step_id,
            "status": status,
            "resolution_reason": reason,
        }
        if approval_id is not None:
            result["approval_id"] = approval_id
        return result

    def _plan_and_expected(
        self, run_id: str
    ) -> tuple[WorkflowPlan, tuple[tuple[str, int, str], ...]]:
        binding = read_workflow_run_binding(
            self.connection, workspace_id=self.workspace_id, run_id=run_id
        )
        if binding is None:
            raise WorkflowSchedulingError(
                f"run {run_id!r} is not admitted as a workflow run"
            )
        plan = read_workflow_plan(
            self.connection,
            workspace_id=self.workspace_id,
            workflow_id=binding.workflow_id,
            workflow_version=binding.workflow_version,
        )
        if plan is None:  # pragma: no cover - 0027's foreign key makes it exist
            raise WorkflowSchedulingError(
                f"run {run_id!r} is bound to a workflow plan this workspace lost"
            )
        expected = _expected_runtime_steps(
            workspace_id=self.workspace_id,
            run_id=run_id,
            plan_hash=binding.plan_hash,
            steps=plan.steps,
        )
        if _stored_runtime_steps(
            self.connection, workspace_id=self.workspace_id, run_id=run_id
        ) != expected:
            raise WorkflowSchedulingError(
                f"run {run_id!r} has not opened the workflow plan it is bound to"
            )
        return plan, expected

    def _require_scheduler_claim(
        self, claim: WorkflowStepClaim | WorkflowLoopIterationClaim
    ) -> None:
        if (
            claim.workspace_id != self.workspace_id
            or claim.service_instance_id != self.identity.service_instance_id
            or claim.fencing_generation != self.fencing_generation
        ):
            raise WorkflowSchedulingError(
                f"claim for workflow step {claim.run_step_id!r} does not match this "
                "scheduler"
            )

    def _require_result_for_claim(
        self,
        claim: WorkflowStepClaim,
        work_unit: WorkUnitEnvelope,
        result: WorkResultEnvelope,
    ) -> WorkResultEnvelope:
        self._require_scheduler_claim(claim)
        work_unit.verify_content_hash()
        if work_unit.run_id != claim.run_id:
            raise WorkflowSchedulingError(
                f"work unit names run {work_unit.run_id!r}, not claim run "
                f"{claim.run_id!r}"
            )
        if work_unit.step_id != claim.workflow_step_id:
            raise WorkflowSchedulingError(
                f"work unit names workflow step {work_unit.step_id!r}, not claim "
                f"step {claim.workflow_step_id!r}"
            )
        if work_unit.fence != claim.fencing_generation:
            raise WorkflowSchedulingError(
                f"work unit fence {work_unit.fence} does not match scheduler "
                f"claim fence {claim.fencing_generation}"
            )
        return WorkflowDispatchPlanner.validate_result(work_unit, result)

    def _require_open_attempt(
        self, claim: WorkflowStepClaim | WorkflowLoopIterationClaim
    ) -> None:
        steps = read_run_steps(
            self.connection, workspace_id=self.workspace_id, run_id=claim.run_id
        )
        step = next((item for item in steps if item.run_step_id == claim.run_step_id), None)
        if step is None or step.status != "running" or not step.attempts:
            raise WorkflowSchedulingError(
                f"workflow step {claim.run_step_id!r} is not running"
            )
        attempt = step.attempts[-1]
        if (
            attempt.attempt_id != claim.runtime_attempt_id
            or attempt.attempt_number != claim.runtime_attempt_number
            or attempt.status != ATTEMPT_STATUS_RUNNING
        ):
            raise WorkflowSchedulingError(
                f"attempt {claim.runtime_attempt_id!r} is not the open attempt of "
                f"{claim.run_step_id!r}"
            )


__all__ = [
    "CAPACITY_LANE_DEFAULT",
    "CAPACITY_LANE_RECOVERY",
    "CAPACITY_WAIT_KIND",
    "CAPACITY_WAIT_PURPOSE",
    "CAPACITY_WAIT_RESOLUTION_REASON",
    "COMPENSATION_OUTCOMES",
    "COMPENSATION_SETTLED_EVENT",
    "COMPENSATION_STARTED_EVENT",
    "GOVERNED_REPAIR_EVENT",
    "GOVERNED_REPAIR_EVIDENCE_KIND",
    "GOVERNED_REPAIR_EVIDENCE_SOURCE",
    "RECONCILIATION_WAIT_KIND",
    "RECONCILIATION_WAIT_PURPOSE",
    "RECONCILIATION_WAIT_RESOLUTION_REASON",
    "RETRY_WAIT_KIND",
    "RETRY_WAIT_PURPOSE",
    "RETRY_WAIT_RESOLUTION_REASON",
    "WORKFLOW_WAIT_KIND",
    "WORKFLOW_WAIT_PURPOSE_KINDS",
    "WORKFLOW_WAIT_RESOLUTION_REASON",
    "WorkflowCapacityPolicy",
    "WorkflowGovernanceDenied",
    "WorkflowGovernancePolicy",
    "WorkflowGovernedAuthority",
    "WorkflowLoopIterationClaim",
    "WorkflowRetryPolicy",
    "WorkflowRuntimeStepOpening",
    "WorkflowScheduler",
    "WorkflowSchedulingError",
    "WorkflowStepClaim",
    "WorkflowWaitPurpose",
    "open_workflow_runtime_steps",
]
