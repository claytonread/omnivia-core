"""Authoritative persistence for the durable Workflow Runtime records (migration 0027).

Storage primitives for the sealed plan and its steps, the runtime run bound to it, the
replay-safe plan and branch observations, the run's selected-connection facts (0030),
its durable loop iteration ledger (0031), mapped-input and capability readiness facts
(0032), the fenced child correlations and their
results, and the evidence-gated completion
decision. Nothing above them: there is no
scheduler readiness here, no dispatcher, no executor, no durable wait, no effect, no
compensation and no transport. Milestones after this one own those; this module owns
the rows they will read and write.

The shape follows :mod:`storage.agent_runtime` deliberately rather than inventing a
second one. :class:`WorkflowWriter` carries every statement and issues it into a
transaction somebody else opened; :func:`transaction_local_workflow_writer` hands one
to a caller already inside a fence, :func:`workflow_writer` opens the fence itself, and
the standalone functions are thin wrappers over the second. There is one copy of every
statement and one fence per composition either way, because `BEGIN IMMEDIATE` does not
nest.

**What is derived here and what is delegated.** A plan row is a re-addressing of a
sealed :class:`~execution.workflow.MaterialisedWorkflow`, so this module derives every
column from it -- including each step's `route`, through the same
:class:`~execution.workflow.StepRouter` the in-memory seam uses, so a stored plan and an
observation of it can never disagree about routing. Everything the migration already
enforces -- the contiguous step sequence, the sealed plan, the `definition_kind`
`workflow` runtime run, the fence line, the budget, the evidence gate -- is left to the
migration. Restating a trigger in Python would be a second copy that can disagree with
the first.

**What this module refuses in Python, and why those two.** A plan already sealed under a
different definition or plan hash, and a run already bound to a different workflow,
version or plan hash, are both refused here rather than in SQL: they are the idempotency
questions -- *is this the same admission again, or a different one wearing the same
identity?* -- and the answer has to distinguish a replay that returns the stored record
from a conflict that raises. A `CHECK` can only ever give the second answer.

**What is deliberately not stored.** No provider secret, no provider invocation detail,
no raw external log, no filesystem path, no URL, no renderer state and no Chat state.
The records are plan identity, run binding, observations, correlations, evidence and one
decision, and every value on them is an identifier, a vocabulary member, a content
digest, a bounded integer or a microsecond instant.

Every read takes the caller's workspace and filters on it in SQL, exactly as
:mod:`storage.agent_runtime` does: an identifier without the workspace it was issued in
cannot be resolved, and could be resolved against the wrong one.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final

from omnivia_core_runtime.execution.workflow import (
    BranchResult,
    ChildWorkflowDefinition,
    MaterialisedStep,
    MaterialisedWorkflow,
    StepRouter,
)
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.ownership.identity import ServiceInstanceIdentity
from omnivia_core_runtime.storage.agent_runtime import runtime_timestamp
from omnivia_core_runtime.storage.connection import StorageError

PLANS: Final = "omnivia_workflow_plans"
PLAN_STEPS: Final = "omnivia_workflow_plan_steps"
RUNS: Final = "omnivia_workflow_runs"
OBSERVATIONS: Final = "omnivia_workflow_run_step_observations"
CORRELATIONS: Final = "omnivia_workflow_child_correlations"
CORRELATION_RESULTS: Final = "omnivia_workflow_child_correlation_results"
COMPLETION_EVIDENCE: Final = "omnivia_workflow_run_completion_evidence"
COMPLETIONS: Final = "omnivia_workflow_run_completions"
STEP_CONNECTIONS: Final = "omnivia_workflow_run_step_connections"
LOOP_ITERATIONS: Final = "omnivia_workflow_loop_iterations"
LOOP_ITERATION_OUTCOMES: Final = "omnivia_workflow_loop_iteration_outcomes"
READINESS_FACTS: Final = "omnivia_workflow_run_step_readiness_facts"

OBSERVATION_PLAN: Final = "plan"
OBSERVATION_BRANCH: Final = "branch"

#: The two directions a step's connection fact may be recorded in, exactly as 0030
#: admits them. Named here so a caller spells them once; the vocabulary itself is the
#: migration's `CHECK`, not a second copy in Python.
CONNECTION_INCOMING: Final = "incoming"
CONNECTION_OUTGOING: Final = "outgoing"

#: What a connection fact states. `required` is what this run declares its step must
#: have selected before it is ready; `selected` is that the selection happened. Neither
#: is a definition-graph verdict: nothing here evaluates a connection's condition.
CONNECTION_REQUIRED: Final = "required"
CONNECTION_SELECTED: Final = "selected"

#: Generic scheduler-readiness fact kinds. These are not live policy decisions: a row
#: says the run recorded the fact, and the scheduler only compares required facts with
#: their matching ready/granted facts.
READINESS_MAPPED_INPUT_REQUIRED: Final = "mapped_input_required"
READINESS_MAPPED_INPUT_READY: Final = "mapped_input_ready"
READINESS_CAPABILITY_REQUIRED: Final = "capability_required"
READINESS_CAPABILITY_GRANTED: Final = "capability_granted"

CHILD_RESULT_ACCEPTED: Final = "accepted"
CHILD_RESULT_CLOSED: Final = "closed"

#: How one loop iteration ended, exactly as 0031 admits it.
LOOP_ITERATION_SUCCEEDED: Final = "succeeded"
LOOP_ITERATION_FAILED: Final = "failed"

#: Why a loop stopped after an iteration. `None` -- no reason -- is the loop continuing;
#: every other value is a stop, and the vocabulary is 0031's `CHECK`, not a second copy.
LOOP_EXIT_REQUESTED: Final = "requested"
LOOP_EXIT_MAX_ITERATIONS: Final = "max_iterations"
LOOP_EXIT_TOTAL_BUDGET: Final = "total_budget"
LOOP_EXIT_FAILED: Final = "failed"

#: Routing is derived once, here, from the same seam the in-memory oracle routes with.
_ROUTER: Final = StepRouter()

_PLAN_COLUMNS: Final = (
    "workflow_id, workflow_version, definition_hash, plan_hash, sealed_at_us, audit_ref"
)
_STEP_COLUMNS: Final = (
    "step_id, component_id, component_version, execution_class, route, "
    "sequence_index, depends_on_json, branch_json, loop_json, child_workflow_json, "
    "step_definition_hash, materialised_step_hash"
)
_RUN_COLUMNS: Final = "run_id, workflow_id, workflow_version, plan_hash, bound_at_us"
_OBSERVATION_COLUMNS: Final = (
    "step_id, observation_kind, route, sequence_index, branch_outcome, branch_reason, "
    "observed_at_us"
)
_CORRELATION_COLUMNS: Final = (
    "correlation_id, parent_step_id, child_workflow_id, child_version, "
    "child_workflow_hash, fence, budget, opened_at_us"
)
_RESULT_COLUMNS: Final = (
    "result_sequence, outcome, fence, child_workflow_id, child_version, "
    "child_workflow_hash, cost, recorded_at_us"
)


def _document(value: dict[str, object] | list[str] | None) -> str | None:
    """One plan-step document, in the exact minified form 0027 admits.

    The migration requires `json(x) = x`, which is SQLite's own minified spelling, so a
    stored document is byte-identical to what SQLite would produce from it. Separators
    rather than :func:`~contracts.v1.to_canonical_json` because canonicalisation also
    sorts keys, and a plan step's documents are hashed by the sealed
    :class:`~execution.workflow.MaterialisedStep` rather than by these bytes -- what has
    to be stable here is only the form the trigger checks.
    """
    return None if value is None else json.dumps(value, separators=(",", ":"))


def _decoded(text: object) -> dict[str, Any] | None:
    if text is None:
        return None
    decoded = json.loads(str(text))
    if not isinstance(decoded, dict):
        raise StorageError("a stored workflow plan step document is not a JSON object")
    return decoded


def _dependencies(text: object) -> tuple[str, ...]:
    decoded = json.loads(str(text))
    if not isinstance(decoded, list):
        raise StorageError("a stored workflow step dependency list is not a JSON array")
    return tuple(str(entry) for entry in decoded)


# --- records ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkflowPlanStep:
    """One step of a sealed plan, as 0027 stores it.

    Both content addresses are carried rather than one: `step_definition_hash` is what
    the author declared and `materialised_step_hash` is what materialisation derived,
    and a reader that could see only the second could not tell whether a plan it is
    looking at came from the definition it claims to.
    """

    step_id: str
    component_id: str
    component_version: str
    execution_class: str
    route: str
    sequence_index: int
    depends_on: tuple[str, ...]
    step_definition_hash: str
    materialised_step_hash: str
    branch: dict[str, Any] | None = None
    loop: dict[str, Any] | None = None
    child_workflow: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class WorkflowPlan:
    """One sealed workflow definition, its plan, and the steps that plan orders.

    A local record rather than a contract one, for the reason
    :class:`~storage.agent_runtime.RunStop` is local: accepted v1 has no workflow plan
    shape, and publishing one as if it were canonical would be this package inventing a
    record the contract does not have.
    """

    workspace_id: str
    workflow_id: str
    workflow_version: str
    definition_hash: str
    plan_hash: str
    sealed_at: str
    audit_reference: str
    steps: tuple[WorkflowPlanStep, ...]


@dataclass(frozen=True, slots=True)
class WorkflowRunBinding:
    """The one immutable binding of a runtime run to the plan it runs."""

    workspace_id: str
    run_id: str
    workflow_id: str
    workflow_version: str
    plan_hash: str
    bound_at: str


@dataclass(frozen=True, slots=True)
class WorkflowRunAdmission:
    """What a Workflow Run states at admission, and nothing it learns later.

    The three identity fields are the pins. They are stated by the caller rather than
    read from the plan, because a pin read from the thing it is meant to pin proves
    nothing: an admission that names a plan hash the sealed plan does not carry is
    refused, which is the whole point of stating it.
    """

    run_id: str
    workflow_id: str
    workflow_version: str
    plan_hash: str
    bound_at_us: int


@dataclass(frozen=True, slots=True)
class WorkflowStepObservation:
    """One replay-safe observation of one step of one run.

    A `plan` observation carries the route and position; a `branch` observation carries
    the outcome and its reason. Never both, because 0027 gives them one row each and a
    record that could carry both would be describing two facts as one.
    """

    step_id: str
    observation_kind: str
    observed_at: str
    route: str | None = None
    sequence_index: int | None = None
    branch_outcome: str | None = None
    branch_reason: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowStepConnection:
    """One connection fact one run recorded about one of its steps.

    A scheduler fact, not a graph evaluation: it says this run recorded this connection
    as `required` or as `selected`, in this direction, at this instant. What made that
    true -- a condition, an operator, an operand -- is upstream and is not stored here.
    """

    step_id: str
    direction: str
    connection_id: str
    fact_kind: str
    recorded_at: str


@dataclass(frozen=True, slots=True)
class WorkflowStepReadinessFact:
    """One mapped-input or capability readiness fact recorded for a run step.

    `fact_kind` says whether the fact is a requirement or its matching satisfaction;
    `fact_id` is the stable mapped-input or capability identifier. The scheduler can
    compare sets, but it cannot infer or re-evaluate why a fact is true.
    """

    step_id: str
    fact_kind: str
    fact_id: str
    recorded_at: str


@dataclass(frozen=True, slots=True)
class WorkflowLoopIteration:
    """One claimed iteration of one loop step, and its completion if it has one.

    `completed_at` is `None` for the open iteration, and 0031 admits at most one of
    those per step. There is no status column beside it: an iteration with no outcome
    row *is* the open one, and a second answer to that would have nowhere to live.
    """

    step_id: str
    iteration_number: int
    loop_iteration_id: str
    runtime_attempt_id: str
    opened_at: str
    status: str | None = None
    cost: int | None = None
    continue_requested: bool | None = None
    exit_reason: str | None = None
    completed_at: str | None = None

    @property
    def is_open(self) -> bool:
        return self.completed_at is None


@dataclass(frozen=True, slots=True)
class WorkflowChildResult:
    """One thing a child correlation consumed, or the one thing that closed it."""

    result_sequence: int
    outcome: str
    fence: int
    child_workflow_id: str
    child_version: str
    child_workflow_hash: str
    recorded_at: str
    cost: int | None = None


@dataclass(frozen=True, slots=True)
class WorkflowChildCorrelation:
    """One fenced, budgeted parent/child binding and everything recorded against it.

    `consumed` is the sum of the costs already accepted and `is_closed` says whether a
    closing result has landed. Both are read off the stored results rather than kept as
    columns beside them, so neither can drift from the rows it summarises.
    """

    correlation_id: str
    parent_step_id: str
    child_workflow_id: str
    child_version: str
    child_workflow_hash: str
    fence: int
    budget: int
    opened_at: str
    results: tuple[WorkflowChildResult, ...]

    @property
    def consumed(self) -> int:
        return sum(result.cost or 0 for result in self.results)

    @property
    def is_closed(self) -> bool:
        return any(result.outcome == CHILD_RESULT_CLOSED for result in self.results)


@dataclass(frozen=True, slots=True)
class WorkflowCompletionEvidence:
    """One kind of evidence a completion may be gated on, and its digest."""

    evidence_kind: str
    evidence_digest: str
    recorded_at: str


@dataclass(frozen=True, slots=True)
class WorkflowCompletion:
    """The single evidence-gated completion decision one run receives."""

    outcome: str
    decided_at: str
    audit_reference: str


@dataclass(frozen=True, slots=True)
class WorkflowRunView:
    """Everything durable state can honestly report about one Workflow Run.

    Deliberately not a run status, a progress percentage or a next-step answer. What is
    stored at this milestone is the plan, the binding, the observations, the
    correlations, the evidence and the decision; attempts, waits, scheduler readiness
    and executor results are not stored yet, and a field for them here would be a
    fabricated one. Each collection is empty for a run that has none, which is an
    answer rather than a gap.
    """

    workspace_id: str
    run_id: str
    plan: WorkflowPlan
    bound_at: str
    plan_observations: tuple[WorkflowStepObservation, ...] = ()
    branch_observations: tuple[WorkflowStepObservation, ...] = ()
    step_connections: tuple[WorkflowStepConnection, ...] = ()
    step_readiness_facts: tuple[WorkflowStepReadinessFact, ...] = ()
    loop_iterations: tuple[WorkflowLoopIteration, ...] = ()
    correlations: tuple[WorkflowChildCorrelation, ...] = ()
    completion_evidence: tuple[WorkflowCompletionEvidence, ...] = ()
    completion: WorkflowCompletion | None = None


# --- writes -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkflowWriter:
    """Every durable Workflow Runtime write, issued into an already-open transaction.

    Not constructible usefully on its own: :func:`workflow_writer` and
    :func:`transaction_local_workflow_writer` are what hand one out, exactly as
    :class:`~storage.agent_runtime.RuntimeWriter` is handed out, and for the same
    reason -- a composition that has to seal a plan and bind a run together cannot get
    there by calling two functions that each open their own `BEGIN IMMEDIATE`.
    """

    connection: sqlite3.Connection
    workspace_id: str

    def seal_plan(
        self,
        plan: MaterialisedWorkflow,
        *,
        sealed_at_us: int,
        audit_ref: str,
    ) -> WorkflowPlan:
        """Materialise one sealed workflow into its immutable plan rows, idempotently.

        The plan is a re-addressing of the definition, so every column is derived from
        `plan` and nothing is taken on the caller's word: the hashes are the sealed
        ones, the order is the materialised one, and each step's route comes from the
        same router the in-memory seam uses.

        Re-sealing the identical plan is the same plan and writes nothing -- the stored
        record is returned, so a caller retrying after a crash proceeds against what is
        actually stored. Re-sealing a *different* definition or plan under the same
        workflow and version is a conflict and raises: 0027 seals a plan that has
        admitted a run, and a plan that has not is still immutable.
        """
        plan.verify_content_hash()
        stored = read_workflow_plan(
            self.connection,
            workspace_id=self.workspace_id,
            workflow_id=plan.workflow_id,
            workflow_version=plan.version,
        )
        if stored is not None:
            if (stored.definition_hash, stored.plan_hash) != (
                plan.definition_hash,
                plan.content_hash,
            ):
                raise StorageError(
                    f"workflow {plan.workflow_id!r}/{plan.version} is already sealed as "
                    f"{stored.plan_hash!r}; a different plan under the same version is a "
                    "conflict, never a re-seal"
                )
            return stored
        self.connection.execute(
            f"INSERT INTO {PLANS} (workspace_id, {_PLAN_COLUMNS}) "
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
        for step in plan.steps:
            self._append_plan_step(plan, step)
        materialised = read_workflow_plan(
            self.connection,
            workspace_id=self.workspace_id,
            workflow_id=plan.workflow_id,
            workflow_version=plan.version,
        )
        if materialised is None:  # pragma: no cover - the insert above just committed it
            raise StorageError("a sealed workflow plan did not read back")
        return materialised

    def _append_plan_step(
        self, plan: MaterialisedWorkflow, step: MaterialisedStep
    ) -> None:
        self.connection.execute(
            f"INSERT INTO {PLAN_STEPS} "
            "(workspace_id, workflow_id, workflow_version, " + _STEP_COLUMNS + ") "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                plan.workflow_id,
                plan.version,
                step.step_id,
                step.component_id,
                step.component_version,
                step.execution_class,
                _ROUTER.route(step).route,
                step.sequence_index,
                _document(list(step.depends_on)),
                _document(None if step.branch is None else step.branch.preimage),
                _document(None if step.loop is None else step.loop.preimage),
                _document(
                    None if step.child_workflow is None else step.child_workflow.preimage
                ),
                step.definition_hash,
                step.content_hash,
            ),
        )

    def admit_workflow_run(
        self, admission: WorkflowRunAdmission
    ) -> WorkflowRunBinding:
        """Bind one runtime run to the exact plan it will run, idempotently.

        The bounded start seam. Three pins are enforced before a statement is issued --
        the workflow, its version and the plan hash -- against the plan actually sealed
        in this workspace, so an admission naming a plan that was never sealed, or one
        whose hash the sealed plan does not carry, writes nothing at all.

        Repeating an equivalent admission returns the stored binding and writes
        nothing. Repeating it under *different* pins is a conflict and raises, because
        one run binds one plan and a second answer about which plan has nowhere to
        live. `bound_at_us` is not part of the comparison: a retry after a crash reads
        its own clock, and the instant a binding already has is the one it keeps.

        That the runtime run exists at all, was admitted as `definition_kind`
        `workflow`, names this same workflow and version, and does not predate its own
        binding are 0027's guards. They are not restated here: a second copy of a rule
        is a second chance to disagree with it.
        """
        stored = read_workflow_run_binding(
            self.connection, workspace_id=self.workspace_id, run_id=admission.run_id
        )
        pins = (
            admission.workflow_id,
            admission.workflow_version,
            admission.plan_hash,
        )
        if stored is not None:
            if (stored.workflow_id, stored.workflow_version, stored.plan_hash) != pins:
                raise StorageError(
                    f"run {admission.run_id!r} is already bound to "
                    f"{stored.workflow_id!r}/{stored.workflow_version} at "
                    f"{stored.plan_hash!r}; a second binding is a conflict, never a "
                    "replay"
                )
            return stored
        plan = read_workflow_plan(
            self.connection,
            workspace_id=self.workspace_id,
            workflow_id=admission.workflow_id,
            workflow_version=admission.workflow_version,
        )
        if plan is None:
            raise StorageError(
                f"workflow {admission.workflow_id!r}/{admission.workflow_version} has no "
                "sealed plan in this workspace for a run to bind to"
            )
        if plan.plan_hash != admission.plan_hash:
            raise StorageError(
                f"workflow {admission.workflow_id!r}/{admission.workflow_version} is "
                f"sealed as {plan.plan_hash!r}, not {admission.plan_hash!r}; an "
                "admission runs the plan it pins or none at all"
            )
        self.connection.execute(
            f"INSERT INTO {RUNS} (workspace_id, {_RUN_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                admission.run_id,
                admission.workflow_id,
                admission.workflow_version,
                admission.plan_hash,
                admission.bound_at_us,
            ),
        )
        return WorkflowRunBinding(
            workspace_id=self.workspace_id,
            run_id=admission.run_id,
            workflow_id=admission.workflow_id,
            workflow_version=admission.workflow_version,
            plan_hash=admission.plan_hash,
            bound_at=runtime_timestamp(admission.bound_at_us),
        )

    def observe_plan_step(
        self, *, run_id: str, step: MaterialisedStep, observed_at_us: int
    ) -> None:
        """Record the route and position one step was planned at, replay-safely.

        Both values are derived from the materialised step rather than taken from the
        caller, so a stored observation is replay-*equivalent* to the plan by
        construction. `INSERT OR IGNORE` is what makes a replay silent; an observation
        that contradicts the one already recorded is refused by 0027's own explicit
        abort, which `OR IGNORE` does not suppress.
        """
        self.connection.execute(
            f"INSERT OR IGNORE INTO {OBSERVATIONS} "
            "(workspace_id, run_id, " + _OBSERVATION_COLUMNS + ") "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
            (
                self.workspace_id,
                run_id,
                step.step_id,
                OBSERVATION_PLAN,
                _ROUTER.route(step).route,
                step.sequence_index,
                observed_at_us,
            ),
        )

    def observe_plan_record(
        self, *, run_id: str, step: WorkflowPlanStep, observed_at_us: int
    ) -> None:
        """Record one already-stored plan step as a run observation, replay-safely.

        The M3 scheduler bridge starts from the durable plan rows rather than from an
        in-memory `MaterialisedStep`. It may therefore replay exactly what M2 stored:
        step id, route and sequence index, still under the same 0027 conflict checks.
        """
        self.connection.execute(
            f"INSERT OR IGNORE INTO {OBSERVATIONS} "
            "(workspace_id, run_id, " + _OBSERVATION_COLUMNS + ") "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
            (
                self.workspace_id,
                run_id,
                step.step_id,
                OBSERVATION_PLAN,
                step.route,
                step.sequence_index,
                observed_at_us,
            ),
        )

    def observe_branch(
        self, *, run_id: str, step_id: str, result: BranchResult, observed_at_us: int
    ) -> None:
        """Record one branch evaluation, replay-safely.

        `BLOCKED` is stored exactly as `MATCHED` and `UNMATCHED` are. A gate that
        abstained is a fact the run has to be able to show afterwards, not an absence.
        """
        self.connection.execute(
            f"INSERT OR IGNORE INTO {OBSERVATIONS} "
            "(workspace_id, run_id, " + _OBSERVATION_COLUMNS + ") "
            "VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?)",
            (
                self.workspace_id,
                run_id,
                step_id,
                OBSERVATION_BRANCH,
                result.outcome,
                result.reason,
                observed_at_us,
            ),
        )

    def record_step_connection(
        self,
        *,
        run_id: str,
        step_id: str,
        direction: str,
        connection_id: str,
        fact_kind: str,
        recorded_at_us: int,
    ) -> None:
        """Record one connection fact about one step of one run, replay-safely.

        Every column but the instant is in 0030's key, so an identical replay is read
        before writing and returns silently. The actual write is a plain `INSERT`, not
        `INSERT OR IGNORE`: bad direction/fact vocabularies, a step that is not in this
        run's plan, a fact predating the run's binding and a writer that is not the
        fenced owner must all fail loudly rather than being ignored.
        """
        exists = self.connection.execute(
            f"SELECT 1 FROM {STEP_CONNECTIONS} WHERE workspace_id = ? AND run_id = ? "
            "AND step_id = ? AND direction = ? AND connection_id = ? "
            "AND fact_kind = ?",
            (
                self.workspace_id,
                run_id,
                step_id,
                direction,
                connection_id,
                fact_kind,
            ),
        ).fetchone()
        if exists is not None:
            return
        self.connection.execute(
            f"INSERT INTO {STEP_CONNECTIONS} "
            "(workspace_id, run_id, step_id, direction, connection_id, fact_kind, "
            "recorded_at_us) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                run_id,
                step_id,
                direction,
                connection_id,
                fact_kind,
                recorded_at_us,
            ),
        )

    def record_step_readiness_fact(
        self,
        *,
        run_id: str,
        step_id: str,
        fact_kind: str,
        fact_id: str,
        recorded_at_us: int,
    ) -> None:
        """Record one mapped-input or capability readiness fact, replay-safely."""
        exists = self.connection.execute(
            f"SELECT 1 FROM {READINESS_FACTS} WHERE workspace_id = ? AND run_id = ? "
            "AND step_id = ? AND fact_kind = ? AND fact_id = ?",
            (self.workspace_id, run_id, step_id, fact_kind, fact_id),
        ).fetchone()
        if exists is not None:
            return
        self.connection.execute(
            f"INSERT INTO {READINESS_FACTS} "
            "(workspace_id, run_id, step_id, fact_kind, fact_id, recorded_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                run_id,
                step_id,
                fact_kind,
                fact_id,
                recorded_at_us,
            ),
        )

    def open_loop_iteration(
        self,
        *,
        run_id: str,
        step_id: str,
        loop_iteration_id: str,
        runtime_attempt_id: str,
        opened_at_us: int,
    ) -> int:
        """Claim the next iteration of one loop step, and return its number.

        The number is allocated from the step's own stored iterations inside the
        transaction rather than taken from the caller, for the reason
        :meth:`open_child_correlation` allocates its fence there: two writers that
        agreed on a number would both hand the trigger one it has to reject.

        Everything that makes this claim safe is 0031's, not restated here: the step
        must declare a loop, the numbering must be contiguous, the attempt must be the
        one every iteration of this step names, the bound must not be exceeded, and a
        step that already holds an open iteration cannot claim a second.
        """
        number = self._next(
            f"SELECT COALESCE(MAX(iteration_number), 0) + 1 FROM {LOOP_ITERATIONS} "
            "WHERE workspace_id = ? AND run_id = ? AND step_id = ?",
            (self.workspace_id, run_id, step_id),
        )
        self.connection.execute(
            f"INSERT INTO {LOOP_ITERATIONS} "
            "(workspace_id, run_id, step_id, iteration_number, loop_iteration_id, "
            "runtime_attempt_id, opened_at_us) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                run_id,
                step_id,
                number,
                loop_iteration_id,
                runtime_attempt_id,
                opened_at_us,
            ),
        )
        return number

    def complete_loop_iteration(
        self,
        *,
        loop_iteration_id: str,
        cost: int,
        continue_requested: bool,
        exit_reason: str | None,
        completed_at_us: int,
        status: str = LOOP_ITERATION_SUCCEEDED,
    ) -> None:
        """Close one claimed loop iteration with what it cost and what follows it.

        A plain `INSERT`, not `INSERT OR IGNORE`: the key is the iteration alone, so
        ignoring a duplicate would silently drop a *different* cost or exit reason
        claimed for the same iteration, which is a contradiction rather than a replay.
        Both budgets are 0031's refusals, checked against the step's own `loop_json`.
        """
        self.connection.execute(
            f"INSERT INTO {LOOP_ITERATION_OUTCOMES} "
            "(workspace_id, loop_iteration_id, status, cost, continue_requested, "
            "exit_reason, completed_at_us) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                loop_iteration_id,
                status,
                cost,
                int(continue_requested),
                exit_reason,
                completed_at_us,
            ),
        )

    def open_child_correlation(
        self,
        *,
        correlation_id: str,
        parent_run_id: str,
        parent_step_id: str,
        child: ChildWorkflowDefinition,
        opened_at_us: int,
    ) -> int:
        """Open one fenced, budgeted parent/child binding, and return its fence.

        The fence is allocated from the parent step's own fence line inside the
        transaction rather than taken from the caller, for the reason
        :meth:`~storage.agent_runtime.RuntimeWriter.append_run_event` allocates its
        sequence there: two writers that agreed on a number would both hand the trigger
        one it has to reject.
        """
        fence = self._next(
            f"SELECT COALESCE(MAX(fence), 0) + 1 FROM {CORRELATIONS} "
            "WHERE workspace_id = ? AND parent_run_id = ? AND parent_step_id = ?",
            (self.workspace_id, parent_run_id, parent_step_id),
        )
        self.connection.execute(
            f"INSERT INTO {CORRELATIONS} "
            "(workspace_id, parent_run_id, correlation_id, parent_step_id, "
            "child_workflow_id, child_version, child_workflow_hash, fence, budget, "
            "opened_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                parent_run_id,
                correlation_id,
                parent_step_id,
                child.workflow_id,
                child.version,
                child.workflow_hash,
                fence,
                child.budget,
                opened_at_us,
            ),
        )
        return fence

    def record_child_result(
        self,
        *,
        correlation_id: str,
        outcome: str,
        fence: int,
        child_workflow_id: str,
        child_version: str,
        child_workflow_hash: str,
        recorded_at_us: int,
        cost: int | None = None,
    ) -> int:
        """Record what one correlation consumed or closed, and number it.

        The fence and the child identity are the caller's claim, not this module's
        lookup: reading them off the correlation being written to would satisfy 0027's
        staleness and identity checks trivially, which is the opposite of checking
        them. A stale fence, a wrong child, an over-budget cost and a result after the
        close are all the migration's refusals.
        """
        sequence = self._next(
            f"SELECT COALESCE(MAX(result_sequence), 0) + 1 FROM {CORRELATION_RESULTS} "
            "WHERE workspace_id = ? AND correlation_id = ?",
            (self.workspace_id, correlation_id),
        )
        self.connection.execute(
            f"INSERT INTO {CORRELATION_RESULTS} "
            "(workspace_id, correlation_id, " + _RESULT_COLUMNS + ") "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                correlation_id,
                sequence,
                outcome,
                fence,
                child_workflow_id,
                child_version,
                child_workflow_hash,
                cost,
                recorded_at_us,
            ),
        )
        return sequence

    def record_completion_evidence(
        self,
        *,
        run_id: str,
        evidence_kind: str,
        evidence_digest: str,
        recorded_at_us: int,
    ) -> None:
        """Record one piece of the evidence a completion may be gated on.

        A plain insert, not `INSERT OR IGNORE`: the key is the kind, so ignoring a
        duplicate would silently drop a *different digest* claimed for the same kind,
        which is a contradiction rather than a replay. Evidence appended after the
        decision it was supposed to gate is 0027's refusal.
        """
        self.connection.execute(
            f"INSERT INTO {COMPLETION_EVIDENCE} "
            "(workspace_id, run_id, evidence_kind, evidence_digest, recorded_at_us) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                self.workspace_id,
                run_id,
                evidence_kind,
                evidence_digest,
                recorded_at_us,
            ),
        )

    def complete_run(
        self, *, run_id: str, outcome: str, decided_at_us: int, audit_ref: str
    ) -> None:
        """Record the one completion decision a Workflow Run receives.

        That the run named at least one evidence kind is 0027's gate, and it is the
        part that is a durable fact. Whether *those particular* kinds satisfy the
        `CompletionRule` the workflow was configured with is a comparison against a
        configured rule nothing stores yet.

        TODO(M5): evaluate the run's stored evidence against its configured
        `CompletionRule` once a completion rule is durable. Until then the rule lives
        only in :class:`~execution.workflow.CompletionEvaluator`, in memory, and this
        seam records the decision a caller already made rather than making it.
        """
        self.connection.execute(
            f"INSERT INTO {COMPLETIONS} "
            "(workspace_id, run_id, outcome, decided_at_us, audit_ref) "
            "VALUES (?, ?, ?, ?, ?)",
            (self.workspace_id, run_id, outcome, decided_at_us, audit_ref),
        )

    def _next(self, query: str, parameters: tuple[object, ...]) -> int:
        row = self.connection.execute(query, parameters).fetchone()
        if row is None:  # pragma: no cover - an aggregate always returns one row
            raise StorageError("a workflow sequence allocation returned no row")
        return int(row[0])


def transaction_local_workflow_writer(
    connection: sqlite3.Connection, *, workspace_id: str
) -> WorkflowWriter:
    """The workflow writes, for a caller that already holds a fenced transaction.

    Weakens no fencing: it opens no transaction and validates no authority, so
    everything it issues is covered by the transaction the caller opened, and the
    persisted triggers refuse an unguarded insert on every 0027 table regardless of
    which Python object issued it.
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
    """One fenced transaction, and the workflow writes that may be issued into it."""
    with fenced_transaction(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ):
        yield transaction_local_workflow_writer(connection, workspace_id=workspace_id)


def seal_workflow_plan(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    plan: MaterialisedWorkflow,
    sealed_at_us: int,
    audit_ref: str,
) -> WorkflowPlan:
    """Seal one materialised workflow, in its own fenced transaction."""
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
    admission: WorkflowRunAdmission,
) -> WorkflowRunBinding:
    """Bind one runtime run to its pinned plan, in its own fenced transaction."""
    with workflow_writer(
        connection,
        identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
    ) as writer:
        return writer.admit_workflow_run(admission)


# --- reads ------------------------------------------------------------------------


def read_workflow_plan(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    workflow_id: str,
    workflow_version: str,
) -> WorkflowPlan | None:
    """One sealed plan and its steps in materialised order, or `None`."""
    row = connection.execute(
        f"SELECT {_PLAN_COLUMNS} FROM {PLANS} "
        "WHERE workspace_id = ? AND workflow_id = ? AND workflow_version = ?",
        (workspace_id, workflow_id, workflow_version),
    ).fetchone()
    if row is None:
        return None
    steps = connection.execute(
        f"SELECT {_STEP_COLUMNS} FROM {PLAN_STEPS} "
        "WHERE workspace_id = ? AND workflow_id = ? AND workflow_version = ? "
        "ORDER BY sequence_index",
        (workspace_id, workflow_id, workflow_version),
    ).fetchall()
    return WorkflowPlan(
        workspace_id=workspace_id,
        workflow_id=str(row[0]),
        workflow_version=str(row[1]),
        definition_hash=str(row[2]),
        plan_hash=str(row[3]),
        sealed_at=runtime_timestamp(int(row[4])),
        audit_reference=str(row[5]),
        steps=tuple(
            WorkflowPlanStep(
                step_id=str(step[0]),
                component_id=str(step[1]),
                component_version=str(step[2]),
                execution_class=str(step[3]),
                route=str(step[4]),
                sequence_index=int(step[5]),
                depends_on=_dependencies(step[6]),
                branch=_decoded(step[7]),
                loop=_decoded(step[8]),
                child_workflow=_decoded(step[9]),
                step_definition_hash=str(step[10]),
                materialised_step_hash=str(step[11]),
            )
            for step in steps
        ),
    )


def read_workflow_run_binding(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> WorkflowRunBinding | None:
    """The plan this run is bound to, or `None` when it is not a Workflow Run."""
    row = connection.execute(
        f"SELECT {_RUN_COLUMNS} FROM {RUNS} WHERE workspace_id = ? AND run_id = ?",
        (workspace_id, run_id),
    ).fetchone()
    if row is None:
        return None
    return WorkflowRunBinding(
        workspace_id=workspace_id,
        run_id=str(row[0]),
        workflow_id=str(row[1]),
        workflow_version=str(row[2]),
        plan_hash=str(row[3]),
        bound_at=runtime_timestamp(int(row[4])),
    )


def read_workflow_run_observations(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    run_id: str,
    observation_kind: str,
) -> tuple[WorkflowStepObservation, ...]:
    """One run's observations of one kind, in the plan's own step order.

    Ordered by the plan's `sequence_index` rather than by observation instant: two
    steps observed in the same microsecond would otherwise come back in whatever order
    the page happened to hold, and the plan order is the one a reviewer reads in.
    """
    rows = connection.execute(
        "SELECT o.step_id, o.observation_kind, o.route, o.sequence_index, "
        "o.branch_outcome, o.branch_reason, o.observed_at_us "
        f"FROM {OBSERVATIONS} o "
        f"JOIN {RUNS} r ON r.workspace_id = o.workspace_id AND r.run_id = o.run_id "
        f"JOIN {PLAN_STEPS} s ON s.workspace_id = r.workspace_id "
        "AND s.workflow_id = r.workflow_id "
        "AND s.workflow_version = r.workflow_version AND s.step_id = o.step_id "
        "WHERE o.workspace_id = ? AND o.run_id = ? AND o.observation_kind = ? "
        "ORDER BY s.sequence_index",
        (workspace_id, run_id, observation_kind),
    ).fetchall()
    return tuple(
        WorkflowStepObservation(
            step_id=str(row[0]),
            observation_kind=str(row[1]),
            route=None if row[2] is None else str(row[2]),
            sequence_index=None if row[3] is None else int(row[3]),
            branch_outcome=None if row[4] is None else str(row[4]),
            branch_reason=None if row[5] is None else str(row[5]),
            observed_at=runtime_timestamp(int(row[6])),
        )
        for row in rows
    )


def read_workflow_run_step_connections(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    run_id: str,
    fact_kind: str | None = None,
) -> tuple[WorkflowStepConnection, ...]:
    """One run's connection facts, of one kind or of both, in stable key order."""
    rows = connection.execute(
        "SELECT step_id, direction, connection_id, fact_kind, recorded_at_us "
        f"FROM {STEP_CONNECTIONS} WHERE workspace_id = ? AND run_id = ? "
        "AND (? IS NULL OR fact_kind = ?) "
        "ORDER BY step_id, fact_kind, direction, connection_id",
        (workspace_id, run_id, fact_kind, fact_kind),
    ).fetchall()
    return tuple(
        WorkflowStepConnection(
            step_id=str(row[0]),
            direction=str(row[1]),
            connection_id=str(row[2]),
            fact_kind=str(row[3]),
            recorded_at=runtime_timestamp(int(row[4])),
        )
        for row in rows
    )


def read_workflow_run_step_readiness_facts(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    run_id: str,
    fact_kind: str | None = None,
) -> tuple[WorkflowStepReadinessFact, ...]:
    """One run's mapped-input and capability readiness facts in stable key order."""
    rows = connection.execute(
        "SELECT step_id, fact_kind, fact_id, recorded_at_us "
        f"FROM {READINESS_FACTS} WHERE workspace_id = ? AND run_id = ? "
        "AND (? IS NULL OR fact_kind = ?) "
        "ORDER BY step_id, fact_kind, fact_id",
        (workspace_id, run_id, fact_kind, fact_kind),
    ).fetchall()
    return tuple(
        WorkflowStepReadinessFact(
            step_id=str(row[0]),
            fact_kind=str(row[1]),
            fact_id=str(row[2]),
            recorded_at=runtime_timestamp(int(row[3])),
        )
        for row in rows
    )


def read_workflow_loop_iterations(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    run_id: str,
    step_id: str | None = None,
) -> tuple[WorkflowLoopIteration, ...]:
    """One run's loop iterations, for one step or for every step, in claim order.

    A `LEFT JOIN` rather than two reads: the open iteration is exactly the one with no
    outcome row, and a caller that had to notice its absence by comparing two lists
    could get that comparison wrong.
    """
    rows = connection.execute(
        "SELECT i.step_id, i.iteration_number, i.loop_iteration_id, "
        "i.runtime_attempt_id, i.opened_at_us, o.status, o.cost, "
        "o.continue_requested, o.exit_reason, o.completed_at_us "
        f"FROM {LOOP_ITERATIONS} i "
        f"LEFT JOIN {LOOP_ITERATION_OUTCOMES} o "
        "ON o.workspace_id = i.workspace_id "
        "AND o.loop_iteration_id = i.loop_iteration_id "
        "WHERE i.workspace_id = ? AND i.run_id = ? "
        "AND (? IS NULL OR i.step_id = ?) "
        "ORDER BY i.step_id, i.iteration_number",
        (workspace_id, run_id, step_id, step_id),
    ).fetchall()
    return tuple(
        WorkflowLoopIteration(
            step_id=str(row[0]),
            iteration_number=int(row[1]),
            loop_iteration_id=str(row[2]),
            runtime_attempt_id=str(row[3]),
            opened_at=runtime_timestamp(int(row[4])),
            status=None if row[5] is None else str(row[5]),
            cost=None if row[6] is None else int(row[6]),
            continue_requested=None if row[7] is None else bool(row[7]),
            exit_reason=None if row[8] is None else str(row[8]),
            completed_at=None if row[9] is None else runtime_timestamp(int(row[9])),
        )
        for row in rows
    )


def read_workflow_child_correlations(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> tuple[WorkflowChildCorrelation, ...]:
    """Every correlation this run's steps opened, each with the results it holds."""
    rows = connection.execute(
        f"SELECT {_CORRELATION_COLUMNS} FROM {CORRELATIONS} "
        "WHERE workspace_id = ? AND parent_run_id = ? "
        "ORDER BY parent_step_id, fence",
        (workspace_id, run_id),
    ).fetchall()
    return tuple(
        WorkflowChildCorrelation(
            correlation_id=str(row[0]),
            parent_step_id=str(row[1]),
            child_workflow_id=str(row[2]),
            child_version=str(row[3]),
            child_workflow_hash=str(row[4]),
            fence=int(row[5]),
            budget=int(row[6]),
            opened_at=runtime_timestamp(int(row[7])),
            results=_child_results(
                connection, workspace_id=workspace_id, correlation_id=str(row[0])
            ),
        )
        for row in rows
    )


def _child_results(
    connection: sqlite3.Connection, *, workspace_id: str, correlation_id: str
) -> tuple[WorkflowChildResult, ...]:
    rows = connection.execute(
        f"SELECT {_RESULT_COLUMNS} FROM {CORRELATION_RESULTS} "
        "WHERE workspace_id = ? AND correlation_id = ? ORDER BY result_sequence",
        (workspace_id, correlation_id),
    ).fetchall()
    return tuple(
        WorkflowChildResult(
            result_sequence=int(row[0]),
            outcome=str(row[1]),
            fence=int(row[2]),
            child_workflow_id=str(row[3]),
            child_version=str(row[4]),
            child_workflow_hash=str(row[5]),
            cost=None if row[6] is None else int(row[6]),
            recorded_at=runtime_timestamp(int(row[7])),
        )
        for row in rows
    )


def read_workflow_completion_evidence(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> tuple[WorkflowCompletionEvidence, ...]:
    """Every evidence kind recorded for this run, in kind order."""
    rows = connection.execute(
        "SELECT evidence_kind, evidence_digest, recorded_at_us "
        f"FROM {COMPLETION_EVIDENCE} WHERE workspace_id = ? AND run_id = ? "
        "ORDER BY evidence_kind",
        (workspace_id, run_id),
    ).fetchall()
    return tuple(
        WorkflowCompletionEvidence(
            evidence_kind=str(row[0]),
            evidence_digest=str(row[1]),
            recorded_at=runtime_timestamp(int(row[2])),
        )
        for row in rows
    )


def read_workflow_completion(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> WorkflowCompletion | None:
    """The one decision this run received, or `None` while it has none."""
    row = connection.execute(
        "SELECT outcome, decided_at_us, audit_ref "
        f"FROM {COMPLETIONS} WHERE workspace_id = ? AND run_id = ?",
        (workspace_id, run_id),
    ).fetchone()
    if row is None:
        return None
    return WorkflowCompletion(
        outcome=str(row[0]),
        decided_at=runtime_timestamp(int(row[1])),
        audit_reference=str(row[2]),
    )


def read_workflow_run(
    connection: sqlite3.Connection, *, workspace_id: str, run_id: str
) -> WorkflowRunView | None:
    """Everything durable state holds about one Workflow Run, or `None`.

    The run detail and review-input projection. Every field is read from a stored row:
    nothing here derives a status, invents an attempt, reports a wait or reports a
    scheduler or executor answer, because none of those is stored at this milestone.
    """
    binding = read_workflow_run_binding(
        connection, workspace_id=workspace_id, run_id=run_id
    )
    if binding is None:
        return None
    plan = read_workflow_plan(
        connection,
        workspace_id=workspace_id,
        workflow_id=binding.workflow_id,
        workflow_version=binding.workflow_version,
    )
    if plan is None:  # pragma: no cover - a foreign key makes a bound plan exist
        raise StorageError(f"run {run_id!r} is bound to a plan this workspace lost")
    return WorkflowRunView(
        workspace_id=workspace_id,
        run_id=run_id,
        plan=plan,
        bound_at=binding.bound_at,
        plan_observations=read_workflow_run_observations(
            connection,
            workspace_id=workspace_id,
            run_id=run_id,
            observation_kind=OBSERVATION_PLAN,
        ),
        branch_observations=read_workflow_run_observations(
            connection,
            workspace_id=workspace_id,
            run_id=run_id,
            observation_kind=OBSERVATION_BRANCH,
        ),
        step_connections=read_workflow_run_step_connections(
            connection, workspace_id=workspace_id, run_id=run_id
        ),
        step_readiness_facts=read_workflow_run_step_readiness_facts(
            connection, workspace_id=workspace_id, run_id=run_id
        ),
        loop_iterations=read_workflow_loop_iterations(
            connection, workspace_id=workspace_id, run_id=run_id
        ),
        correlations=read_workflow_child_correlations(
            connection, workspace_id=workspace_id, run_id=run_id
        ),
        completion_evidence=read_workflow_completion_evidence(
            connection, workspace_id=workspace_id, run_id=run_id
        ),
        completion=read_workflow_completion(
            connection, workspace_id=workspace_id, run_id=run_id
        ),
    )


def read_workspace_workflow_run_ids(
    connection: sqlite3.Connection, *, workspace_id: str
) -> tuple[str, ...]:
    """Every Workflow Run this workspace holds, in binding order then identifier order."""
    rows = connection.execute(
        f"SELECT run_id FROM {RUNS} WHERE workspace_id = ? ORDER BY bound_at_us, run_id",
        (workspace_id,),
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


__all__ = [
    "CHILD_RESULT_ACCEPTED",
    "CHILD_RESULT_CLOSED",
    "CONNECTION_INCOMING",
    "CONNECTION_OUTGOING",
    "CONNECTION_REQUIRED",
    "CONNECTION_SELECTED",
    "LOOP_EXIT_FAILED",
    "LOOP_EXIT_MAX_ITERATIONS",
    "LOOP_EXIT_REQUESTED",
    "LOOP_EXIT_TOTAL_BUDGET",
    "LOOP_ITERATION_FAILED",
    "LOOP_ITERATION_SUCCEEDED",
    "OBSERVATION_BRANCH",
    "OBSERVATION_PLAN",
    "READINESS_CAPABILITY_GRANTED",
    "READINESS_CAPABILITY_REQUIRED",
    "READINESS_MAPPED_INPUT_READY",
    "READINESS_MAPPED_INPUT_REQUIRED",
    "WorkflowChildCorrelation",
    "WorkflowChildResult",
    "WorkflowCompletion",
    "WorkflowCompletionEvidence",
    "WorkflowLoopIteration",
    "WorkflowPlan",
    "WorkflowPlanStep",
    "WorkflowRunAdmission",
    "WorkflowRunBinding",
    "WorkflowRunView",
    "WorkflowStepConnection",
    "WorkflowStepObservation",
    "WorkflowStepReadinessFact",
    "WorkflowWriter",
    "admit_workflow_run",
    "read_workflow_child_correlations",
    "read_workflow_completion",
    "read_workflow_completion_evidence",
    "read_workflow_loop_iterations",
    "read_workflow_plan",
    "read_workflow_run",
    "read_workflow_run_binding",
    "read_workflow_run_observations",
    "read_workflow_run_step_connections",
    "read_workflow_run_step_readiness_facts",
    "read_workspace_workflow_run_ids",
    "seal_workflow_plan",
    "transaction_local_workflow_writer",
    "workflow_writer",
]
