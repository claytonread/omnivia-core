"""What a sealed Workflow plan means to the canonical runtime, and nothing more.

`workflow.start` seals a plan and binds a Run to it. This module is the two things that
have to be true for that Run to actually execute: the plan's steps exist as canonical
`omnivia_runtime_run_steps` rows, and the generic scheduler knows which of them the plan
will admit next. It owns no queue, no executor, no second state machine and no state of
its own -- RT-106 still claims, RT-107 still resolves waits, RT-109 still recovers, 0025
still cancels, and every row written here is written through the existing writers.

**Opening is derivation, not planning.** Every column of every step comes off the sealed
plan: the identifier is a digest of the workspace, the run and the plan digest with the
plan's own `step_id`; the ordinal is the plan's `sequence_index`; the kind is the plan's
`route`. Nothing is allocated, nothing is routed a second time and nothing is chosen. A
plan is immutable once it has admitted a Run, so re-deriving this for the same Run always
produces the same rows -- which is what makes a replay recognisable rather than a second
opening, and what makes a *drifted* set of rows a refusal rather than something to
repair. History that no longer matches the plan it was derived from is not history this
module will write over.

**Readiness is the plan's dependency edges, and only those.** `depends_on` names steps,
and a dependency is satisfied when its own runtime step has succeeded -- not when it has
merely finished, because a failed or skipped predecessor has not produced what its
dependants were planned to consume. The sealed order is a topological one, so the plan
could almost be walked by ordinal alone; checking the edges is what makes that a property
this build enforces rather than one it happens to get away with.

**An `agent_component` run is not this module's business.** 0018's own
`definition_kind` is what says so, read off the canonical run row before anything else,
and both seams then answer for such a run the way the scheduler behaves with no plan at
all: every runnable step is ready, and nothing is observed. `agent_component` runs share
the scheduler and are unaffected by the Workflow lane existing.

**The binding is verified before it authorises anything.** A Workflow run's plan is
reached through T-0688's verifying binding reader, so drifted, non-canonical or
cross-linked binding bytes refuse at the readiness check -- before the durable job is
claimed, before an attempt is opened, before a step status, a plan observation or a run
event is written. Execution and `workflow.inspect` therefore fail closed on the same
evidence rather than one reporting what the other quietly ran anyway. A run that is not
in `omnivia_runtime_runs` at all, and a `workflow` run whose `omnivia_workflow_runs` row
or binding document is gone, refuse there too: each is evidence that something outside
these paths changed, and none of them is a run this module will execute unplanned.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Final

from omnivia_core_runtime.ownership.identity import Clock, ServiceInstanceIdentity
from omnivia_core_runtime.service.runtime_scheduler import (
    RuntimeScheduler,
    _lineage_id,
)
from omnivia_core_runtime.storage.agent_runtime import transaction_local_writer
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.workflow_runs import (
    SealedWorkflowPlan,
    StoredPlanStep,
    read_workflow_plan,
    read_workflow_run_binding,
    transaction_local_workflow_writer,
)
from omnivia_core_runtime.storage.workflow_runtime_hardening import (
    read_runtime_definition_binding,
)

__all__ = [
    "MAX_RUNTIME_STEP_ORDINAL",
    "WORKFLOW_STEP_KIND_PREFIX",
    "WorkflowStepPlan",
    "open_workflow_runtime_steps",
    "workflow_run_step_id",
    "workflow_runtime_scheduler",
    "workflow_step_kind",
]

#: The `step_kind` a Workflow plan step is opened as: the lane, then the plan's own
#: route. Lowercase and dotted because 0018's `step_kind` guard requires it, and carrying
#: the route means a canonical step row states which kind of work the plan said it was
#: without a second table to join.
WORKFLOW_STEP_KIND_PREFIX: Final = "workflow."

#: 0018 bounds a run's step ordinals to 1..256. A plan with more steps than that cannot be
#: opened at all, and saying so at the point of opening is better than a bare integrity
#: error from the two hundred and fifty-seventh insert.
MAX_RUNTIME_STEP_ORDINAL: Final = 256

_STEP_STATUS_SUCCEEDED: Final = "succeeded"

#: The `definition_kind` 0018 records for a run this lane serves. The only other value
#: 0018 admits is `agent_component`, which shares the scheduler and has no plan here.
_DEFINITION_KIND_WORKFLOW: Final = "workflow"

_RUN_DEFINITION_KIND: Final = (
    "SELECT definition_kind FROM omnivia_runtime_runs "
    "WHERE workspace_id = ? AND run_id = ?"
)

_STORED_RUNTIME_STEPS: Final = (
    "SELECT run_step_id, ordinal, step_kind FROM omnivia_runtime_run_steps "
    "WHERE workspace_id = ? AND run_id = ? ORDER BY ordinal"
)

_LATEST_STEP_STATUS: Final = (
    "SELECT status FROM omnivia_runtime_run_step_states "
    "WHERE workspace_id = ? AND run_step_id = ? ORDER BY state_sequence DESC LIMIT 1"
)


def workflow_run_step_id(
    *, workspace_id: str, run_id: str, plan_hash: str, step_id: str
) -> str:
    """The canonical runtime step one plan step of one run is executed as.

    Derived rather than allocated, from exactly the four facts that identify it: an
    allocator would mint a different identifier for the same step on a retry of the
    opening, and the second attempt would then open a duplicate step instead of
    recognising the first one. The plan digest is in the preimage because a step's
    identity is a statement about the plan it came from.
    """
    return _lineage_id("workflow_runtime_step", workspace_id, run_id, plan_hash, step_id)


def workflow_step_kind(route: str) -> str:
    """One plan route, as the canonical `step_kind` its runtime step is opened with."""
    return f"{WORKFLOW_STEP_KIND_PREFIX}{route.lower()}"


def open_workflow_runtime_steps(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    run_id: str,
    plan: SealedWorkflowPlan,
    opened_at_us: int,
) -> tuple[str, ...]:
    """Open one Run's canonical steps from its sealed plan, in the caller's transaction.

    Transaction-local on purpose: this runs inside `workflow.start`'s single mutation, so
    the steps commit with the job row, the canonical Run, the sealed plan and the binding
    or with none of them. A Run that is durable but has no steps to run, and steps that
    belong to a Run no commit ever admitted, are both states that cannot survive.

    Every step is opened, all `pending`, in the plan's own order. Opening only the
    currently-ready ones would have to allocate ordinals in the order work happened to
    become ready, and 0018 requires them contiguous from one -- so the plan's sequence
    would stop being the ordinal, and the run's step ledger would no longer read as the
    plan it was derived from. Which of them may be *claimed* is
    :class:`WorkflowStepPlan`'s question, asked at each claim, and it is what keeps a
    dependant from running before what it depends on.

    Re-deriving for a Run whose steps already match is a replay and writes nothing. Steps
    that do not match refuse: they were derived from this plan too, so a difference means
    the stored lineage is not the lineage this plan produces, and rewriting it would
    rewrite a Run's history rather than continue it.
    """
    expected = _expected_steps(workspace_id=workspace_id, run_id=run_id, plan=plan)
    stored = tuple(
        (str(row[0]), int(row[1]), str(row[2]))
        for row in connection.execute(
            _STORED_RUNTIME_STEPS, (workspace_id, run_id)
        ).fetchall()
    )
    if stored:
        if stored != expected:
            raise StorageError(
                f"run {run_id!r} already holds runtime steps that its sealed plan "
                f"{plan.plan_hash!r} does not derive"
            )
        return tuple(step[0] for step in stored)

    writer = transaction_local_writer(connection, workspace_id=workspace_id)
    for run_step_id, ordinal, step_kind in expected:
        writer.append_run_step(
            run_id=run_id,
            run_step_id=run_step_id,
            ordinal=ordinal,
            step_kind=step_kind,
            created_at_us=opened_at_us,
        )
    return tuple(step[0] for step in expected)


def _expected_steps(
    *, workspace_id: str, run_id: str, plan: SealedWorkflowPlan
) -> tuple[tuple[str, int, str], ...]:
    """The exact canonical steps one sealed plan derives for one run, in plan order."""
    if not plan.steps:  # pragma: no cover - 0027 refuses a run bound to an empty plan
        raise StorageError(f"workflow plan {plan.plan_hash!r} has no steps to open")
    if len(plan.steps) > MAX_RUNTIME_STEP_ORDINAL:
        raise StorageError(
            f"workflow plan {plan.plan_hash!r} has {len(plan.steps)} steps; a run admits "
            f"at most {MAX_RUNTIME_STEP_ORDINAL}"
        )
    return tuple(
        (
            workflow_run_step_id(
                workspace_id=workspace_id,
                run_id=run_id,
                plan_hash=plan.plan_hash,
                step_id=step.step_id,
            ),
            step.sequence_index + 1,
            workflow_step_kind(step.route),
        )
        for step in sorted(plan.steps, key=lambda step: step.sequence_index)
    )


@dataclass(frozen=True, slots=True)
class WorkflowStepPlan:
    """The sealed plan of whichever Workflow a claimed run happens to be bound to.

    One instance serves a whole workspace rather than one run, because the scheduler it
    is handed to serves a whole workspace: it is asked about the step it is about to
    claim, and it answers from the plan that step's run is bound to. A run bound to no
    Workflow answers as though there were no plan at all.
    """

    workspace_id: str

    def ready(
        self, connection: sqlite3.Connection, *, run_id: str, run_step_id: str
    ) -> bool:
        """Whether every step this one depends on has already succeeded.

        `succeeded`, not merely terminal. A dependant was planned to run after its
        dependency produced something, and a failed predecessor produced nothing -- so
        treating "finished somehow" as satisfaction would run the rest of a plan on top of
        a step that did not do its work.
        """
        plan, steps = self._plan(connection, run_id)
        if plan is None:
            return True
        step = steps.get(run_step_id)
        if step is None:
            # A canonical step of a Workflow run that its plan does not derive. Refusing
            # to claim it is the only safe answer: the plan cannot say what it should do.
            return False
        return all(
            _step_status(
                connection,
                self.workspace_id,
                workflow_run_step_id(
                    workspace_id=self.workspace_id,
                    run_id=run_id,
                    plan_hash=plan.plan_hash,
                    step_id=dependency,
                ),
            )
            == _STEP_STATUS_SUCCEEDED
            for dependency in step.depends_on
        )

    def observe(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        run_step_id: str,
        observed_at_us: int,
    ) -> None:
        """Record that a plan step was reached, with the route and position it was sealed at.

        Written at the instant an attempt opens over it, which is when the step was
        actually reached -- not when the run was admitted, which would publish an
        observation of work nothing had started. The route and sequence come off the
        sealed plan, and 0027's own trigger refuses a second observation of one step that
        states either differently, so the sealed plan's authority over both is enforced by
        the database on every claim rather than asserted here.
        """
        plan, steps = self._plan(connection, run_id)
        if plan is None:
            return
        step = steps.get(run_step_id)
        if step is None:  # pragma: no cover - `ready` refuses such a step first
            return
        transaction_local_workflow_writer(
            connection, workspace_id=self.workspace_id
        ).observe_plan_step(
            run_id=run_id,
            step_id=step.step_id,
            route=step.route,
            sequence_index=step.sequence_index,
            observed_at_us=observed_at_us,
        )

    def _plan(
        self, connection: sqlite3.Connection, run_id: str
    ) -> tuple[SealedWorkflowPlan | None, dict[str, StoredPlanStep]]:
        """The plan one run is bound to, indexed by the runtime step each step is run as.

        **The canonical run row decides what kind of run this is, and it decides first.**
        `omnivia_runtime_runs.definition_kind` is 0018's own answer to "is this a Workflow
        run", and it is the only row here that cannot be removed without removing the run:
        the stop ledger, the step ledger and the binding all point at it. Taking the
        *absence* of an `omnivia_workflow_runs` row as that answer instead would read
        "somebody deleted this run's Workflow row" as "this is an `agent_component` run",
        and execute the run with no plan gating it at all.

        A run this workspace does not hold is refused rather than answered as planless.
        The scheduler only ever asks about runs it just read out of these same tables, so
        reaching that refusal means the caller is asking about a run that is not there --
        which is not a question with a safe permissive answer.

        An `agent_component` run has no plan here and never had one: it shares the
        scheduler, and both seams answer for it exactly as a scheduler with no plan does.

        **A `workflow` run must produce both rows, verified, before anything is claimed or
        observed.** The raw `omnivia_workflow_runs` row says which plan it is bound to;
        `read_runtime_definition_binding` re-derives the binding document's digest, byte
        length and canonical spelling, re-validates it through the public contract, and
        re-runs 0035's own admission join against the run row and the sealed plan -- so a
        missing row or binding bytes edited outside this database's guards refuse here,
        before a durable job is claimed, an application or runtime attempt is opened, a
        step status is written, a plan observation is recorded or a run event is appended.
        Reading the three raw columns instead would let the execution path ignore exactly
        the drift `workflow.inspect` already fails closed on, which is the worse of the two
        answers: a Run reported as unreadable but executed anyway.

        A Workflow run holding *no* binding document is refused for the same reason. This
        lane writes the run and its binding in one commit, so there is no such run it can
        produce; a Legacy Run predating 0035 stays readable through `workflow.inspect`,
        which labels it as one, but a binding that cannot be produced is not a binding
        that can be confirmed, and confirming it is what authorises execution.
        """
        kind = connection.execute(
            _RUN_DEFINITION_KIND, (self.workspace_id, run_id)
        ).fetchone()
        if kind is None:
            raise StorageError(
                f"workspace {self.workspace_id!r} holds no canonical run {run_id!r} to "
                "plan against"
            )
        if str(kind[0]) != _DEFINITION_KIND_WORKFLOW:
            return None, {}
        bound = read_workflow_run_binding(
            connection, workspace_id=self.workspace_id, run_id=run_id
        )
        if bound is None:
            raise StorageError(
                f"run {run_id!r} is a workflow run this workspace holds no sealed plan "
                "binding for"
            )
        verified = read_runtime_definition_binding(
            connection, workspace_id=self.workspace_id, run_id=run_id
        )
        if verified is None:
            raise StorageError(
                f"run {run_id!r} is bound to a workflow but carries no binding document "
                "to execute against"
            )
        plan = read_workflow_plan(
            connection,
            workspace_id=self.workspace_id,
            workflow_id=bound.workflow_id,
            workflow_version=bound.workflow_version,
        )
        if plan is None:  # pragma: no cover - 0027's foreign key forbids this
            raise StorageError(
                f"run {run_id!r} binds a plan this workspace has not sealed"
            )
        # The plan reached here is looked up by workflow and version -- 0027's primary
        # key -- which is not the join the verifying reader made, and the reader is what
        # already refused every way these four could disagree. Restating them is what
        # makes "the steps about to run are the steps this binding names" a property of
        # this function rather than of a query in another module, and what would catch a
        # later reader that stopped checking one of them.
        document = verified.binding
        if (  # pragma: no cover - the verifying reader refuses each of these first
            document["workflowId"] != bound.workflow_id
            or document["workflowVersion"] != bound.workflow_version
            or document["definitionDigest"] != plan.definition_hash
            or bound.plan_hash != plan.plan_hash
        ):
            raise StorageError(
                f"run {run_id!r} would execute a plan its verified binding does not name"
            )
        return plan, {
            workflow_run_step_id(
                workspace_id=self.workspace_id,
                run_id=run_id,
                plan_hash=plan.plan_hash,
                step_id=step.step_id,
            ): step
            for step in plan.steps
        }


def _step_status(
    connection: sqlite3.Connection, workspace_id: str, run_step_id: str
) -> str | None:
    row = connection.execute(
        _LATEST_STEP_STATUS, (workspace_id, run_step_id)
    ).fetchone()
    return None if row is None else str(row[0])


def workflow_runtime_scheduler(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
    clock: Clock,
) -> RuntimeScheduler:
    """The one scheduler, told what this workspace's sealed Workflow plans require.

    A composition rather than a subclass or a second scheduler: claims, retries,
    settlement, terminalization and recovery are RT-106's, unchanged, and the only thing
    added is the dependency gate and the plan observation. A build that composed this for
    a workspace with no Workflow runs would behave exactly as the bare scheduler does.
    """
    # The assignment is also where `WorkflowStepPlan` is checked against
    # `RuntimeStepPlan`: the scheduler's field is typed as the protocol, so a signature
    # here that drifted from the seam is a type error at this line rather than an
    # attribute error inside whichever claim reached it first.
    return RuntimeScheduler(
        connection=connection,
        identity=identity,
        workspace_id=workspace_id,
        fencing_generation=fencing_generation,
        clock=clock,
        plan=WorkflowStepPlan(workspace_id),
    )
