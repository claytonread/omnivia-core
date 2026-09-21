"""The authoritative Workflow release authority, composed in the Core bootstrap.

Founder Ruling 2 settles composition ownership: the one authority that resolves a
released Workflow to the exact material a Run binds is composed in Core's own runtime
service bootstrap, and Platform and hosted Dev reach it only through the registered
`workflow.*` operations. `service/main.py` composes exactly one of these per served
workspace; nothing outside Core instantiates a competing resolver, and no caller has to
supply one for the production path to answer.

Which construction, and why this one
------------------------------------

The accepted contracts define no release catalogue, no definition store and no
execution-profile registry, and Ruling 2 forbids a second release store. What they *do*
define is the durable record of the releases this workspace has already admitted: 0027's
`omnivia_workflow_plans` holds one immutable sealed plan per exact `(workflow_id,
workflow_version)`, and 0035's `omnivia_workflow_runtime_bindings` holds the
`RuntimeDefinitionBinding` each Run of it was admitted with -- the pinned `releaseRef`,
the execution-profile, effective-policy and Component implementation digests, the
resource snapshots and the optional model-policy pair. That pair *is* the accepted
release record, so this authority reads it rather than opening a second one.

Re-resolution from that record is behaviour the accepted contracts already anticipate:
`workflow_runtime_hardening._BOUND_MATERIAL_FIELDS` excludes `bindingId`, `boundAt` and
`boundBy` from what a resume compares precisely so that "a re-resolution that restates
the same material at a new instant has not drifted". This authority restates exactly
those fields, from the binding this workspace committed, and stamps none of the
admission's own facts -- those stay the handler's, off its own allocator, clock and
authorised principal.

What it is not: a publication path. Core does not own release publication, so the *first*
release of a version still enters this workspace through whoever authored it. Until one
has, this authority answers "no such released workflow version is available here", which
is a refusal a caller can act on and never a licence to bind a Run to material nobody
released.

Every failure is explicit (Ruling 2)
------------------------------------

Three outcomes and no fourth. A version this workspace holds no sealed plan or no
believable binding for resolves to `None`, which `workflow.start` refuses as `not_found`.
A stored release that cannot be believed -- bytes that do not match their digest, a
document the public `RuntimeDefinitionBinding` contract rejects, a plan step whose
content hash no longer addresses its own preimage -- refuses as
`internal_non_recoverable` rather than being repaired, ignored or answered with a
neighbouring version. An instance not serving authoritative storage refuses as
`dependency_unavailable`. There is no nearest match, no `latest`, and no fallback to an
unapproved version: resolution answers for the exact version it was asked for or it does
not answer at all.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from omnivia_core.contracts.v1 import (
    ERROR_CODE_DEPENDENCY_UNAVAILABLE,
    ERROR_CODE_INTERNAL_NON_RECOVERABLE,
)
from omnivia_core_runtime.execution.profile import ExecutionError
from omnivia_core_runtime.execution.workflow import (
    BranchDefinition,
    ChildWorkflowDefinition,
    LoopDefinition,
    MaterialisedStep,
    MaterialisedWorkflow,
)
from omnivia_core_runtime.service.handlers.workflow import WorkflowRelease
from omnivia_core_runtime.service.operations import application_refusal
from omnivia_core_runtime.storage.connection import StorageError
from omnivia_core_runtime.storage.workflow_runs import (
    SealedWorkflowPlan,
    StoredPlanStep,
    read_workflow_plan,
)
from omnivia_core_runtime.storage.workflow_runtime_hardening import (
    read_runtime_definition_binding,
)

#: Exactly the members of a `RuntimeDefinitionBinding` that state the *release*, which is
#: the whole of what a resolver may state. The remainder of the document --
#: `bindingSchemaVersion`, `bindingId`, `workflowId`, `workflowVersion`,
#: `definitionDigest`, `boundAt`, `boundBy` -- is either derived from the plan or a fact
#: about one admission, and `handlers/workflow.py` stamps all of it itself.
_RELEASE_MATERIAL_FIELDS: Final[tuple[str, ...]] = (
    "releaseRef",
    "executionProfileDigest",
    "effectivePolicyDigest",
    "componentImplementationDigests",
    "resourceBindingSnapshots",
    "modelPolicySnapshotRef",
    "modelPolicySnapshotDigest",
)

#: The bound Runs of one sealed plan, oldest first. 0027 makes a plan immutable once it
#: has admitted a Run, so every Run of one version names the same material and the oldest
#: is the one that established it.
_RUN_OF_VERSION: Final = (
    "SELECT run_id FROM omnivia_workflow_runs "
    "WHERE workspace_id = ? AND workflow_id = ? AND workflow_version = ? "
    "ORDER BY bound_at_us, run_id LIMIT 1"
)

_MESSAGE_NO_STORAGE: Final = (
    "this service instance is not serving authoritative workflow storage"
)
_MESSAGE_UNBELIEVABLE: Final = (
    "this workspace holds a release record for that workflow version that cannot be "
    "believed, so no run may be bound to it: {reason}"
)


@dataclass(frozen=True)
class WorkspaceWorkflowReleaseAuthority:
    """The composed release authority for one served workspace.

    `service` is the live `ServiceRunner`, held rather than its connection, because the
    connection is established by startup and a bootstrap that captured it early would
    pin a handle the runner may have replaced. Read the same way the Workflow handlers
    read theirs, and absent means refuse rather than proceed.
    """

    service: Any
    workspace_id: str

    def __call__(
        self, *, workflow_id: str, workflow_version: str
    ) -> WorkflowRelease | None:
        """The exact released material for one version, or `None` for no such release."""
        connection = getattr(self.service, "connection", None)
        if connection is None:
            raise application_refusal(
                ERROR_CODE_DEPENDENCY_UNAVAILABLE, _MESSAGE_NO_STORAGE
            )
        try:
            plan = read_workflow_plan(
                connection,
                workspace_id=self.workspace_id,
                workflow_id=workflow_id,
                workflow_version=workflow_version,
            )
            if plan is None:
                return None
            material = self._material(connection, workflow_id, workflow_version)
            if material is None:
                return None
            return WorkflowRelease(plan=_materialised(plan), material=material)
        except (StorageError, ExecutionError, KeyError, TypeError, ValueError) as error:
            # A release this workspace does hold but cannot state truthfully. Refusing is
            # the whole point: the alternative is binding a Run to material that failed
            # its own address, which is exactly the fabricated execution history the
            # binding exists to prevent.
            raise application_refusal(
                ERROR_CODE_INTERNAL_NON_RECOVERABLE,
                _MESSAGE_UNBELIEVABLE.format(reason=error),
            ) from error

    def _material(
        self, connection: sqlite3.Connection, workflow_id: str, workflow_version: str
    ) -> Mapping[str, object] | None:
        """The pinned release material off this version's own committed binding.

        Read through T-0688's verifying reader, so the bytes, their digest and length,
        their canonical form, the public contract and the Run and plan facts the binding
        names are all re-checked before a single member of it is believed.
        """
        row = connection.execute(
            _RUN_OF_VERSION, (self.workspace_id, workflow_id, workflow_version)
        ).fetchone()
        if row is None:
            return None
        stored = read_runtime_definition_binding(
            connection, workspace_id=self.workspace_id, run_id=str(row[0])
        )
        if stored is None:
            # A Legacy Run: durable before 0035, with no binding to restate. Nothing here
            # may invent one, so this version has no release to resolve.
            return None
        return {
            key: stored.binding[key]
            for key in _RELEASE_MATERIAL_FIELDS
            if key in stored.binding
        }


def _materialised(plan: SealedWorkflowPlan) -> MaterialisedWorkflow:
    """The sealed plan, rebuilt as the `MaterialisedWorkflow` it was sealed from.

    Every hash is the stored one rather than a recomputed one, and then verified:
    `MaterialisedWorkflow` re-addresses each step out of its own canonical preimage on
    construction, and the plan itself is verified here, so a row edited outside this
    database's guards raises instead of resolving.
    """
    rebuilt = MaterialisedWorkflow(
        workflow_id=plan.workflow_id,
        version=plan.workflow_version,
        definition_hash=plan.definition_hash,
        steps=tuple(_step(step) for step in plan.steps),
        content_hash=plan.plan_hash,
    )
    rebuilt.verify_content_hash()
    return rebuilt


def _step(step: StoredPlanStep) -> MaterialisedStep:
    return MaterialisedStep(
        step_id=step.step_id,
        component_id=step.component_id,
        component_version=step.component_version,
        execution_class=step.execution_class,
        sequence_index=step.sequence_index,
        depends_on=step.depends_on,
        definition_hash=step.step_definition_hash,
        branch=None if step.branch is None else _branch(step.branch),
        loop=None if step.loop is None else _loop(step.loop),
        child_workflow=(
            None if step.child_workflow is None else _child(step.child_workflow)
        ),
        content_hash=step.materialised_step_hash,
    )


def _branch(preimage: Mapping[str, object]) -> BranchDefinition:
    expected = preimage["expected_value"]
    return BranchDefinition(
        input_key=str(preimage["input_key"]),
        operator=str(preimage["operator"]),
        expected_value=None if expected is None else str(expected),
    )


def _loop(preimage: Mapping[str, object]) -> LoopDefinition:
    return LoopDefinition(
        max_iterations=_integer(preimage["max_iterations"]),
        per_iteration_budget=_integer(preimage["per_iteration_budget"]),
        total_budget=_integer(preimage["total_budget"]),
    )


def _child(preimage: Mapping[str, object]) -> ChildWorkflowDefinition:
    return ChildWorkflowDefinition(
        workflow_id=str(preimage["workflow_id"]),
        version=str(preimage["version"]),
        workflow_hash=str(preimage["workflow_hash"]),
        budget=_integer(preimage["budget"]),
    )


def _integer(value: object) -> int:
    """One stored count, as the integer it has to already be.

    `int(value)` would accept `"3"` and `3.7` and quietly produce a different plan than
    the one that was sealed, which the content hash would then refuse in a place that
    cannot say why.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"a stored plan step holds {value!r} where an integer belongs")
    return value


__all__ = ["WorkspaceWorkflowReleaseAuthority"]
