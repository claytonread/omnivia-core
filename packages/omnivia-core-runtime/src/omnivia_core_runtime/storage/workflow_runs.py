"""Durable Workflow Run persistence over migration 0027, and nothing above it.

Migration 0027 landed eight tables and twenty-four triggers and then no Python at
all: `storage/workflow_runtime_hardening.py` inserts `omnivia_workflow_runs` and
*reads* `omnivia_workflow_plans` in one join, but nothing in this repository has
ever sealed a plan, so T-0688's own `admit_bound_run` cannot succeed in a real
workspace. This module is the missing half. It seals a plan, binds a run to the
exact plan it names, records the replay-safe observations, fenced child
correlations and evidence a completion is gated on, and projects a run's state
from stored rows.

What it deliberately is not. It is persistence and the state projection over
persistence: no scheduler, no dispatcher, no executor, no transport, no policy.
It decides *which state the stored rows already state*, never which state a run
should move to next.

It composes with T-0688 rather than competing with it. Run admission delegates to
:func:`~omnivia_core_runtime.storage.workflow_runtime_hardening.transaction_local_binding_writer`
so a run row and its `RuntimeDefinitionBinding` still land in one statement pair
under 0035's triggers; this module adds the pin checks that must happen *before*
that write and owns none of the binding document's shape.

The run state vocabulary
------------------------

A Workflow Run has eight states -- `created`, `queued`, `running`, `waiting`,
`completed`, `failed`, `cancelled`, `indeterminate` -- and none of them is a new
durable column. They are a total function of two facts migration 0018 already
stores: the `run_status` of the run's highest-sequence `omnivia_runtime_events`
row, and whether any `omnivia_runtime_run_steps` row has been opened for it.

That is deliberate. 0018's `omnivia_guard_runtime_events_insert` trigger already
refuses an illegal transition and already makes a terminal event final, and
`semantics_runtime.RUN_STATUS_TRANSITIONS` already states the same table in
Python. A ninth state column would be a third copy of a rule enforced twice, and
the copy that disagreed would be this one. So `admitted` carries both pre-execution
states and the opened-steps fact separates them: a bound run with no steps is
`created`, and one whose steps are open is `queued`.

`partially_completed` maps to `failed`, not to `completed`. A run that reached the
end without succeeding is not a success, and reporting it as one is the single
worst answer this projection could give.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from omnivia_core.contracts.v1.canonical_json import canonicalize
from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.semantics_workflow import (
    validate_runtime_definition_binding,
)
from omnivia_core_runtime.execution.workflow import (
    BRANCH_OUTCOMES,
    MaterialisedWorkflow,
    StepRouter,
)
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.workflow_runtime_hardening import (
    BoundRunAdmission,
    StoredRuntimeDefinitionBinding,
    bound_material,
    read_runtime_definition_binding,
    read_runtime_definition_binding_projection,
    transaction_local_binding_writer,
)

__all__ = [
    "WORKFLOW_RUN_STATES",
    "WORKFLOW_RUN_STATE_CANCELLED",
    "WORKFLOW_RUN_STATE_COMPLETED",
    "WORKFLOW_RUN_STATE_CREATED",
    "WORKFLOW_RUN_STATE_FAILED",
    "WORKFLOW_RUN_STATE_INDETERMINATE",
    "WORKFLOW_RUN_STATE_QUEUED",
    "WORKFLOW_RUN_STATE_RUNNING",
    "WORKFLOW_RUN_STATE_TRANSITIONS",
    "WORKFLOW_RUN_STATE_WAITING",
    "WORKFLOW_RUN_TERMINAL_STATES",
    "BranchObservationRecord",
    "ChildCorrelationRecord",
    "ChildResultRecord",
    "CompletionEvidenceRecord",
    "PlanObservationRecord",
    "RunCompletionRecord",
    "SealedWorkflowPlan",
    "StoredPlanStep",
    "WorkflowRunBinding",
    "WorkflowRunStateRefused",
    "WorkflowRunView",
    "WorkflowWriter",
    "admit_workflow_run",
    "read_workflow_plan",
    "read_workflow_run",
    "read_workflow_run_binding",
    "read_workspace_workflow_run_ids",
    "seal_workflow_plan",
    "transaction_local_workflow_writer",
    "validate_workflow_run_state_transition",
    "workflow_run_state",
    "workflow_writer",
]

_PLANS: Final = "omnivia_workflow_plans"
_PLAN_STEPS: Final = "omnivia_workflow_plan_steps"
_RUNS: Final = "omnivia_workflow_runs"
_OBSERVATIONS: Final = "omnivia_workflow_run_step_observations"
_CORRELATIONS: Final = "omnivia_workflow_child_correlations"
_CORRELATION_RESULTS: Final = "omnivia_workflow_child_correlation_results"
_COMPLETION_EVIDENCE: Final = "omnivia_workflow_run_completion_evidence"
_COMPLETIONS: Final = "omnivia_workflow_run_completions"

_RUNTIME_EVENTS: Final = "omnivia_runtime_events"
_RUNTIME_RUN_STEPS: Final = "omnivia_runtime_run_steps"

OBSERVATION_KIND_PLAN: Final = "plan"
OBSERVATION_KIND_BRANCH: Final = "branch"

CHILD_RESULT_ACCEPTED: Final = "accepted"
CHILD_RESULT_CLOSED: Final = "closed"

COMPLETION_OUTCOME_SUCCEEDED: Final = "SUCCEEDED"
COMPLETION_OUTCOME_FAILED: Final = "FAILED"


# --- run states ---------------------------------------------------------------------

WORKFLOW_RUN_STATE_CREATED: Final = "created"
WORKFLOW_RUN_STATE_QUEUED: Final = "queued"
WORKFLOW_RUN_STATE_RUNNING: Final = "running"
WORKFLOW_RUN_STATE_WAITING: Final = "waiting"
WORKFLOW_RUN_STATE_COMPLETED: Final = "completed"
WORKFLOW_RUN_STATE_FAILED: Final = "failed"
WORKFLOW_RUN_STATE_CANCELLED: Final = "cancelled"
WORKFLOW_RUN_STATE_INDETERMINATE: Final = "indeterminate"

WORKFLOW_RUN_STATES: Final[tuple[str, ...]] = (
    WORKFLOW_RUN_STATE_CREATED,
    WORKFLOW_RUN_STATE_QUEUED,
    WORKFLOW_RUN_STATE_RUNNING,
    WORKFLOW_RUN_STATE_WAITING,
    WORKFLOW_RUN_STATE_COMPLETED,
    WORKFLOW_RUN_STATE_FAILED,
    WORKFLOW_RUN_STATE_CANCELLED,
    WORKFLOW_RUN_STATE_INDETERMINATE,
)

WORKFLOW_RUN_TERMINAL_STATES: Final[frozenset[str]] = frozenset(
    {
        WORKFLOW_RUN_STATE_COMPLETED,
        WORKFLOW_RUN_STATE_FAILED,
        WORKFLOW_RUN_STATE_CANCELLED,
    }
)
"""The states a Workflow Run never leaves.

`indeterminate` is deliberately absent, exactly as `uncertain` is absent from
`semantics_runtime.RUN_TERMINAL_STATUSES`: an unreconciled run is an open
question, and calling it finished would licence the blind retry the uncertainty
exists to forbid.
"""

WORKFLOW_RUN_STATE_TRANSITIONS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        WORKFLOW_RUN_STATE_CREATED: frozenset(
            {
                WORKFLOW_RUN_STATE_CREATED,
                WORKFLOW_RUN_STATE_QUEUED,
                WORKFLOW_RUN_STATE_CANCELLED,
            }
        ),
        WORKFLOW_RUN_STATE_QUEUED: frozenset(
            {
                WORKFLOW_RUN_STATE_QUEUED,
                WORKFLOW_RUN_STATE_RUNNING,
                WORKFLOW_RUN_STATE_CANCELLED,
            }
        ),
        WORKFLOW_RUN_STATE_RUNNING: frozenset(
            {
                WORKFLOW_RUN_STATE_RUNNING,
                WORKFLOW_RUN_STATE_WAITING,
                WORKFLOW_RUN_STATE_COMPLETED,
                WORKFLOW_RUN_STATE_FAILED,
                WORKFLOW_RUN_STATE_CANCELLED,
                WORKFLOW_RUN_STATE_INDETERMINATE,
            }
        ),
        WORKFLOW_RUN_STATE_WAITING: frozenset(
            {
                WORKFLOW_RUN_STATE_WAITING,
                WORKFLOW_RUN_STATE_RUNNING,
                WORKFLOW_RUN_STATE_FAILED,
                WORKFLOW_RUN_STATE_CANCELLED,
            }
        ),
        WORKFLOW_RUN_STATE_INDETERMINATE: frozenset(
            {
                WORKFLOW_RUN_STATE_INDETERMINATE,
                WORKFLOW_RUN_STATE_RUNNING,
                WORKFLOW_RUN_STATE_COMPLETED,
                WORKFLOW_RUN_STATE_FAILED,
                WORKFLOW_RUN_STATE_CANCELLED,
            }
        ),
        WORKFLOW_RUN_STATE_COMPLETED: frozenset({WORKFLOW_RUN_STATE_COMPLETED}),
        WORKFLOW_RUN_STATE_FAILED: frozenset({WORKFLOW_RUN_STATE_FAILED}),
        WORKFLOW_RUN_STATE_CANCELLED: frozenset({WORKFLOW_RUN_STATE_CANCELLED}),
    }
)
"""Which Workflow Run state may follow which.

Every state reaches itself, because re-observing a run that has not moved is not a
transition. Every terminal state reaches only itself, so a downgrade out of one is
refused rather than silently applied.

This table is not independent authority. It is the image, under
:data:`_STATE_FOR_RUN_STATUS`, of `semantics_runtime.RUN_STATUS_TRANSITIONS` --
which 0018's event-insert trigger enforces on the way to disk -- plus the one edge
that table cannot state because both of its endpoints are `admitted`:
`created -> queued`. A caller therefore cannot use this projection to reach a
state the durable stream would have refused.
"""

_STATE_FOR_RUN_STATUS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "admitted": WORKFLOW_RUN_STATE_CREATED,
        "running": WORKFLOW_RUN_STATE_RUNNING,
        "waiting": WORKFLOW_RUN_STATE_WAITING,
        "succeeded": WORKFLOW_RUN_STATE_COMPLETED,
        "partially_completed": WORKFLOW_RUN_STATE_FAILED,
        "failed": WORKFLOW_RUN_STATE_FAILED,
        "cancelled": WORKFLOW_RUN_STATE_CANCELLED,
        "uncertain": WORKFLOW_RUN_STATE_INDETERMINATE,
    }
)
"""Migration 0018's `run_status` vocabulary, read as a Workflow Run state.

`partially_completed` reads as `failed`: it is explicitly not a success, and this
projection refuses to report one. `admitted` reads as `created` here and is
promoted to `queued` by :func:`workflow_run_state` when the run's steps are open.
"""


class WorkflowRunStateRefused(StorageError):
    """A Workflow Run state was asked to move somewhere it may not go.

    Typed, because a caller has to tell an illegal transition from a terminal
    downgrade from an unknown state, and the incumbent runtime path cannot: it
    lets 0018's trigger abort surface as a bare `sqlite3.IntegrityError` whose
    only discriminator is its message text. `diagnostic` is a closed code, the
    same shape `workflow_runtime_hardening` already uses for its own refusals.
    """

    def __init__(self, diagnostic: str, message: str) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic


DIAGNOSTIC_UNKNOWN_STATE: Final = "unknown_state"
DIAGNOSTIC_ILLEGAL_TRANSITION: Final = "illegal_transition"
DIAGNOSTIC_TERMINAL_DOWNGRADE: Final = "terminal_downgrade"


def validate_workflow_run_state_transition(previous: str, current: str) -> None:
    """Refuse a Workflow Run state move that :data:`WORKFLOW_RUN_STATE_TRANSITIONS` forbids.

    Fails closed on an unrecognised state at either end. That is the opposite of
    `semantics_runtime.validate_run_status_transition`, which returns silently on
    an unknown status so an older build cannot reject a newer wire value it has
    simply never seen. Nothing here comes off a wire: both ends are derived from
    stored rows by :func:`workflow_run_state`, so an unknown state means this
    module disagrees with the database, and continuing would write under a state
    machine it cannot evaluate.
    """
    if previous not in WORKFLOW_RUN_STATE_TRANSITIONS:
        raise WorkflowRunStateRefused(
            DIAGNOSTIC_UNKNOWN_STATE,
            f"{previous!r} is not a Workflow Run state this build knows",
        )
    if current not in WORKFLOW_RUN_STATE_TRANSITIONS:
        raise WorkflowRunStateRefused(
            DIAGNOSTIC_UNKNOWN_STATE,
            f"{current!r} is not a Workflow Run state this build knows",
        )
    if current in WORKFLOW_RUN_STATE_TRANSITIONS[previous]:
        return
    if previous in WORKFLOW_RUN_TERMINAL_STATES:
        raise WorkflowRunStateRefused(
            DIAGNOSTIC_TERMINAL_DOWNGRADE,
            f"a {previous!r} Workflow Run is finished and does not become {current!r}",
        )
    raise WorkflowRunStateRefused(
        DIAGNOSTIC_ILLEGAL_TRANSITION,
        f"a Workflow Run may not move from {previous!r} to {current!r}",
    )


def workflow_run_state(*, run_status: str, steps_opened: bool) -> str:
    """The Workflow Run state stated by one `run_status` and the run's step ledger.

    `steps_opened` separates the two pre-execution states, and only those: it is
    consulted for `admitted` alone, because once a run is running, waiting or
    finished its own status is the whole answer and an opened-step count could
    only contradict it.
    """
    state = _STATE_FOR_RUN_STATUS.get(run_status)
    if state is None:
        raise WorkflowRunStateRefused(
            DIAGNOSTIC_UNKNOWN_STATE,
            f"{run_status!r} is not a run status this build projects",
        )
    if state == WORKFLOW_RUN_STATE_CREATED and steps_opened:
        return WORKFLOW_RUN_STATE_QUEUED
    return state


# --- stored records -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredPlanStep:
    """One materialised step exactly as migration 0027 holds it."""

    step_id: str
    component_id: str
    component_version: str
    execution_class: str
    route: str
    sequence_index: int
    depends_on: tuple[str, ...]
    branch: Mapping[str, object] | None
    loop: Mapping[str, object] | None
    child_workflow: Mapping[str, object] | None
    step_definition_hash: str
    materialised_step_hash: str


@dataclass(frozen=True, slots=True)
class SealedWorkflowPlan:
    """One sealed definition and the single plan it materialises to."""

    workspace_id: str
    workflow_id: str
    workflow_version: str
    definition_hash: str
    plan_hash: str
    sealed_at_us: int
    audit_ref: str
    steps: tuple[StoredPlanStep, ...]


@dataclass(frozen=True, slots=True)
class WorkflowRunBinding:
    """One runtime run bound to exactly one sealed plan."""

    workspace_id: str
    run_id: str
    workflow_id: str
    workflow_version: str
    plan_hash: str
    bound_at_us: int


@dataclass(frozen=True, slots=True)
class PlanObservationRecord:
    """One replay-safe observation that a plan step was reached."""

    step_id: str
    route: str
    sequence_index: int
    observed_at_us: int


@dataclass(frozen=True, slots=True)
class BranchObservationRecord:
    """One replay-safe observation of how a branch step evaluated."""

    step_id: str
    outcome: str
    reason: str
    observed_at_us: int


@dataclass(frozen=True, slots=True)
class ChildCorrelationRecord:
    """One fenced, budgeted parent/child binding."""

    correlation_id: str
    parent_run_id: str
    parent_step_id: str
    child_workflow_id: str
    child_version: str
    child_workflow_hash: str
    fence: int
    budget: int
    opened_at_us: int


@dataclass(frozen=True, slots=True)
class ChildResultRecord:
    """One boundary result a child correlation consumed or closed with."""

    correlation_id: str
    result_sequence: int
    outcome: str
    fence: int
    child_workflow_id: str
    child_version: str
    child_workflow_hash: str
    cost: int | None
    recorded_at_us: int


@dataclass(frozen=True, slots=True)
class CompletionEvidenceRecord:
    """One piece of evidence a completion decision may be gated on."""

    evidence_kind: str
    evidence_digest: str
    recorded_at_us: int


@dataclass(frozen=True, slots=True)
class RunCompletionRecord:
    """The single evidence-gated completion decision for one run."""

    outcome: str
    decided_at_us: int
    audit_ref: str


@dataclass(frozen=True, slots=True)
class WorkflowRunView:
    """Every durable Workflow fact about one run, and its projected state.

    There is no `attempts`, no `waits` and no `next_step` field. Those are the
    canonical Runtime records migration 0018 owns and `storage/agent_runtime.py`
    reads; restating them here would publish a second answer to a question that
    already has one.

    `binding` is the run row -- the workflow, version and plan digest 0027 indexes --
    and `binding_projection` is the public `RuntimeDefinitionBindingProjection` that
    T-0688's verifying reader produced for the same run. The document itself is
    deliberately not carried: a reader that wants the pinned material asks T-0688 for
    it and gets the recomputation with it, and a copy sitting in this dataclass would
    be a copy nobody re-verified.
    """

    binding: WorkflowRunBinding
    binding_projection: Mapping[str, object]
    plan: SealedWorkflowPlan
    state: str
    run_status: str
    plan_observations: tuple[PlanObservationRecord, ...]
    branch_observations: tuple[BranchObservationRecord, ...]
    correlations: tuple[ChildCorrelationRecord, ...]
    correlation_results: tuple[ChildResultRecord, ...]
    completion_evidence: tuple[CompletionEvidenceRecord, ...]
    completion: RunCompletionRecord | None


# --- helpers ------------------------------------------------------------------------


def _canonical_json(value: object) -> str:
    """RFC 8785 bytes, which 0027's `json(x) <> x` guard accepts as canonical.

    JCS sorts object keys and strips insignificant whitespace, so SQLite's own
    `json()` minifier is a fixed point on its output. `json.dumps` is not: its
    default separators leave the spaces that guard rejects.
    """
    return canonicalize(value)


def _optional_document(value: Mapping[str, object] | None) -> str | None:
    return None if value is None else _canonical_json(dict(value))


def _optional_mapping(document: object) -> Mapping[str, object] | None:
    if document is None:
        return None
    loaded = json.loads(str(document))
    if not isinstance(loaded, dict):  # pragma: no cover - 0027 rejects this shape
        raise StorageError("a stored workflow plan step document is not an object")
    return loaded


# --- writes -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkflowWriter:
    """The Workflow Run writes, issued into a transaction that is already open.

    Not usefully constructible on its own: :func:`workflow_writer` and
    :func:`transaction_local_workflow_writer` are what hand one out, exactly as
    `agent_runtime` and `workflow_runtime_hardening` do for their own records, and
    neither issues a statement outside a fenced transaction. The workspace is
    bound at construction because a plan, its runs and their observations are one
    workspace's work.
    """

    connection: sqlite3.Connection
    workspace_id: str

    # -- plan sealing --

    def seal_plan(
        self,
        plan: MaterialisedWorkflow,
        *,
        sealed_at_us: int,
        audit_ref: str,
    ) -> SealedWorkflowPlan:
        """Store one materialised plan, or return the identical one already stored.

        Every column is derived from ``plan`` through the same seam an in-memory
        observation routes with -- :class:`StepRouter` for the route,
        ``MaterialisedStep`` for the order and both hashes -- so a stored plan and
        an observation of it cannot disagree about what the plan says.

        Re-sealing identical content is a replay and writes nothing. Re-sealing
        *different* content under the same identity is a conflict, because 0027
        makes a plan that has admitted a run immutable and a plan nobody can
        re-address is not a plan two authorings may share.
        """
        plan.verify_content_hash()
        stored = _read_plan(
            self.connection, self.workspace_id, plan.workflow_id, plan.version
        )
        if stored is not None:
            if stored.plan_hash == plan.content_hash and (
                stored.definition_hash == plan.definition_hash
            ):
                return stored
            raise StorageError(
                f"workflow {plan.workflow_id!r} version {plan.version!r} is already "
                f"sealed as {stored.plan_hash!r}"
            )

        self.connection.execute(
            f"INSERT INTO {_PLANS} (workspace_id, workflow_id, workflow_version, "
            "definition_hash, plan_hash, sealed_at_us, audit_ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                plan.workflow_id,
                plan.version,
                plan.definition_hash,
                plan.content_hash,
                sealed_at_us,
                audit_ref,
            ),
        )
        router = StepRouter()
        for step in plan.steps:
            self.connection.execute(
                f"INSERT INTO {_PLAN_STEPS} (workspace_id, workflow_id, workflow_version, "
                "step_id, component_id, component_version, execution_class, route, "
                "sequence_index, depends_on_json, branch_json, loop_json, "
                "child_workflow_json, step_definition_hash, materialised_step_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.workspace_id,
                    plan.workflow_id,
                    plan.version,
                    step.step_id,
                    step.component_id,
                    step.component_version,
                    step.execution_class,
                    router.route(step).route,
                    step.sequence_index,
                    _canonical_json(list(step.depends_on)),
                    _optional_document(
                        None if step.branch is None else step.branch.preimage
                    ),
                    _optional_document(
                        None if step.loop is None else step.loop.preimage
                    ),
                    _optional_document(
                        None
                        if step.child_workflow is None
                        else step.child_workflow.preimage
                    ),
                    step.definition_hash,
                    step.content_hash,
                ),
            )
        sealed = _read_plan(
            self.connection, self.workspace_id, plan.workflow_id, plan.version
        )
        if sealed is None:  # pragma: no cover - the insert above just committed it
            raise StorageError("a sealed workflow plan did not read back")
        return sealed

    # -- run admission --

    def admit_run(self, admission: BoundRunAdmission) -> StoredRuntimeDefinitionBinding:
        """Bind one runtime run to the exact plan it pins, or replay that binding.

        The pin is checked here, before any statement is issued, because 0027's
        foreign key can only say *a* plan is missing -- it cannot distinguish a
        version this workspace never sealed from a plan hash that names different
        content under a version it did. Those are different operator mistakes and
        they get different messages.

        The write itself is T-0688's. :func:`transaction_local_binding_writer`
        lands the run row and its `RuntimeDefinitionBinding` together under 0035's
        triggers; re-implementing that insert here would give
        `omnivia_workflow_runs` a second writer that could disagree with the first
        about what a bound run is.

        A replay is decided on the whole binding, not on the three columns
        `omnivia_workflow_runs` happens to hold. Those columns are the workflow, its
        version and the plan digest; a binding also pins the release, the execution
        profile, the effective policy, every Component implementation digest, the
        resource snapshots and the model policy. Two admissions can agree on all three
        columns and name entirely different material, and returning the *stored*
        binding for the second one would report material that was never bound as the
        material this run executes against. So the stored document is read back through
        T-0688's verifying reader and compared by
        :func:`~omnivia_core_runtime.storage.workflow_runtime_hardening.bound_material`
        -- the same field list a resume drifts on, which excludes `bindingId`,
        `bindingSchemaVersion`, `boundAt` and `boundBy`. A crash-retry that reads its
        own clock and mints a new binding identifier for the same material is still the
        replay it was; anything else is a conflict and raises before a statement.
        """
        plan = _read_plan(
            self.connection,
            self.workspace_id,
            admission.workflow_id,
            admission.workflow_version,
        )
        if plan is None:
            raise StorageError(
                f"this workspace has no sealed plan for workflow "
                f"{admission.workflow_id!r} version {admission.workflow_version!r}"
            )
        if plan.plan_hash != admission.plan_hash:
            raise StorageError(
                f"workflow {admission.workflow_id!r} version "
                f"{admission.workflow_version!r} is sealed as {plan.plan_hash!r}, "
                f"not {admission.plan_hash!r}"
            )
        bound = _read_binding(self.connection, self.workspace_id, admission.run_id)
        if bound is not None:
            if (
                bound.workflow_id != admission.workflow_id
                or bound.workflow_version != admission.workflow_version
                or bound.plan_hash != admission.plan_hash
            ):
                raise StorageError(
                    f"run {admission.run_id!r} is already bound to workflow "
                    f"{bound.workflow_id!r} version {bound.workflow_version!r}"
                )
            return _replayed_binding(
                self.connection, self.workspace_id, admission
            )
        writer = transaction_local_binding_writer(
            self.connection, workspace_id=self.workspace_id
        )
        return writer.admit_bound_run(admission)

    # -- observations --

    def observe_plan_step(
        self,
        *,
        run_id: str,
        step_id: str,
        route: str,
        sequence_index: int,
        observed_at_us: int,
    ) -> None:
        """Record that a plan step was reached, once.

        `INSERT OR IGNORE` makes a replay silent; 0027's own trigger makes a
        *contradiction* loud, refusing a second observation of the same step that
        states a different route or position. Idempotence and honesty are not the
        same property and this pair keeps both.
        """
        self.connection.execute(
            f"INSERT OR IGNORE INTO {_OBSERVATIONS} (workspace_id, run_id, step_id, "
            "observation_kind, route, sequence_index, branch_outcome, branch_reason, "
            "observed_at_us) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
            (
                self.workspace_id,
                run_id,
                step_id,
                OBSERVATION_KIND_PLAN,
                route,
                sequence_index,
                observed_at_us,
            ),
        )

    def observe_branch(
        self,
        *,
        run_id: str,
        step_id: str,
        outcome: str,
        reason: str,
        observed_at_us: int,
    ) -> None:
        """Record how a branch step evaluated, once."""
        if outcome not in BRANCH_OUTCOMES:
            raise StorageError(f"{outcome!r} is not a branch outcome")
        self.connection.execute(
            f"INSERT OR IGNORE INTO {_OBSERVATIONS} (workspace_id, run_id, step_id, "
            "observation_kind, route, sequence_index, branch_outcome, branch_reason, "
            "observed_at_us) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?)",
            (
                self.workspace_id,
                run_id,
                step_id,
                OBSERVATION_KIND_BRANCH,
                outcome,
                reason,
                observed_at_us,
            ),
        )

    # -- child correlations, the boundary a result crosses --

    def open_child_correlation(self, correlation: ChildCorrelationRecord) -> None:
        """Open one fenced, budgeted parent/child binding."""
        self.connection.execute(
            f"INSERT INTO {_CORRELATIONS} (workspace_id, correlation_id, parent_run_id, "
            "parent_step_id, child_workflow_id, child_version, child_workflow_hash, "
            "fence, budget, opened_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                correlation.correlation_id,
                correlation.parent_run_id,
                correlation.parent_step_id,
                correlation.child_workflow_id,
                correlation.child_version,
                correlation.child_workflow_hash,
                correlation.fence,
                correlation.budget,
                correlation.opened_at_us,
            ),
        )

    def record_child_result(self, result: ChildResultRecord) -> None:
        """Record one boundary result against the fence it was opened with.

        The fence and child identity are carried on the result rather than looked
        up, so 0027's trigger can refuse a result minted under a superseded fence
        instead of quietly attributing it to the current one.
        """
        self.connection.execute(
            f"INSERT INTO {_CORRELATION_RESULTS} (workspace_id, correlation_id, "
            "result_sequence, outcome, fence, child_workflow_id, child_version, "
            "child_workflow_hash, cost, recorded_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                result.correlation_id,
                result.result_sequence,
                result.outcome,
                result.fence,
                result.child_workflow_id,
                result.child_version,
                result.child_workflow_hash,
                result.cost,
                result.recorded_at_us,
            ),
        )

    # -- completion --

    def record_completion_evidence(
        self,
        *,
        run_id: str,
        evidence_kind: str,
        evidence_digest: str,
        recorded_at_us: int,
    ) -> None:
        """Record one piece of evidence, before any completion decision exists."""
        self.connection.execute(
            f"INSERT INTO {_COMPLETION_EVIDENCE} (workspace_id, run_id, evidence_kind, "
            "evidence_digest, recorded_at_us) VALUES (?, ?, ?, ?, ?)",
            (self.workspace_id, run_id, evidence_kind, evidence_digest, recorded_at_us),
        )

    def complete_run(
        self, *, run_id: str, outcome: str, decided_at_us: int, audit_ref: str
    ) -> None:
        """Decide one run's completion, gated on the evidence already recorded.

        The gate is 0027's, not this method's: a completion with no evidence row
        is refused by the trigger, so a caller cannot reach a decision by calling
        this before it recorded what the decision rests on.
        """
        if outcome not in {COMPLETION_OUTCOME_SUCCEEDED, COMPLETION_OUTCOME_FAILED}:
            raise StorageError(f"{outcome!r} is not a workflow completion outcome")
        self.connection.execute(
            f"INSERT INTO {_COMPLETIONS} (workspace_id, run_id, outcome, decided_at_us, "
            "audit_ref) VALUES (?, ?, ?, ?, ?)",
            (self.workspace_id, run_id, outcome, decided_at_us, audit_ref),
        )


def transaction_local_workflow_writer(
    connection: sqlite3.Connection, *, workspace_id: str
) -> WorkflowWriter:
    """The Workflow writes, for a caller that already holds a fenced transaction.

    The narrow companion to :func:`workflow_writer`, for the seam that has to seal
    a plan and admit a run alongside its own statements -- a mutation's audit,
    claim and outcome -- in one transaction, and cannot call the standalone
    wrapper because `BEGIN IMMEDIATE` does not nest.

    This weakens no fencing. It opens no transaction and validates no authority,
    so everything it issues is covered by the entry and pre-commit validation of
    the transaction the caller opened; 0027's triggers refuse an unguarded insert
    regardless of which Python object issued it.
    """
    return WorkflowWriter(connection, workspace_id)


@contextmanager
def workflow_writer(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
) -> Iterator[WorkflowWriter]:
    """One fenced transaction, and the Workflow writes that may be issued into it."""
    with fenced_transaction(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ):
        yield transaction_local_workflow_writer(connection, workspace_id=workspace_id)


# --- reads --------------------------------------------------------------------------


def _read_plan(
    connection: sqlite3.Connection,
    workspace_id: str,
    workflow_id: str,
    workflow_version: str,
) -> SealedWorkflowPlan | None:
    row = connection.execute(
        f"SELECT definition_hash, plan_hash, sealed_at_us, audit_ref FROM {_PLANS} "
        "WHERE workspace_id = ? AND workflow_id = ? AND workflow_version = ?",
        (workspace_id, workflow_id, workflow_version),
    ).fetchone()
    if row is None:
        return None
    steps = tuple(
        StoredPlanStep(
            step_id=str(step[0]),
            component_id=str(step[1]),
            component_version=str(step[2]),
            execution_class=str(step[3]),
            route=str(step[4]),
            sequence_index=int(step[5]),
            depends_on=tuple(str(item) for item in json.loads(str(step[6]))),
            branch=_optional_mapping(step[7]),
            loop=_optional_mapping(step[8]),
            child_workflow=_optional_mapping(step[9]),
            step_definition_hash=str(step[10]),
            materialised_step_hash=str(step[11]),
        )
        for step in connection.execute(
            "SELECT step_id, component_id, component_version, execution_class, route, "
            "sequence_index, depends_on_json, branch_json, loop_json, "
            f"child_workflow_json, step_definition_hash, materialised_step_hash "
            f"FROM {_PLAN_STEPS} WHERE workspace_id = ? AND workflow_id = ? "
            "AND workflow_version = ? ORDER BY sequence_index",
            (workspace_id, workflow_id, workflow_version),
        ).fetchall()
    )
    return SealedWorkflowPlan(
        workspace_id=workspace_id,
        workflow_id=workflow_id,
        workflow_version=workflow_version,
        definition_hash=str(row[0]),
        plan_hash=str(row[1]),
        sealed_at_us=int(row[2]),
        audit_ref=str(row[3]),
        steps=steps,
    )


def _read_binding(
    connection: sqlite3.Connection, workspace_id: str, run_id: str
) -> WorkflowRunBinding | None:
    row = connection.execute(
        f"SELECT workflow_id, workflow_version, plan_hash, bound_at_us FROM {_RUNS} "
        "WHERE workspace_id = ? AND run_id = ?",
        (workspace_id, run_id),
    ).fetchone()
    if row is None:
        return None
    return WorkflowRunBinding(
        workspace_id=workspace_id,
        run_id=run_id,
        workflow_id=str(row[0]),
        workflow_version=str(row[1]),
        plan_hash=str(row[2]),
        bound_at_us=int(row[3]),
    )


def _verified_binding(
    connection: sqlite3.Connection, workspace_id: str, run_id: str
) -> StoredRuntimeDefinitionBinding:
    """The binding this run was admitted with, believed only after recomputation.

    Read through T-0688's :func:`read_runtime_definition_binding` rather than off the
    row, because that reader is what re-derives the digest, the byte length and the
    canonical spelling, re-validates through the public contract and re-runs 0035's
    own admission join. A `SELECT binding_json` here would hand back whatever bytes
    the file holds -- including bytes edited outside this database's guards -- as the
    binding this run executes against.

    `None` from that reader means a run row with no binding document at all. For a run
    this module admitted that is impossible by construction, since T-0688 writes both
    rows or neither; reaching it means the binding was removed underneath us, and a
    run whose binding cannot be produced is not a run whose binding can be confirmed.
    """
    stored = read_runtime_definition_binding(
        connection, workspace_id=workspace_id, run_id=run_id
    )
    if stored is None:
        raise StorageError(f"run {run_id!r} is bound but carries no binding document")
    return stored


def _replayed_binding(
    connection: sqlite3.Connection,
    workspace_id: str,
    admission: BoundRunAdmission,
) -> StoredRuntimeDefinitionBinding:
    """The stored binding, once the incoming one is proven to be the same binding.

    The incoming document is validated first, so a malformed binding refuses as a
    malformed binding rather than as a mismatch: `bound_material` reads members off
    whatever mapping it is given, and comparing an unvalidated one would let a
    binding missing every pinned field decide its own comparison.
    """
    try:
        validate_runtime_definition_binding(admission.binding)
    except ContractSemanticError as error:
        raise StorageError(
            f"run {admission.run_id!r} was re-admitted with a binding that is not a "
            "valid RuntimeDefinitionBinding"
        ) from error
    stored = _verified_binding(connection, workspace_id, admission.run_id)
    if bound_material(stored.binding) != bound_material(admission.binding):
        raise StorageError(
            f"run {admission.run_id!r} is already bound to different material than "
            "this admission names"
        )
    return stored


def _latest_run_status(
    connection: sqlite3.Connection, workspace_id: str, run_id: str
) -> str | None:
    row = connection.execute(
        f"SELECT run_status FROM {_RUNTIME_EVENTS} WHERE workspace_id = ? AND run_id = ? "
        "ORDER BY sequence DESC LIMIT 1",
        (workspace_id, run_id),
    ).fetchone()
    return None if row is None else str(row[0])


def _steps_opened(
    connection: sqlite3.Connection, workspace_id: str, run_id: str
) -> bool:
    row = connection.execute(
        f"SELECT 1 FROM {_RUNTIME_RUN_STEPS} WHERE workspace_id = ? AND run_id = ? LIMIT 1",
        (workspace_id, run_id),
    ).fetchone()
    return row is not None


def seal_workflow_plan(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    plan: MaterialisedWorkflow,
    sealed_at_us: int,
    audit_ref: str,
) -> SealedWorkflowPlan:
    """Seal one plan in its own fenced transaction."""
    with workflow_writer(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as writer:
        return writer.seal_plan(plan, sealed_at_us=sealed_at_us, audit_ref=audit_ref)


def admit_workflow_run(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    admission: BoundRunAdmission,
) -> StoredRuntimeDefinitionBinding:
    """Admit one Workflow Run in its own fenced transaction."""
    with workflow_writer(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as writer:
        return writer.admit_run(admission)


def read_workflow_plan(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    workflow_id: str,
    workflow_version: str,
) -> SealedWorkflowPlan | None:
    """The sealed plan for one workflow version in this workspace, or `None`."""
    return _read_plan(connection, workspace_id, workflow_id, workflow_version)


def read_workflow_run_binding(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> WorkflowRunBinding | None:
    """Which sealed plan one run is bound to, or `None` for a run that is not a Workflow.

    The three columns `omnivia_workflow_runs` holds, and deliberately not the binding
    document. This answers one question only -- *which sealed plan is this run bound to*
    -- and `None` is its answer both for an `agent_component` run and for a Workflow run
    whose row is gone, which it cannot tell apart. Deciding *that* is 0018's
    `definition_kind`, and a caller for which the difference matters reads it first:
    :class:`~omnivia_core_runtime.service.workflow_runtime.WorkflowStepPlan` does, so a
    `workflow` run reaching `None` here refuses rather than executing unplanned.

    It is not evidence a run may be executed against, and nothing may treat it as such:
    `WorkflowStepPlan` pairs it with T-0688's
    :func:`read_runtime_definition_binding` before any step is claimed or observed, and
    :func:`read_workflow_run` reports a run through the same verifying reader's
    projection.
    """
    return _read_binding(connection, workspace_id, run_id)


def read_workflow_run(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> WorkflowRunView | None:
    """Every durable Workflow fact about one run, and the state they state.

    Reads a workspace, never a record's claim about one: a run of another
    workspace is invisible rather than merely unlikely to be asked for.
    """
    binding = _read_binding(connection, workspace_id, run_id)
    if binding is None:
        return None
    # T-0688's own projection, not a second read of the same bytes: it recomputes the
    # digest, the length and the canonical spelling, re-validates through the public
    # contract and re-runs 0035's admission join before it will state anything, so a
    # binding edited outside this database fails this read closed instead of being
    # reported as the material the run is executing against.
    projection = read_runtime_definition_binding_projection(
        connection, workspace_id=workspace_id, run_id=run_id
    )
    if projection is None:  # pragma: no cover - the run row was just read above
        raise StorageError(f"run {run_id!r} vanished between two reads")
    plan = _read_plan(
        connection, workspace_id, binding.workflow_id, binding.workflow_version
    )
    if plan is None:  # pragma: no cover - 0027's foreign key forbids this
        raise StorageError(f"run {run_id!r} binds a plan this workspace has not sealed")
    run_status = _latest_run_status(connection, workspace_id, run_id)
    if run_status is None:
        raise StorageError(f"run {run_id!r} has no runtime event stream")

    observations = connection.execute(
        "SELECT step_id, observation_kind, route, sequence_index, branch_outcome, "
        f"branch_reason, observed_at_us FROM {_OBSERVATIONS} "
        "WHERE workspace_id = ? AND run_id = ? ORDER BY observed_at_us, step_id, "
        "observation_kind",
        (workspace_id, run_id),
    ).fetchall()
    plan_observations = tuple(
        PlanObservationRecord(
            step_id=str(row[0]),
            route=str(row[2]),
            sequence_index=int(row[3]),
            observed_at_us=int(row[6]),
        )
        for row in observations
        if str(row[1]) == OBSERVATION_KIND_PLAN
    )
    branch_observations = tuple(
        BranchObservationRecord(
            step_id=str(row[0]),
            outcome=str(row[4]),
            reason=str(row[5]),
            observed_at_us=int(row[6]),
        )
        for row in observations
        if str(row[1]) == OBSERVATION_KIND_BRANCH
    )

    correlations = tuple(
        ChildCorrelationRecord(
            correlation_id=str(row[0]),
            parent_run_id=str(row[1]),
            parent_step_id=str(row[2]),
            child_workflow_id=str(row[3]),
            child_version=str(row[4]),
            child_workflow_hash=str(row[5]),
            fence=int(row[6]),
            budget=int(row[7]),
            opened_at_us=int(row[8]),
        )
        for row in connection.execute(
            "SELECT correlation_id, parent_run_id, parent_step_id, child_workflow_id, "
            f"child_version, child_workflow_hash, fence, budget, opened_at_us "
            f"FROM {_CORRELATIONS} WHERE workspace_id = ? AND parent_run_id = ? "
            "ORDER BY opened_at_us, correlation_id",
            (workspace_id, run_id),
        ).fetchall()
    )
    correlation_results = tuple(
        ChildResultRecord(
            correlation_id=str(row[0]),
            result_sequence=int(row[1]),
            outcome=str(row[2]),
            fence=int(row[3]),
            child_workflow_id=str(row[4]),
            child_version=str(row[5]),
            child_workflow_hash=str(row[6]),
            cost=None if row[7] is None else int(row[7]),
            recorded_at_us=int(row[8]),
        )
        for row in connection.execute(
            "SELECT r.correlation_id, r.result_sequence, r.outcome, r.fence, "
            "r.child_workflow_id, r.child_version, r.child_workflow_hash, r.cost, "
            f"r.recorded_at_us FROM {_CORRELATION_RESULTS} r "
            f"JOIN {_CORRELATIONS} c ON c.workspace_id = r.workspace_id "
            "AND c.correlation_id = r.correlation_id "
            "WHERE r.workspace_id = ? AND c.parent_run_id = ? "
            "ORDER BY r.correlation_id, r.result_sequence",
            (workspace_id, run_id),
        ).fetchall()
    )

    completion_evidence = tuple(
        CompletionEvidenceRecord(
            evidence_kind=str(row[0]),
            evidence_digest=str(row[1]),
            recorded_at_us=int(row[2]),
        )
        for row in connection.execute(
            "SELECT evidence_kind, evidence_digest, recorded_at_us FROM "
            f"{_COMPLETION_EVIDENCE} WHERE workspace_id = ? AND run_id = ? "
            "ORDER BY evidence_kind",
            (workspace_id, run_id),
        ).fetchall()
    )
    completion_row = connection.execute(
        f"SELECT outcome, decided_at_us, audit_ref FROM {_COMPLETIONS} "
        "WHERE workspace_id = ? AND run_id = ?",
        (workspace_id, run_id),
    ).fetchone()
    completion = (
        None
        if completion_row is None
        else RunCompletionRecord(
            outcome=str(completion_row[0]),
            decided_at_us=int(completion_row[1]),
            audit_ref=str(completion_row[2]),
        )
    )

    return WorkflowRunView(
        binding=binding,
        binding_projection=projection,
        plan=plan,
        state=workflow_run_state(
            run_status=run_status,
            steps_opened=_steps_opened(connection, workspace_id, run_id),
        ),
        run_status=run_status,
        plan_observations=plan_observations,
        branch_observations=branch_observations,
        correlations=correlations,
        correlation_results=correlation_results,
        completion_evidence=completion_evidence,
        completion=completion,
    )


def read_workspace_workflow_run_ids(
    connection: sqlite3.Connection, *, workspace_id: str
) -> tuple[str, ...]:
    """Every Workflow Run id bound in this workspace, in stable order."""
    return tuple(
        str(row[0])
        for row in connection.execute(
            f"SELECT run_id FROM {_RUNS} WHERE workspace_id = ? ORDER BY run_id",
            (workspace_id,),
        ).fetchall()
    )
