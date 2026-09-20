"""Runtime Execution Planes: the EP2 workflow materialisation and run oracle.

This is a provisional in-memory EP2 conformance oracle, not a second runtime.
It holds no database, scheduler, recovery, transport, filesystem or
Platform handle, and it never writes canonical state -- exactly the same
non-authoritative posture :mod:`profile`, :mod:`planes` and :mod:`registry`
already hold, extended to workflow shape instead of single-step execution.

Four frozen, content-addressed values carry workflow identity, reusing
:class:`~omnivia_core_runtime.execution.profile.ContentAddressed` rather than
inventing a second hashing rule: :class:`WorkflowDefinition` and
:class:`StepDefinition` are what an author declares; :class:`MaterialisedWorkflow`
and :class:`MaterialisedStep` are what :func:`materialise_workflow` deterministically
derives from them -- a topological step order that is the same regardless of the
order steps were declared in, so two authorings of the same graph seal to the
same hash and materialise to the same plan.

A step may optionally carry a :class:`BranchDefinition` (a tiny EQUALS /
NOT_EQUALS / PRESENT gate that returns an explicit ``BLOCKED`` result on
unavailable or invalid input rather than falling back to a default), a
:class:`LoopDefinition` with a :class:`LoopController` that refuses to run an
iteration once its bound or budget is exhausted, or a :class:`ChildWorkflowDefinition`
naming another workflow's exact id, version, hash and budget. :class:`StepRouter`
turns a materialised step into exactly one of the five reference routes --
``AGENT``, ``DETERMINISTIC``, ``EFFECT``, ``WAIT`` (the same four execution classes
:mod:`profile` already closes) or ``CHILD_WORKFLOW`` -- and an ``EFFECT`` route only
ever carries a :class:`~omnivia_core_runtime.execution.planes.CapabilityProposal`
as inert data; the router never dispatches it.

:class:`DeterministicImplementationRegistry` is the static, in-memory,
fail-closed registry for one more kind of build: a deterministic component
implementation, keyed by its own identity and version and resolved only
against the exact component it claims to implement. :class:`ChildWorkflowCorrelator`
is the in-memory parent/child correlation ledger: it fences every open child
run and rejects a stale, wrong or late result. :class:`CompletionEvaluator`
requires both a configured outcome and the evidence kinds a
:class:`CompletionRule` names. :class:`RunOracle` ties the rest together for one
run: replay-equivalent plan and branch observations, a cancellation fence, and
the residual :class:`~omnivia_core_runtime.execution.planes.CapabilityProposal`
values an ``EFFECT`` route yielded and this seam never dispatched.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Final

from omnivia_core_runtime.execution.planes import CapabilityProposal
from omnivia_core_runtime.execution.profile import (
    BUILD_TRUST_APPROVED,
    BUILD_TRUST_PROTOTYPE,
    BUILD_TRUST_STATES,
    EXECUTION_CLASS_AGENT,
    EXECUTION_CLASS_DETERMINISTIC,
    EXECUTION_CLASS_EFFECT,
    EXECUTION_CLASS_WAIT,
    EXECUTION_CLASSES,
    ISOLATION_MIN,
    ContentAddressed,
    ExecutionContractError,
    ExecutionRefused,
    derive_id,
    require_collection,
    require_digest,
    require_identifier,
    require_isolation,
    require_version,
    require_vocabulary,
)
from omnivia_core_runtime.execution.registry import (
    EVENT_REGISTERED,
    EVENT_REMOVED,
    EVENT_REPLACED,
)

_UNSEALED_CONTENT_HASH: Final = "sha256:" + "0" * 64
_MAX_LOOP_ITERATIONS: Final = 10_000
_MAX_PAYLOAD_BYTES: Final = 16 * 1024 * 1024

#: The five reference routes a materialised step resolves to. The first four are
#: exactly :data:`~omnivia_core_runtime.execution.profile.EXECUTION_CLASSES` --
#: routing never invents a second spelling of them -- and ``CHILD_WORKFLOW`` is
#: the one route no execution class names, because delegating to another
#: workflow is not a shape of work a profile itself runs.
ROUTE_AGENT: Final = EXECUTION_CLASS_AGENT
ROUTE_DETERMINISTIC: Final = EXECUTION_CLASS_DETERMINISTIC
ROUTE_EFFECT: Final = EXECUTION_CLASS_EFFECT
ROUTE_WAIT: Final = EXECUTION_CLASS_WAIT
ROUTE_CHILD_WORKFLOW: Final = "CHILD_WORKFLOW"

ROUTES: Final[frozenset[str]] = EXECUTION_CLASSES | {ROUTE_CHILD_WORKFLOW}

BRANCH_OPERATOR_EQUALS: Final = "EQUALS"
BRANCH_OPERATOR_NOT_EQUALS: Final = "NOT_EQUALS"
BRANCH_OPERATOR_PRESENT: Final = "PRESENT"

BRANCH_OPERATORS: Final[frozenset[str]] = frozenset(
    {BRANCH_OPERATOR_EQUALS, BRANCH_OPERATOR_NOT_EQUALS, BRANCH_OPERATOR_PRESENT}
)

#: ``BLOCKED`` is the explicit third answer: unavailable or invalid input never
#: falls back to ``MATCHED`` or ``UNMATCHED`` by default.
BRANCH_MATCHED: Final = "MATCHED"
BRANCH_UNMATCHED: Final = "UNMATCHED"
BRANCH_BLOCKED: Final = "BLOCKED"

BRANCH_OUTCOMES: Final[frozenset[str]] = frozenset(
    {BRANCH_MATCHED, BRANCH_UNMATCHED, BRANCH_BLOCKED}
)

IMPLEMENTATION_POSTURE_PURE: Final = "PURE"
IMPLEMENTATION_POSTURE_PROPOSES_EFFECT: Final = "PROPOSES_EFFECT"

IMPLEMENTATION_POSTURES: Final[frozenset[str]] = frozenset(
    {IMPLEMENTATION_POSTURE_PURE, IMPLEMENTATION_POSTURE_PROPOSES_EFFECT}
)

CANCELLATION_NONE: Final = "NONE"
CANCELLATION_COOPERATIVE: Final = "COOPERATIVE"

CANCELLATION_POSTURES: Final[frozenset[str]] = frozenset(
    {CANCELLATION_NONE, CANCELLATION_COOPERATIVE}
)

OUTCOME_SUCCEEDED: Final = "SUCCEEDED"
OUTCOME_FAILED: Final = "FAILED"

OUTCOMES: Final[frozenset[str]] = frozenset({OUTCOME_SUCCEEDED, OUTCOME_FAILED})


# --------------------------------------------------------------------------
# Branch: the EQUALS / NOT_EQUALS / PRESENT gate
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BranchResult:
    """One branch evaluation: an outcome and the reason it was reached."""

    outcome: str
    reason: str

    def __post_init__(self) -> None:
        require_vocabulary("outcome", self.outcome, BRANCH_OUTCOMES)
        require_identifier("reason", self.reason)


@dataclass(frozen=True, slots=True)
class BranchDefinition:
    """A tiny gate over one named input: ``EQUALS``, ``NOT_EQUALS`` or ``PRESENT``.

    ``evaluate`` never raises for unavailable or invalid input -- it returns an
    explicit :data:`BRANCH_BLOCKED` result instead, because a branch that could
    silently fall back to a default is a branch whose gate cannot be trusted.
    """

    input_key: str
    operator: str
    expected_value: str | None = None

    def __post_init__(self) -> None:
        require_identifier("input_key", self.input_key)
        require_vocabulary("operator", self.operator, BRANCH_OPERATORS)
        if self.operator == BRANCH_OPERATOR_PRESENT:
            if self.expected_value is not None:
                raise ExecutionContractError(
                    "incoherent_branch",
                    "PRESENT must not declare an expected value",
                )
        elif self.expected_value is None:
            raise ExecutionContractError(
                "incoherent_branch",
                f"{self.operator} requires an expected value",
            )
        else:
            require_identifier("expected_value", self.expected_value)

    @property
    def preimage(self) -> dict[str, object]:
        return {
            "input_key": self.input_key,
            "operator": self.operator,
            "expected_value": self.expected_value,
        }

    def evaluate(self, available_inputs: tuple[tuple[str, str], ...]) -> BranchResult:
        """Evaluate this gate against ``available_inputs``, never raising.

        Unavailable input (the key is not present) and invalid input (the value
        present is not a bounded identifier) are both refused explicitly as
        :data:`BRANCH_BLOCKED` -- the same outcome, distinguished only by reason,
        so a caller can never mistake either for a passed or failed gate.
        """
        names = tuple(name for name, _ in available_inputs)
        require_collection("available_inputs", names, required=False)
        values = dict(available_inputs)
        if self.input_key not in values:
            return BranchResult(BRANCH_BLOCKED, "input_unavailable")
        value = values[self.input_key]
        try:
            require_identifier("branch value", value)
        except ExecutionContractError:
            return BranchResult(BRANCH_BLOCKED, "input_invalid")
        if self.operator == BRANCH_OPERATOR_PRESENT:
            return BranchResult(BRANCH_MATCHED, "present")
        equal = value == self.expected_value
        matched = equal if self.operator == BRANCH_OPERATOR_EQUALS else not equal
        return BranchResult(
            BRANCH_MATCHED if matched else BRANCH_UNMATCHED, self.operator.lower()
        )


# --------------------------------------------------------------------------
# Loop: bounded iteration with a controller that refuses exhaustion
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LoopDefinition:
    """A bounded loop: a positive iteration cap, a per-iteration and total budget."""

    max_iterations: int
    per_iteration_budget: int
    total_budget: int

    def __post_init__(self) -> None:
        if not 0 < self.max_iterations <= _MAX_LOOP_ITERATIONS:
            raise ExecutionContractError(
                "invalid_loop_bound",
                f"max_iterations must be in 1..{_MAX_LOOP_ITERATIONS}",
            )
        if self.per_iteration_budget < 1:
            raise ExecutionContractError(
                "invalid_loop_bound", "per_iteration_budget must be positive"
            )
        if self.total_budget < self.per_iteration_budget:
            raise ExecutionContractError(
                "invalid_loop_bound",
                "total_budget must be at least one per_iteration_budget",
            )

    @property
    def preimage(self) -> dict[str, object]:
        return {
            "max_iterations": self.max_iterations,
            "per_iteration_budget": self.per_iteration_budget,
            "total_budget": self.total_budget,
        }


class LoopController:
    """Tracks one loop's iterations and spend, refusing exhaustion before it runs."""

    def __init__(self, definition: LoopDefinition) -> None:
        self.definition = definition
        self._iterations = 0
        self._consumed = 0

    @property
    def iterations(self) -> int:
        return self._iterations

    @property
    def consumed_budget(self) -> int:
        return self._consumed

    def admit(self, iteration_cost: int) -> int:
        """Admit one iteration costing ``iteration_cost``, or refuse exhaustion.

        Returns the 1-based iteration index on admission. Every refusal happens
        before the iteration is recorded, so a caller that catches
        :class:`~omnivia_core_runtime.execution.profile.ExecutionRefused` sees no
        partial spend.
        """
        if not 0 < iteration_cost <= self.definition.per_iteration_budget:
            raise ExecutionRefused(
                "iteration_budget_exceeded",
                "iteration cost exceeds the per-iteration budget",
            )
        if self._iterations >= self.definition.max_iterations:
            raise ExecutionRefused(
                "loop_exhausted", "loop has reached its maximum iterations"
            )
        if self._consumed + iteration_cost > self.definition.total_budget:
            raise ExecutionRefused(
                "loop_exhausted", "loop has reached its total budget"
            )
        self._iterations += 1
        self._consumed += iteration_cost
        return self._iterations


# --------------------------------------------------------------------------
# Child workflow: the exact id, version, hash and budget a step delegates to
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChildWorkflowDefinition:
    """The exact child workflow a ``WAIT`` step delegates to, and its budget."""

    workflow_id: str
    version: str
    workflow_hash: str
    budget: int

    def __post_init__(self) -> None:
        require_identifier("workflow_id", self.workflow_id)
        require_version("version", self.version)
        require_digest("workflow_hash", self.workflow_hash)
        if self.budget < 1:
            raise ExecutionContractError("invalid_budget", "budget must be positive")

    @property
    def preimage(self) -> dict[str, object]:
        return {
            "workflow_id": self.workflow_id,
            "version": self.version,
            "workflow_hash": self.workflow_hash,
            "budget": self.budget,
        }


def _optional_preimage(
    value: BranchDefinition | LoopDefinition | ChildWorkflowDefinition | None,
) -> dict[str, object] | None:
    return None if value is None else value.preimage


# --------------------------------------------------------------------------
# Workflow and step definitions: frozen, content-addressed, order-independent
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StepDefinition(ContentAddressed):
    """One declared step: a component, its dependencies, and its optional shapes.

    A step naming a :class:`ChildWorkflowDefinition` must declare the ``WAIT``
    execution class -- delegating to another workflow and then waiting on it is
    what ``WAIT`` already means, and giving child delegation a second spelling
    would let two steps disagree about which one they meant.
    """

    step_id: str
    component_id: str
    component_version: str
    execution_class: str
    depends_on: tuple[str, ...] = ()
    branch: BranchDefinition | None = None
    loop: LoopDefinition | None = None
    child_workflow: ChildWorkflowDefinition | None = None
    content_hash: str = _UNSEALED_CONTENT_HASH

    def __post_init__(self) -> None:
        require_identifier("step_id", self.step_id)
        require_identifier("component_id", self.component_id)
        require_version("component_version", self.component_version)
        require_vocabulary("execution_class", self.execution_class, EXECUTION_CLASSES)
        require_collection(
            f"step {self.step_id!r} depends_on", self.depends_on, required=False
        )
        if self.step_id in self.depends_on:
            raise ExecutionContractError(
                "self_dependency", f"step {self.step_id!r} must not depend on itself"
            )
        if (
            self.child_workflow is not None
            and self.execution_class != EXECUTION_CLASS_WAIT
        ):
            raise ExecutionContractError(
                "incoherent_posture",
                "a step with a child workflow must declare the WAIT execution class",
            )
        require_digest("content_hash", self.content_hash)

    def canonical_preimage(self) -> dict[str, object]:
        return {
            "step_id": self.step_id,
            "component_id": self.component_id,
            "component_version": self.component_version,
            "execution_class": self.execution_class,
            "depends_on": list(self.depends_on),
            "branch": _optional_preimage(self.branch),
            "loop": _optional_preimage(self.loop),
            "child_workflow": _optional_preimage(self.child_workflow),
        }


def _topological_order(steps: tuple[StepDefinition, ...]) -> tuple[str, ...]:
    """Return every step id in a deterministic order, refusing a dependency cycle.

    Kahn's algorithm with the ready set broken by lexicographic step id, so the
    order depends only on the graph -- never on the order ``steps`` was handed
    in -- and two authorings of the same graph materialise identically.
    """
    by_id = {step.step_id: step for step in steps}
    indegree = {step_id: 0 for step_id in by_id}
    dependents: dict[str, list[str]] = {step_id: [] for step_id in by_id}
    for step in steps:
        for dep in step.depends_on:
            if dep not in by_id:
                raise ExecutionContractError(
                    "unknown_dependency",
                    f"step {step.step_id!r} depends on unknown step {dep!r}",
                )
            indegree[step.step_id] += 1
            dependents[dep].append(step.step_id)
    ready = sorted(step_id for step_id, degree in indegree.items() if degree == 0)
    remaining = dict(indegree)
    order: list[str] = []
    while ready:
        ready.sort()
        current = ready.pop(0)
        order.append(current)
        for dependent in dependents[current]:
            remaining[dependent] -= 1
            if remaining[dependent] == 0:
                ready.append(dependent)
    if len(order) != len(by_id):
        raise ExecutionContractError(
            "cyclic_dependency", "workflow steps contain a dependency cycle"
        )
    return tuple(order)


@dataclass(frozen=True, slots=True)
class WorkflowDefinition(ContentAddressed):
    """A frozen, content-addressed workflow: its id, version and declared steps.

    Sealing is order-independent: the preimage sorts steps by id, so two
    authorings that declare the same steps in a different order seal to the
    same hash. Acyclic-ness and unknown dependencies are validated eagerly here,
    not deferred to materialisation.
    """

    workflow_id: str
    version: str
    steps: tuple[StepDefinition, ...]
    content_hash: str = _UNSEALED_CONTENT_HASH

    def __post_init__(self) -> None:
        require_identifier("workflow_id", self.workflow_id)
        require_version("version", self.version)
        step_ids = tuple(step.step_id for step in self.steps)
        require_collection("steps", step_ids, required=True)
        for step in self.steps:
            step.verify_content_hash()
        _topological_order(self.steps)
        require_digest("content_hash", self.content_hash)

    def canonical_preimage(self) -> dict[str, object]:
        ordered = sorted(self.steps, key=lambda step: step.step_id)
        return {
            "workflow_id": self.workflow_id,
            "version": self.version,
            "steps": [step.content_hash for step in ordered],
        }


@dataclass(frozen=True, slots=True)
class MaterialisedStep(ContentAddressed):
    """One step at its deterministic position in a materialised workflow."""

    step_id: str
    component_id: str
    component_version: str
    execution_class: str
    sequence_index: int
    depends_on: tuple[str, ...]
    definition_hash: str
    branch: BranchDefinition | None = None
    loop: LoopDefinition | None = None
    child_workflow: ChildWorkflowDefinition | None = None
    content_hash: str = _UNSEALED_CONTENT_HASH

    def __post_init__(self) -> None:
        require_identifier("step_id", self.step_id)
        require_identifier("component_id", self.component_id)
        require_version("component_version", self.component_version)
        require_vocabulary("execution_class", self.execution_class, EXECUTION_CLASSES)
        if self.sequence_index < 0:
            raise ExecutionContractError(
                "invalid_sequence", "sequence_index must not be negative"
            )
        require_collection(
            f"step {self.step_id!r} depends_on", self.depends_on, required=False
        )
        require_digest("definition_hash", self.definition_hash)
        if (
            self.child_workflow is not None
            and self.execution_class != EXECUTION_CLASS_WAIT
        ):
            raise ExecutionContractError(
                "incoherent_posture",
                "a step with a child workflow must declare the WAIT execution class",
            )
        require_digest("content_hash", self.content_hash)

    def canonical_preimage(self) -> dict[str, object]:
        return {
            "step_id": self.step_id,
            "component_id": self.component_id,
            "component_version": self.component_version,
            "execution_class": self.execution_class,
            "sequence_index": self.sequence_index,
            "depends_on": list(self.depends_on),
            "definition_hash": self.definition_hash,
            "branch": _optional_preimage(self.branch),
            "loop": _optional_preimage(self.loop),
            "child_workflow": _optional_preimage(self.child_workflow),
        }


@dataclass(frozen=True, slots=True)
class MaterialisedWorkflow(ContentAddressed):
    """The deterministic plan derived from one :class:`WorkflowDefinition`."""

    workflow_id: str
    version: str
    definition_hash: str
    steps: tuple[MaterialisedStep, ...]
    content_hash: str = _UNSEALED_CONTENT_HASH

    def __post_init__(self) -> None:
        require_identifier("workflow_id", self.workflow_id)
        require_version("version", self.version)
        require_digest("definition_hash", self.definition_hash)
        require_collection(
            "steps",
            tuple(step.step_id for step in self.steps),
            required=True,
        )
        for step in self.steps:
            step.verify_content_hash()
        require_digest("content_hash", self.content_hash)

    def canonical_preimage(self) -> dict[str, object]:
        return {
            "workflow_id": self.workflow_id,
            "version": self.version,
            "definition_hash": self.definition_hash,
            "steps": [step.content_hash for step in self.steps],
        }


def materialise_workflow(definition: WorkflowDefinition) -> MaterialisedWorkflow:
    """Deterministically derive a :class:`MaterialisedWorkflow` from ``definition``.

    ``definition`` and every one of its steps must already be sealed and
    verified -- materialisation never mutates or re-derives identity, it only
    orders and re-addresses what was already validated. The resulting order
    depends only on the dependency graph, so a reordered but otherwise
    identical ``definition`` materialises to the same plan.
    """
    definition.verify_content_hash()
    for step in definition.steps:
        step.verify_content_hash()
    order = _topological_order(definition.steps)
    by_id = {step.step_id: step for step in definition.steps}
    materialised_steps = tuple(
        MaterialisedStep(
            step_id=step_id,
            component_id=by_id[step_id].component_id,
            component_version=by_id[step_id].component_version,
            execution_class=by_id[step_id].execution_class,
            sequence_index=index,
            depends_on=by_id[step_id].depends_on,
            definition_hash=by_id[step_id].content_hash,
            branch=by_id[step_id].branch,
            loop=by_id[step_id].loop,
            child_workflow=by_id[step_id].child_workflow,
        ).sealed()
        for index, step_id in enumerate(order)
    )
    return MaterialisedWorkflow(
        workflow_id=definition.workflow_id,
        version=definition.version,
        definition_hash=definition.content_hash,
        steps=materialised_steps,
    ).sealed()


# --------------------------------------------------------------------------
# Step router: five routes, and an EFFECT route that only ever carries data
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """One routing decision: which route, for which step, carrying what data.

    ``effect_proposal`` is set only for an ``EFFECT`` route, and is inert data --
    :class:`StepRouter` never inspects, applies or dispatches it.
    """

    route: str
    step_id: str
    effect_proposal: CapabilityProposal | None = None


class StepRouter:
    """Turns one materialised step into exactly one of the five reference routes."""

    def route(
        self,
        step: MaterialisedStep,
        *,
        effect_proposal: CapabilityProposal | None = None,
    ) -> RouteDecision:
        if step.child_workflow is not None:
            if effect_proposal is not None:
                raise ExecutionContractError(
                    "unexpected_effect_proposal",
                    "only an EFFECT step may carry an effect proposal",
                )
            return RouteDecision(ROUTE_CHILD_WORKFLOW, step.step_id)
        if step.execution_class == EXECUTION_CLASS_EFFECT:
            return RouteDecision(
                ROUTE_EFFECT, step.step_id, effect_proposal=effect_proposal
            )
        if effect_proposal is not None:
            raise ExecutionContractError(
                "unexpected_effect_proposal",
                "only an EFFECT step may carry an effect proposal",
            )
        return RouteDecision(step.execution_class, step.step_id)


# --------------------------------------------------------------------------
# Deterministic implementation registry: identity, version, build, fail closed
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeterministicImplementationDescriptor(ContentAddressed):
    """One concrete build of a deterministic component implementation."""

    implementation_id: str
    version: str
    build_hash: str
    component_id: str
    component_version: str
    input_schema_ref: str
    output_schema_ref: str
    max_input_bytes: int
    max_output_bytes: int
    required_isolation: int
    posture: str
    cancellation: str
    evidence_kinds: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ()
    trust_state: str = BUILD_TRUST_PROTOTYPE
    content_hash: str = _UNSEALED_CONTENT_HASH

    def __post_init__(self) -> None:
        require_identifier("implementation_id", self.implementation_id)
        require_version("version", self.version)
        require_digest("build_hash", self.build_hash)
        require_identifier("component_id", self.component_id)
        require_version("component_version", self.component_version)
        require_digest("input_schema_ref", self.input_schema_ref)
        require_digest("output_schema_ref", self.output_schema_ref)
        if not 0 < self.max_input_bytes <= _MAX_PAYLOAD_BYTES:
            raise ExecutionContractError(
                "invalid_limit", "max_input_bytes is outside its bound"
            )
        if not 0 < self.max_output_bytes <= _MAX_PAYLOAD_BYTES:
            raise ExecutionContractError(
                "invalid_limit", "max_output_bytes is outside its bound"
            )
        require_isolation("required_isolation", self.required_isolation)
        require_vocabulary("posture", self.posture, IMPLEMENTATION_POSTURES)
        require_vocabulary("cancellation", self.cancellation, CANCELLATION_POSTURES)
        require_collection("evidence_kinds", self.evidence_kinds, required=False)
        require_collection("platforms", self.platforms, required=True)
        require_vocabulary("trust_state", self.trust_state, BUILD_TRUST_STATES)
        require_digest("content_hash", self.content_hash)

    @property
    def key(self) -> tuple[str, str]:
        return (self.implementation_id, self.version)


@dataclass(frozen=True, slots=True)
class ImplementationHistoryEvent:
    """One append-only registration, replacement or removal record."""

    kind: str
    key: tuple[str, str]
    content_hash: str
    build_hash: str


class DeterministicImplementationRegistry:
    """A static, in-memory, exact-version, fail-closed deterministic implementation registry.

    Resolution fails closed on any mismatch: an unknown key, a pinned identity
    that has since been replaced, a component or component version the
    implementation does not claim, a build trust below ``APPROVED``, or a build
    that runs below the isolation the caller requires.
    Replacement and removal keep the prior descriptor's identity in
    :attr:`history`, which outlives the entry itself.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], DeterministicImplementationDescriptor] = {}
        self._history: list[ImplementationHistoryEvent] = []

    @property
    def history(self) -> tuple[ImplementationHistoryEvent, ...]:
        return tuple(self._history)

    def entries(self) -> tuple[DeterministicImplementationDescriptor, ...]:
        return tuple(self._entries[key] for key in sorted(self._entries))

    def register(
        self, descriptor: DeterministicImplementationDescriptor
    ) -> DeterministicImplementationDescriptor:
        descriptor.verify_content_hash()
        key = descriptor.key
        previous = self._entries.get(key)
        if previous is not None:
            self._record(EVENT_REPLACED, previous)
        self._entries[key] = descriptor
        self._record(EVENT_REGISTERED, descriptor)
        return descriptor

    def remove(
        self, implementation_id: str, version: str
    ) -> DeterministicImplementationDescriptor:
        entry = self._require_entry(implementation_id, version)
        del self._entries[(implementation_id, version)]
        self._record(EVENT_REMOVED, entry)
        return entry

    def resolve(
        self,
        implementation_id: str,
        version: str,
        *,
        component_id: str,
        component_version: str,
        expect_content_hash: str | None = None,
        minimum_isolation: int = ISOLATION_MIN,
    ) -> DeterministicImplementationDescriptor:
        require_identifier("component_id", component_id)
        require_version("component_version", component_version)
        require_isolation("minimum_isolation", minimum_isolation)
        entry = self._require_entry(implementation_id, version)
        if (
            expect_content_hash is not None
            and entry.content_hash != expect_content_hash
        ):
            raise ExecutionRefused(
                "replaced_descriptor",
                "implementation no longer carries the pinned identity",
            )
        if (
            entry.component_id != component_id
            or entry.component_version != component_version
        ):
            raise ExecutionRefused(
                "unsupported_component",
                "implementation does not serve the requested component",
            )
        if entry.trust_state != BUILD_TRUST_APPROVED:
            raise ExecutionRefused(
                "unsupported_trust",
                f"implementation trust is {entry.trust_state!r}, "
                f"not {BUILD_TRUST_APPROVED}",
            )
        if entry.required_isolation < minimum_isolation:
            raise ExecutionRefused(
                "insufficient_isolation",
                f"implementation runs at isolation {entry.required_isolation}, "
                f"below the required {minimum_isolation}",
            )
        return entry

    def _require_entry(
        self, implementation_id: str, version: str
    ) -> DeterministicImplementationDescriptor:
        require_identifier("implementation_id", implementation_id)
        require_version("version", version)
        entry = self._entries.get((implementation_id, version))
        if entry is None:
            raise ExecutionRefused(
                "unknown_entry",
                f"no deterministic implementation registered as "
                f"{implementation_id}/{version}",
            )
        return entry

    def _record(
        self, kind: str, descriptor: DeterministicImplementationDescriptor
    ) -> None:
        self._history.append(
            ImplementationHistoryEvent(
                kind, descriptor.key, descriptor.content_hash, descriptor.build_hash
            )
        )


# --------------------------------------------------------------------------
# Child workflow correlation: fenced, budgeted, replay-safe
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChildCorrelation:
    """One open parent/child correlation: its fencing token and remaining budget."""

    parent_run_id: str
    parent_step_id: str
    child_workflow_id: str
    child_version: str
    child_workflow_hash: str
    fence: int
    budget: int
    consumed: int = 0


class ChildWorkflowCorrelator:
    """In-memory parent/child correlation, keyed by exact parent run and step.

    Every result is checked against the currently open correlation: a fence
    behind the current one is stale, a different child identity is wrong, and a
    result against a correlation already closed is late -- all three are
    refused, never silently accepted.

    The fence line is per parent step and monotonic across :meth:`close`, not
    per open correlation: a key that is closed and opened again resumes the
    count rather than restarting it, so a result minted against the closed
    epoch stays stale instead of landing on its successor.
    """

    def __init__(self) -> None:
        self._open: dict[tuple[str, str], ChildCorrelation] = {}
        self._fences: dict[tuple[str, str], int] = {}

    def open(
        self, parent_run_id: str, parent_step_id: str, child: ChildWorkflowDefinition
    ) -> ChildCorrelation:
        require_identifier("parent_run_id", parent_run_id)
        require_identifier("parent_step_id", parent_step_id)
        key = (parent_run_id, parent_step_id)
        fence = self._fences.get(key, 0) + 1
        self._fences[key] = fence
        correlation = ChildCorrelation(
            parent_run_id,
            parent_step_id,
            child.workflow_id,
            child.version,
            child.workflow_hash,
            fence,
            child.budget,
        )
        self._open[key] = correlation
        return correlation

    def accept(
        self,
        parent_run_id: str,
        parent_step_id: str,
        *,
        child_workflow_id: str,
        child_version: str,
        child_workflow_hash: str,
        fence: int,
        cost: int,
    ) -> ChildCorrelation:
        """Accept one child result, or refuse it as stale, wrong, late or over budget."""
        key = (parent_run_id, parent_step_id)
        current = self._open.get(key)
        if current is None:
            raise ExecutionRefused(
                "unknown_correlation", "no open correlation for this parent step"
            )
        if (child_workflow_id, child_version, child_workflow_hash) != (
            current.child_workflow_id,
            current.child_version,
            current.child_workflow_hash,
        ):
            raise ExecutionRefused(
                "wrong_child",
                "result does not name the child workflow that was opened",
            )
        if fence < current.fence:
            raise ExecutionRefused(
                "stale_fence", "child result was minted against a superseded fence"
            )
        if fence > current.fence:
            raise ExecutionRefused(
                "unknown_fence",
                "child result claims a fence this correlation has not reached",
            )
        if cost < 1:
            raise ExecutionContractError("invalid_cost", "cost must be positive")
        if current.consumed + cost > current.budget:
            raise ExecutionRefused(
                "budget_exceeded", "child result exceeds the correlation budget"
            )
        updated = replace(current, consumed=current.consumed + cost)
        self._open[key] = updated
        return updated

    def close(self, parent_run_id: str, parent_step_id: str) -> ChildCorrelation:
        """Close a correlation. A result accepted after this is late and refused."""
        key = (parent_run_id, parent_step_id)
        entry = self._open.pop(key, None)
        if entry is None:
            raise ExecutionRefused(
                "unknown_correlation", "no open correlation for this parent step"
            )
        return entry


# --------------------------------------------------------------------------
# Completion: requires both a configured outcome and its required evidence
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompletionRule:
    """The outcomes and evidence kinds a workflow's completion is configured to require."""

    required_outcomes: tuple[str, ...]
    required_evidence_kinds: tuple[str, ...]

    def __post_init__(self) -> None:
        require_collection(
            "required_outcomes",
            self.required_outcomes,
            required=True,
            allowed=OUTCOMES,
        )
        require_collection(
            "required_evidence_kinds", self.required_evidence_kinds, required=True
        )


class CompletionEvaluator:
    """Evaluates one outcome against a :class:`CompletionRule`, evidence and all."""

    def __init__(self, rule: CompletionRule) -> None:
        self.rule = rule

    def evaluate(self, outcome: str, evidence_kinds: tuple[str, ...]) -> bool:
        """Return ``True`` if ``outcome``/``evidence_kinds`` satisfy the rule, else refuse."""
        require_vocabulary("outcome", outcome, OUTCOMES)
        require_collection("evidence_kinds", evidence_kinds, required=False)
        if outcome not in self.rule.required_outcomes:
            raise ExecutionRefused(
                "unconfigured_outcome",
                f"outcome {outcome!r} is not a configured completion outcome",
            )
        missing = [
            kind
            for kind in self.rule.required_evidence_kinds
            if kind not in evidence_kinds
        ]
        if missing:
            raise ExecutionRefused(
                "missing_evidence", f"completion requires evidence kind {missing[0]!r}"
            )
        return True


# --------------------------------------------------------------------------
# Run oracle: replay-equivalent observations, cancellation fencing, residuals
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanObservation:
    """One recorded routing decision for one step of one run."""

    run_id: str
    workflow_hash: str
    step_id: str
    route: str
    sequence_index: int

    @property
    def key(self) -> str:
        return derive_id(
            "plan_observation",
            self.run_id,
            self.workflow_hash,
            self.step_id,
            self.route,
            str(self.sequence_index),
        )


@dataclass(frozen=True, slots=True)
class BranchObservation:
    """One recorded branch evaluation for one step of one run."""

    run_id: str
    step_id: str
    outcome: str
    reason: str

    @property
    def key(self) -> str:
        return derive_id(
            "branch_observation", self.run_id, self.step_id, self.outcome, self.reason
        )


class RunOracle:
    """A provisional in-memory EP2 conformance oracle for one workflow run.

    This is explicitly **not** canonical state, a scheduler, recovery or
    storage: it holds nothing but frozen observations, a cancellation fence and
    the residual effect proposals an ``EFFECT`` route yielded and this seam
    never dispatched. Observing the same plan or branch twice is replay-safe --
    it returns the prior observation rather than recording a conflicting one --
    and every observation is refused once the run has been cancelled.
    """

    def __init__(self, run_id: str) -> None:
        require_identifier("run_id", run_id)
        self.run_id = run_id
        self._router = StepRouter()
        self._plan: dict[str, PlanObservation] = {}
        self._branches: dict[str, BranchObservation] = {}
        self._residual_effects: list[CapabilityProposal] = []
        self._fence = 1
        self._cancelled = False

    @property
    def fence(self) -> int:
        return self._fence

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    @property
    def plan(self) -> tuple[PlanObservation, ...]:
        return tuple(self._plan[step_id] for step_id in sorted(self._plan))

    @property
    def residual_effect_proposals(self) -> tuple[CapabilityProposal, ...]:
        return tuple(self._residual_effects)

    def observe_plan(
        self,
        workflow: MaterialisedWorkflow,
        *,
        effect_proposals: tuple[tuple[str, CapabilityProposal], ...] = (),
    ) -> tuple[PlanObservation, ...]:
        """Record the route for every step, replaying a prior identical observation.

        ``effect_proposals`` pairs a step id with the proposal its ``EFFECT``
        route carries; it is recorded as a residual and never dispatched. A
        replayed step's proposal is not re-recorded, so residuals stay
        one-per-decision across repeated calls.
        """
        if self._cancelled:
            raise ExecutionRefused("run_cancelled", "run has been cancelled")
        workflow.verify_content_hash()
        proposal_by_step = dict(effect_proposals)
        if len(proposal_by_step) != len(effect_proposals):
            raise ExecutionContractError(
                "duplicate_entry", "effect_proposals names the same step twice"
            )
        observations: list[PlanObservation] = []
        for step in workflow.steps:
            recorded = self._plan.get(step.step_id)
            decision = self._router.route(
                step, effect_proposal=proposal_by_step.get(step.step_id)
            )
            observation = PlanObservation(
                self.run_id,
                workflow.content_hash,
                step.step_id,
                decision.route,
                step.sequence_index,
            )
            if recorded is not None:
                if recorded.key != observation.key:
                    raise ExecutionRefused(
                        "plan_conflict",
                        f"step {step.step_id!r} was already planned with a "
                        "different route",
                    )
                observations.append(recorded)
                continue
            self._plan[step.step_id] = observation
            observations.append(observation)
            if decision.route == ROUTE_EFFECT and decision.effect_proposal is not None:
                self._residual_effects.append(decision.effect_proposal)
        return tuple(observations)

    def observe_branch(
        self,
        step_id: str,
        branch: BranchDefinition,
        available_inputs: tuple[tuple[str, str], ...],
    ) -> BranchObservation:
        """Evaluate and record one branch, replaying a prior identical observation."""
        if self._cancelled:
            raise ExecutionRefused("run_cancelled", "run has been cancelled")
        require_identifier("step_id", step_id)
        result = branch.evaluate(available_inputs)
        observation = BranchObservation(
            self.run_id, step_id, result.outcome, result.reason
        )
        recorded = self._branches.get(step_id)
        if recorded is not None:
            if recorded.key != observation.key:
                raise ExecutionRefused(
                    "branch_conflict",
                    f"step {step_id!r} was already observed with a different "
                    "branch outcome",
                )
            return recorded
        self._branches[step_id] = observation
        return observation

    def cancel(self) -> int:
        """Cancel the run and rotate its fence, invalidating anything minted against it."""
        self._cancelled = True
        self._fence += 1
        return self._fence


__all__ = [
    "BRANCH_BLOCKED",
    "BRANCH_MATCHED",
    "BRANCH_OPERATORS",
    "BRANCH_OPERATOR_EQUALS",
    "BRANCH_OPERATOR_NOT_EQUALS",
    "BRANCH_OPERATOR_PRESENT",
    "BRANCH_OUTCOMES",
    "BRANCH_UNMATCHED",
    "CANCELLATION_COOPERATIVE",
    "CANCELLATION_NONE",
    "CANCELLATION_POSTURES",
    "IMPLEMENTATION_POSTURES",
    "IMPLEMENTATION_POSTURE_PROPOSES_EFFECT",
    "IMPLEMENTATION_POSTURE_PURE",
    "OUTCOMES",
    "OUTCOME_FAILED",
    "OUTCOME_SUCCEEDED",
    "ROUTES",
    "ROUTE_AGENT",
    "ROUTE_CHILD_WORKFLOW",
    "ROUTE_DETERMINISTIC",
    "ROUTE_EFFECT",
    "ROUTE_WAIT",
    "BranchDefinition",
    "BranchObservation",
    "BranchResult",
    "ChildCorrelation",
    "ChildWorkflowCorrelator",
    "ChildWorkflowDefinition",
    "CompletionEvaluator",
    "CompletionRule",
    "DeterministicImplementationDescriptor",
    "DeterministicImplementationRegistry",
    "ImplementationHistoryEvent",
    "LoopController",
    "LoopDefinition",
    "MaterialisedStep",
    "MaterialisedWorkflow",
    "PlanObservation",
    "RouteDecision",
    "RunOracle",
    "StepDefinition",
    "StepRouter",
    "WorkflowDefinition",
    "materialise_workflow",
]
