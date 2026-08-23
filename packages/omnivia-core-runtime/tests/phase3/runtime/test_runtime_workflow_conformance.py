"""Conformance oracles for the EP2 workflow seam.

The seam is a provisional in-memory oracle holding no database, scheduler,
recovery, transport or Platform handle, so every test here is built from frozen
values and recording callables. What is asserted is the seam's own contract:

- a workflow's identity and its materialised plan depend on the dependency
  graph and nothing else -- two authorings that declare the same steps in a
  different order seal to the same hash and materialise to the same order;
- an unknown dependency, a self dependency and a cycle are refused eagerly at
  definition, not deferred to materialisation;
- routing answers exactly one of five routes -- ``AGENT``, ``DETERMINISTIC``,
  ``EFFECT``, ``WAIT``, ``CHILD_WORKFLOW`` -- and an ``EFFECT`` route carries a
  ``CapabilityProposal`` as inert data the router never dispatches;
- the deterministic implementation registry is exact-identity, exact-version,
  exact-component, trust-aware, isolation-aware and fail closed, and its history
  outlives replacement and removal;
- a branch answers ``MATCHED``/``UNMATCHED``/``BLOCKED`` and never falls back to
  a default on unavailable or invalid input;
- a loop refuses an iteration once its cap or budget would be exhausted, and
  refuses before any spend is recorded;
- child correlation is fenced per parent step, and a stale, wrong, late or
  over-budget result is refused;
- completion requires both a configured outcome and every evidence kind the rule
  names;
- one run's plan and branch observations are replay-equivalent, reconstructible
  from the sealed definition alone, and refused once the run is cancelled.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest
from omnivia_core_runtime.execution import (
    BRANCH_BLOCKED,
    BRANCH_MATCHED,
    BRANCH_OPERATOR_EQUALS,
    BRANCH_OPERATOR_NOT_EQUALS,
    BRANCH_OPERATOR_PRESENT,
    BRANCH_OPERATORS,
    BRANCH_OUTCOMES,
    BRANCH_UNMATCHED,
    BUILD_TRUST_APPROVED,
    BUILD_TRUST_DISABLED,
    BUILD_TRUST_PROTOTYPE,
    BUILD_TRUST_QUARANTINED,
    CANCELLATION_COOPERATIVE,
    CANCELLATION_NONE,
    CANCELLATION_POSTURES,
    EXECUTION_CLASS_AGENT,
    EXECUTION_CLASS_DETERMINISTIC,
    EXECUTION_CLASS_EFFECT,
    EXECUTION_CLASS_WAIT,
    EXECUTION_CLASSES,
    IMPLEMENTATION_POSTURE_PROPOSES_EFFECT,
    IMPLEMENTATION_POSTURE_PURE,
    IMPLEMENTATION_POSTURES,
    OUTCOME_FAILED,
    OUTCOME_SUCCEEDED,
    OUTCOMES,
    ROUTE_AGENT,
    ROUTE_CHILD_WORKFLOW,
    ROUTE_DETERMINISTIC,
    ROUTE_EFFECT,
    ROUTE_WAIT,
    ROUTES,
    BranchDefinition,
    CapabilityProposal,
    ChildWorkflowCorrelator,
    ChildWorkflowDefinition,
    CompletionEvaluator,
    CompletionRule,
    DeterministicImplementationDescriptor,
    DeterministicImplementationRegistry,
    ExecutionContractError,
    ExecutionRefused,
    LoopController,
    LoopDefinition,
    MaterialisedWorkflow,
    RunOracle,
    StepDefinition,
    StepRouter,
    WorkflowDefinition,
    materialise_workflow,
)
from omnivia_core_runtime.execution import workflow as workflow_module
from omnivia_core_runtime.execution.registry import (
    EVENT_REGISTERED,
    EVENT_REMOVED,
    EVENT_REPLACED,
)

DIGEST = "sha256:" + "1" * 64
OTHER_DIGEST = "sha256:" + "2" * 64
BUILD = "sha256:" + "3" * 64
OTHER_BUILD = "sha256:" + "4" * 64
CHILD_HASH = "sha256:" + "7" * 64
OTHER_CHILD_HASH = "sha256:" + "8" * 64


def step(
    step_id: str,
    execution_class: str = EXECUTION_CLASS_DETERMINISTIC,
    **overrides: object,
) -> StepDefinition:
    fields: dict[str, object] = {
        "step_id": step_id,
        "component_id": "component.echo",
        "component_version": "1.0.0",
        "execution_class": execution_class,
    }
    fields.update(overrides)
    return StepDefinition(**fields).sealed()  # type: ignore[arg-type]


def workflow(*steps: StepDefinition, **overrides: object) -> WorkflowDefinition:
    fields: dict[str, object] = {
        "workflow_id": "workflow.review",
        "version": "1.0.0",
        "steps": steps,
    }
    fields.update(overrides)
    return WorkflowDefinition(**fields).sealed()  # type: ignore[arg-type]


def child() -> ChildWorkflowDefinition:
    return ChildWorkflowDefinition(
        workflow_id="workflow.summarise",
        version="1.0.0",
        workflow_hash=CHILD_HASH,
        budget=10,
    )


def implementation(**overrides: object) -> DeterministicImplementationDescriptor:
    fields: dict[str, object] = {
        "implementation_id": "impl.echo",
        "version": "1.0.0",
        "build_hash": BUILD,
        "component_id": "component.echo",
        "component_version": "1.0.0",
        "input_schema_ref": DIGEST,
        "output_schema_ref": OTHER_DIGEST,
        "max_input_bytes": 4_096,
        "max_output_bytes": 4_096,
        "required_isolation": 2,
        "posture": IMPLEMENTATION_POSTURE_PURE,
        "cancellation": CANCELLATION_COOPERATIVE,
        "evidence_kinds": ("component.output",),
        "platforms": ("darwin", "linux"),
        "trust_state": BUILD_TRUST_APPROVED,
    }
    fields.update(overrides)
    return DeterministicImplementationDescriptor(**fields).sealed()  # type: ignore[arg-type]


#: The mixed golden workflow: one step of every route, and a dependency graph
#: whose topological order is not the order the steps are declared in below.
GOLDEN_STEPS = (
    step("d.wait", EXECUTION_CLASS_WAIT, depends_on=("c.write",)),
    step("b.compute", EXECUTION_CLASS_DETERMINISTIC, depends_on=("a.plan",)),
    step(
        "e.child",
        EXECUTION_CLASS_WAIT,
        depends_on=("b.compute",),
        child_workflow=child(),
    ),
    step("a.plan", EXECUTION_CLASS_AGENT),
    step("c.write", EXECUTION_CLASS_EFFECT, depends_on=("b.compute",)),
)

GOLDEN_ORDER = ("a.plan", "b.compute", "c.write", "d.wait", "e.child")

GOLDEN_ROUTES = (
    ROUTE_AGENT,
    ROUTE_DETERMINISTIC,
    ROUTE_EFFECT,
    ROUTE_WAIT,
    ROUTE_CHILD_WORKFLOW,
)


# --------------------------------------------------------------------------
# The route vocabulary: the four execution classes plus exactly one more
# --------------------------------------------------------------------------


def test_the_five_routes_are_the_four_execution_classes_and_child_workflow() -> None:
    assert ROUTES == {"AGENT", "DETERMINISTIC", "EFFECT", "WAIT", "CHILD_WORKFLOW"}
    assert EXECUTION_CLASSES < ROUTES
    assert ROUTES - EXECUTION_CLASSES == {ROUTE_CHILD_WORKFLOW}
    # Routing never invents a second spelling of an execution class.
    assert (ROUTE_AGENT, ROUTE_DETERMINISTIC, ROUTE_EFFECT, ROUTE_WAIT) == (
        EXECUTION_CLASS_AGENT,
        EXECUTION_CLASS_DETERMINISTIC,
        EXECUTION_CLASS_EFFECT,
        EXECUTION_CLASS_WAIT,
    )
    assert BRANCH_OPERATORS == {"EQUALS", "NOT_EQUALS", "PRESENT"}
    assert BRANCH_OUTCOMES == {"MATCHED", "UNMATCHED", "BLOCKED"}
    assert IMPLEMENTATION_POSTURES == {"PURE", "PROPOSES_EFFECT"}
    assert CANCELLATION_POSTURES == {"NONE", "COOPERATIVE"}
    assert OUTCOMES == {"SUCCEEDED", "FAILED"}


# --------------------------------------------------------------------------
# Identity and materialisation: a function of the graph, never of the authoring
# --------------------------------------------------------------------------


def test_declaration_order_changes_neither_the_hash_nor_the_plan() -> None:
    declared = workflow(*GOLDEN_STEPS)
    reversed_authoring = workflow(*reversed(GOLDEN_STEPS))
    rotated = workflow(*(GOLDEN_STEPS[2:] + GOLDEN_STEPS[:2]))

    assert declared.content_hash == reversed_authoring.content_hash == rotated.content_hash

    plans = [materialise_workflow(one) for one in (declared, reversed_authoring, rotated)]
    assert {plan.content_hash for plan in plans} == {plans[0].content_hash}
    for plan in plans:
        assert tuple(materialised.step_id for materialised in plan.steps) == GOLDEN_ORDER
        assert tuple(materialised.sequence_index for materialised in plan.steps) == (
            0,
            1,
            2,
            3,
            4,
        )


def test_materialisation_is_a_pure_function_of_the_sealed_definition() -> None:
    definition = workflow(*GOLDEN_STEPS)

    assert materialise_workflow(definition) == materialise_workflow(definition)
    assert materialise_workflow(definition).definition_hash == definition.content_hash


def test_a_material_change_seals_to_a_different_hash() -> None:
    baseline = workflow(*GOLDEN_STEPS)

    assert workflow(*GOLDEN_STEPS, version="1.0.1").content_hash != baseline.content_hash
    assert (
        workflow(*GOLDEN_STEPS, workflow_id="workflow.other").content_hash
        != baseline.content_hash
    )
    # A step's optional shapes are hashed, not decoration hung off the side.
    assert (
        step("a.plan", loop=LoopDefinition(3, 5, 15)).content_hash
        != step("a.plan").content_hash
    )
    assert (
        step(
            "a.plan",
            branch=BranchDefinition("mode", BRANCH_OPERATOR_EQUALS, "fast"),
        ).content_hash
        != step("a.plan").content_hash
    )
    assert (
        step("e.child", EXECUTION_CLASS_WAIT, child_workflow=child()).content_hash
        != step("e.child", EXECUTION_CLASS_WAIT).content_hash
    )


def test_an_edited_definition_no_longer_materialises() -> None:
    tampered = replace(workflow(*GOLDEN_STEPS), version="9.9.9")

    with pytest.raises(ExecutionContractError) as caught:
        materialise_workflow(tampered)
    assert caught.value.reason == "content_hash_mismatch"


def test_an_unsealed_step_is_refused_before_the_workflow_is_built() -> None:
    unsealed = StepDefinition(
        step_id="a.plan",
        component_id="component.echo",
        component_version="1.0.0",
        execution_class=EXECUTION_CLASS_AGENT,
    )

    assert not unsealed.is_sealed
    with pytest.raises(ExecutionContractError) as caught:
        workflow(unsealed)
    assert caught.value.reason == "unsealed_descriptor"


def test_an_unsealed_workflow_is_refused_before_it_is_materialised() -> None:
    unsealed = WorkflowDefinition(
        workflow_id="workflow.review",
        version="1.0.0",
        steps=(step("a.plan"),),
    )

    with pytest.raises(ExecutionContractError) as caught:
        materialise_workflow(unsealed)
    assert caught.value.reason == "unsealed_descriptor"


# --------------------------------------------------------------------------
# Graph refusals: unknown dependency, self dependency, cycle
# --------------------------------------------------------------------------


def test_a_dependency_on_a_step_that_is_not_declared_is_refused() -> None:
    with pytest.raises(ExecutionContractError) as caught:
        workflow(step("a.plan"), step("b.compute", depends_on=("z.absent",)))
    assert caught.value.reason == "unknown_dependency"


def test_a_step_may_not_depend_on_itself() -> None:
    with pytest.raises(ExecutionContractError) as caught:
        step("a.plan", depends_on=("a.plan",))
    assert caught.value.reason == "self_dependency"


@pytest.mark.parametrize(
    "edges",
    [
        (("a.one", ("b.two",)), ("b.two", ("a.one",))),
        (
            ("a.one", ("c.three",)),
            ("b.two", ("a.one",)),
            ("c.three", ("b.two",)),
        ),
    ],
)
def test_a_dependency_cycle_is_refused_at_definition(
    edges: tuple[tuple[str, tuple[str, ...]], ...],
) -> None:
    """A cycle never reaches materialisation: the definition itself refuses it."""
    steps = tuple(step(step_id, depends_on=deps) for step_id, deps in edges)

    with pytest.raises(ExecutionContractError) as caught:
        workflow(*steps)
    assert caught.value.reason == "cyclic_dependency"


def test_a_workflow_declares_at_least_one_step_and_never_the_same_one_twice() -> None:
    with pytest.raises(ExecutionContractError) as empty:
        workflow()
    assert empty.value.reason == "empty_collection"

    with pytest.raises(ExecutionContractError) as duplicate:
        workflow(step("a.plan"), step("a.plan", EXECUTION_CLASS_AGENT))
    assert duplicate.value.reason == "duplicate_entry"


def test_a_child_workflow_step_must_declare_the_wait_execution_class() -> None:
    for wrong in (
        EXECUTION_CLASS_AGENT,
        EXECUTION_CLASS_DETERMINISTIC,
        EXECUTION_CLASS_EFFECT,
    ):
        with pytest.raises(ExecutionContractError) as caught:
            step("e.child", wrong, child_workflow=child())
        assert caught.value.reason == "incoherent_posture"

    assert step("e.child", EXECUTION_CLASS_WAIT, child_workflow=child()).is_sealed


# --------------------------------------------------------------------------
# Routing: five routes, and an EFFECT proposal that is only ever data
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("execution_class", "route"),
    [
        (EXECUTION_CLASS_AGENT, ROUTE_AGENT),
        (EXECUTION_CLASS_DETERMINISTIC, ROUTE_DETERMINISTIC),
        (EXECUTION_CLASS_EFFECT, ROUTE_EFFECT),
        (EXECUTION_CLASS_WAIT, ROUTE_WAIT),
    ],
)
def test_each_execution_class_routes_to_its_own_named_route(
    execution_class: str, route: str
) -> None:
    plan = materialise_workflow(workflow(step("a.one", execution_class)))

    decision = StepRouter().route(plan.steps[0])

    assert (decision.route, decision.step_id) == (route, "a.one")
    assert decision.effect_proposal is None


def test_a_child_workflow_step_routes_to_child_workflow_not_to_wait() -> None:
    plan = materialise_workflow(
        workflow(step("e.child", EXECUTION_CLASS_WAIT, child_workflow=child()))
    )

    decision = StepRouter().route(plan.steps[0])

    assert decision.route == ROUTE_CHILD_WORKFLOW
    assert plan.steps[0].execution_class == EXECUTION_CLASS_WAIT
    assert plan.steps[0].child_workflow == child()


def test_an_effect_route_carries_the_proposal_as_inert_data() -> None:
    proposal = CapabilityProposal("artifact.write", DIGEST)
    plan = materialise_workflow(workflow(step("c.write", EXECUTION_CLASS_EFFECT)))

    decision = StepRouter().route(plan.steps[0], effect_proposal=proposal)

    assert decision.route == ROUTE_EFFECT
    assert decision.effect_proposal is proposal
    # The router returned it and did nothing with it: there is no dispatch seam
    # on the router at all for it to have reached.
    assert not hasattr(StepRouter(), "dispatch")


@pytest.mark.parametrize(
    "declared",
    [
        step("a.plan", EXECUTION_CLASS_AGENT),
        step("b.compute", EXECUTION_CLASS_DETERMINISTIC),
        step("d.wait", EXECUTION_CLASS_WAIT),
        step("e.child", EXECUTION_CLASS_WAIT, child_workflow=child()),
    ],
)
def test_only_an_effect_step_may_carry_an_effect_proposal(
    declared: StepDefinition,
) -> None:
    plan = materialise_workflow(workflow(declared))

    with pytest.raises(ExecutionContractError) as caught:
        StepRouter().route(
            plan.steps[0], effect_proposal=CapabilityProposal("artifact.write", DIGEST)
        )
    assert caught.value.reason == "unexpected_effect_proposal"


def test_the_mixed_golden_workflow_routes_every_step_exactly_once() -> None:
    plan = materialise_workflow(workflow(*GOLDEN_STEPS))
    router = StepRouter()

    decisions = tuple(router.route(materialised) for materialised in plan.steps)

    assert tuple(decision.step_id for decision in decisions) == GOLDEN_ORDER
    assert tuple(decision.route for decision in decisions) == GOLDEN_ROUTES
    assert {decision.route for decision in decisions} == ROUTES
    assert all(decision.effect_proposal is None for decision in decisions)


# --------------------------------------------------------------------------
# Deterministic implementation registry: exact, fail closed, history-keeping
# --------------------------------------------------------------------------


def registered() -> DeterministicImplementationRegistry:
    registry = DeterministicImplementationRegistry()
    registry.register(implementation())
    return registry


def resolve(
    registry: DeterministicImplementationRegistry, **overrides: object
) -> DeterministicImplementationDescriptor:
    fields: dict[str, object] = {
        "component_id": "component.echo",
        "component_version": "1.0.0",
    }
    fields.update(overrides)
    return registry.resolve("impl.echo", "1.0.0", **fields)  # type: ignore[arg-type]


def test_resolution_needs_the_exact_implementation_identity_and_version() -> None:
    registry = registered()

    assert resolve(registry) == implementation()

    for wrong in (("impl.other", "1.0.0"), ("impl.echo", "1.0.1")):
        with pytest.raises(ExecutionRefused) as caught:
            registry.resolve(
                *wrong, component_id="component.echo", component_version="1.0.0"
            )
        assert caught.value.reason == "unknown_entry"


def test_resolution_serves_only_the_exact_component_it_claims() -> None:
    registry = registered()

    for wrong in (
        {"component_id": "component.other"},
        {"component_version": "1.0.1"},
    ):
        with pytest.raises(ExecutionRefused) as caught:
            resolve(registry, **wrong)
        assert caught.value.reason == "unsupported_component"


@pytest.mark.parametrize(
    "trust", [BUILD_TRUST_PROTOTYPE, BUILD_TRUST_QUARANTINED, BUILD_TRUST_DISABLED]
)
def test_only_an_approved_build_resolves(trust: str) -> None:
    registry = DeterministicImplementationRegistry()
    registry.register(implementation(trust_state=trust))

    with pytest.raises(ExecutionRefused) as caught:
        resolve(registry)
    assert caught.value.reason == "unsupported_trust"


def test_a_build_below_the_required_isolation_is_refused() -> None:
    registry = registered()

    assert resolve(registry, minimum_isolation=2).required_isolation == 2

    with pytest.raises(ExecutionRefused) as caught:
        resolve(registry, minimum_isolation=3)
    assert caught.value.reason == "insufficient_isolation"

    with pytest.raises(ExecutionContractError) as out_of_bound:
        resolve(registry, minimum_isolation=4)
    assert out_of_bound.value.reason == "invalid_isolation"


def test_a_pinned_identity_never_resolves_to_its_replacement() -> None:
    registry = DeterministicImplementationRegistry()
    original = implementation()
    registry.register(original)

    assert resolve(registry, expect_content_hash=original.content_hash) == original

    registry.register(implementation(build_hash=OTHER_BUILD))
    with pytest.raises(ExecutionRefused) as caught:
        resolve(registry, expect_content_hash=original.content_hash)
    assert caught.value.reason == "replaced_descriptor"

    # The successor still answers under its own identity.
    assert resolve(registry).build_hash == OTHER_BUILD


def test_a_removed_implementation_resolves_no_longer() -> None:
    registry = registered()

    assert registry.remove("impl.echo", "1.0.0") == implementation()
    assert registry.entries() == ()

    for call in (
        lambda: resolve(registry),
        lambda: registry.remove("impl.echo", "1.0.0"),
    ):
        with pytest.raises(ExecutionRefused) as caught:
            call()
        assert caught.value.reason == "unknown_entry"


def test_replacement_and_removal_retain_the_prior_build_identity() -> None:
    registry = DeterministicImplementationRegistry()
    original = implementation()
    successor = implementation(build_hash=OTHER_BUILD)

    registry.register(original)
    registry.register(successor)
    registry.remove("impl.echo", "1.0.0")

    assert [event.kind for event in registry.history] == [
        EVENT_REGISTERED,
        EVENT_REPLACED,
        EVENT_REGISTERED,
        EVENT_REMOVED,
    ]
    identities = [(event.content_hash, event.build_hash) for event in registry.history]
    assert identities[0] == (original.content_hash, BUILD)
    assert identities[1] == (original.content_hash, BUILD)
    assert identities[2] == identities[3] == (successor.content_hash, OTHER_BUILD)
    assert all(event.key == ("impl.echo", "1.0.0") for event in registry.history)


def test_the_registry_refuses_an_unsealed_or_edited_descriptor() -> None:
    registry = DeterministicImplementationRegistry()

    with pytest.raises(ExecutionContractError) as unsealed:
        registry.register(
            DeterministicImplementationDescriptor(
                implementation_id="impl.echo",
                version="1.0.0",
                build_hash=BUILD,
                component_id="component.echo",
                component_version="1.0.0",
                input_schema_ref=DIGEST,
                output_schema_ref=OTHER_DIGEST,
                max_input_bytes=4_096,
                max_output_bytes=4_096,
                required_isolation=2,
                posture=IMPLEMENTATION_POSTURE_PURE,
                cancellation=CANCELLATION_COOPERATIVE,
                platforms=("darwin",),
            )
        )
    assert unsealed.value.reason == "unsealed_descriptor"

    with pytest.raises(ExecutionContractError) as tampered:
        registry.register(replace(implementation(), component_id="component.other"))
    assert tampered.value.reason == "content_hash_mismatch"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"version": "1.0"}, "invalid_version"),
        ({"implementation_id": "Impl.Echo"}, "invalid_identifier"),
        ({"build_hash": "deadbeef"}, "invalid_digest"),
        ({"input_schema_ref": "sha256:nope"}, "invalid_digest"),
        ({"max_input_bytes": 0}, "invalid_limit"),
        ({"max_output_bytes": 16 * 1024 * 1024 + 1}, "invalid_limit"),
        ({"required_isolation": -1}, "invalid_isolation"),
        ({"posture": "pure"}, "unknown_vocabulary_member"),
        ({"cancellation": "FORCED"}, "unknown_vocabulary_member"),
        ({"trust_state": "approved"}, "unknown_vocabulary_member"),
        ({"platforms": ()}, "empty_collection"),
        ({"evidence_kinds": ("a.one", "a.one")}, "duplicate_entry"),
    ],
)
def test_implementation_validation_refuses_each_malformed_field(
    overrides: dict[str, object], reason: str
) -> None:
    with pytest.raises(ExecutionContractError) as caught:
        implementation(**overrides)
    assert caught.value.reason == reason


def test_the_declared_posture_is_a_claim_the_registry_carries_not_a_grant() -> None:
    """``PROPOSES_EFFECT`` is data on the descriptor; nothing here acts on it."""
    registry = DeterministicImplementationRegistry()
    registry.register(
        implementation(posture=IMPLEMENTATION_POSTURE_PROPOSES_EFFECT)
    )

    resolved = resolve(registry)

    assert resolved.posture == IMPLEMENTATION_POSTURE_PROPOSES_EFFECT
    assert resolved.cancellation == CANCELLATION_COOPERATIVE
    assert implementation(cancellation=CANCELLATION_NONE).cancellation == "NONE"


# --------------------------------------------------------------------------
# Branches: MATCHED, UNMATCHED, and an explicit BLOCKED that is never a default
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("operator", "expected", "inputs", "outcome", "reason"),
    [
        (BRANCH_OPERATOR_EQUALS, "fast", (("mode", "fast"),), BRANCH_MATCHED, "equals"),
        (
            BRANCH_OPERATOR_EQUALS,
            "fast",
            (("mode", "slow"),),
            BRANCH_UNMATCHED,
            "equals",
        ),
        (
            BRANCH_OPERATOR_NOT_EQUALS,
            "fast",
            (("mode", "slow"),),
            BRANCH_MATCHED,
            "not_equals",
        ),
        (
            BRANCH_OPERATOR_NOT_EQUALS,
            "fast",
            (("mode", "fast"),),
            BRANCH_UNMATCHED,
            "not_equals",
        ),
        (BRANCH_OPERATOR_PRESENT, None, (("mode", "fast"),), BRANCH_MATCHED, "present"),
        (
            BRANCH_OPERATOR_EQUALS,
            "fast",
            (("other", "fast"),),
            BRANCH_BLOCKED,
            "input_unavailable",
        ),
        (BRANCH_OPERATOR_PRESENT, None, (), BRANCH_BLOCKED, "input_unavailable"),
        (
            BRANCH_OPERATOR_EQUALS,
            "fast",
            (("mode", "FAST"),),
            BRANCH_BLOCKED,
            "input_invalid",
        ),
        (
            BRANCH_OPERATOR_PRESENT,
            None,
            (("mode", ""),),
            BRANCH_BLOCKED,
            "input_invalid",
        ),
    ],
)
def test_a_branch_answers_matched_unmatched_or_blocked(
    operator: str,
    expected: str | None,
    inputs: tuple[tuple[str, str], ...],
    outcome: str,
    reason: str,
) -> None:
    """Unavailable and invalid input are refused explicitly, never defaulted."""
    result = BranchDefinition("mode", operator, expected).evaluate(inputs)

    assert (result.outcome, result.reason) == (outcome, reason)


def test_a_blocked_branch_is_not_a_quietly_failed_one() -> None:
    """``PRESENT`` on a missing key is BLOCKED, not UNMATCHED: the gate abstains."""
    gate = BranchDefinition("mode", BRANCH_OPERATOR_PRESENT)

    assert gate.evaluate((("other", "fast"),)).outcome == BRANCH_BLOCKED
    assert gate.evaluate((("mode", "fast"),)).outcome == BRANCH_MATCHED
    # An invalid value never resolves to the "not equal, therefore matched" arm.
    not_equals = BranchDefinition("mode", BRANCH_OPERATOR_NOT_EQUALS, "fast")
    assert not_equals.evaluate((("mode", "FAST"),)).outcome == BRANCH_BLOCKED


@pytest.mark.parametrize(
    ("operator", "expected", "reason"),
    [
        (BRANCH_OPERATOR_PRESENT, "fast", "incoherent_branch"),
        (BRANCH_OPERATOR_EQUALS, None, "incoherent_branch"),
        (BRANCH_OPERATOR_NOT_EQUALS, None, "incoherent_branch"),
        (BRANCH_OPERATOR_EQUALS, "FAST", "invalid_identifier"),
        ("MATCHES", "fast", "unknown_vocabulary_member"),
        ("equals", "fast", "unknown_vocabulary_member"),
    ],
)
def test_an_incoherent_branch_is_not_constructible(
    operator: str, expected: str | None, reason: str
) -> None:
    with pytest.raises(ExecutionContractError) as caught:
        BranchDefinition("mode", operator, expected)
    assert caught.value.reason == reason


def test_a_branch_refuses_an_input_set_that_names_one_key_twice() -> None:
    gate = BranchDefinition("mode", BRANCH_OPERATOR_EQUALS, "fast")

    with pytest.raises(ExecutionContractError) as caught:
        gate.evaluate((("mode", "fast"), ("mode", "slow")))
    assert caught.value.reason == "duplicate_entry"


# --------------------------------------------------------------------------
# Loops: bounded iteration, bounded spend, and no partial spend on refusal
# --------------------------------------------------------------------------


def test_a_loop_admits_up_to_its_iteration_cap_and_then_refuses() -> None:
    controller = LoopController(LoopDefinition(3, 5, 100))

    assert [controller.admit(1) for _ in range(3)] == [1, 2, 3]
    assert (controller.iterations, controller.consumed_budget) == (3, 3)

    with pytest.raises(ExecutionRefused) as caught:
        controller.admit(1)
    assert caught.value.reason == "loop_exhausted"
    # Refused before it counted: the exhausted loop does not keep climbing.
    assert (controller.iterations, controller.consumed_budget) == (3, 3)


def test_a_loop_refuses_the_iteration_that_would_exhaust_its_total_budget() -> None:
    controller = LoopController(LoopDefinition(10, 4, 10))

    controller.admit(4)
    controller.admit(4)

    with pytest.raises(ExecutionRefused) as caught:
        controller.admit(4)
    assert caught.value.reason == "loop_exhausted"
    assert (controller.iterations, controller.consumed_budget) == (2, 8)

    # What still fits is still admitted: the budget is a bound, not a shutdown.
    assert controller.admit(2) == 3
    assert controller.consumed_budget == 10


@pytest.mark.parametrize("cost", [0, -1, 6])
def test_an_iteration_outside_the_per_iteration_budget_is_refused(cost: int) -> None:
    controller = LoopController(LoopDefinition(3, 5, 100))

    with pytest.raises(ExecutionRefused) as caught:
        controller.admit(cost)
    assert caught.value.reason == "iteration_budget_exceeded"
    assert (controller.iterations, controller.consumed_budget) == (0, 0)


@pytest.mark.parametrize(
    "bounds",
    [(0, 5, 100), (-1, 5, 100), (10_001, 5, 100), (3, 0, 100), (3, 5, 4)],
)
def test_an_unbounded_or_incoherent_loop_is_not_constructible(
    bounds: tuple[int, int, int],
) -> None:
    with pytest.raises(ExecutionContractError) as caught:
        LoopDefinition(*bounds)
    assert caught.value.reason == "invalid_loop_bound"


# --------------------------------------------------------------------------
# Child correlation: fenced per parent step, stale/wrong/late refused
# --------------------------------------------------------------------------


def accept(
    correlator: ChildWorkflowCorrelator, fence: int, cost: int = 1, **overrides: object
) -> object:
    fields: dict[str, object] = {
        "child_workflow_id": "workflow.summarise",
        "child_version": "1.0.0",
        "child_workflow_hash": CHILD_HASH,
        "fence": fence,
        "cost": cost,
    }
    fields.update(overrides)
    return correlator.accept("run-0001", "e.child", **fields)  # type: ignore[arg-type]


def test_an_opened_correlation_accepts_the_result_it_was_opened_for() -> None:
    correlator = ChildWorkflowCorrelator()
    correlation = correlator.open("run-0001", "e.child", child())

    assert (correlation.fence, correlation.budget, correlation.consumed) == (1, 10, 0)

    updated = accept(correlator, fence=1, cost=4)

    assert updated.consumed == 4  # type: ignore[attr-defined]
    assert accept(correlator, fence=1, cost=6).consumed == 10  # type: ignore[attr-defined]


def test_a_result_that_names_another_child_is_refused_as_wrong() -> None:
    correlator = ChildWorkflowCorrelator()
    correlator.open("run-0001", "e.child", child())

    for wrong in (
        {"child_workflow_id": "workflow.other"},
        {"child_version": "1.0.1"},
        {"child_workflow_hash": OTHER_CHILD_HASH},
    ):
        with pytest.raises(ExecutionRefused) as caught:
            accept(correlator, fence=1, **wrong)
        assert caught.value.reason == "wrong_child"


def test_a_result_behind_or_ahead_of_the_fence_is_refused() -> None:
    correlator = ChildWorkflowCorrelator()
    correlator.open("run-0001", "e.child", child())
    correlator.open("run-0001", "e.child", child())

    with pytest.raises(ExecutionRefused) as stale:
        accept(correlator, fence=1)
    assert stale.value.reason == "stale_fence"

    with pytest.raises(ExecutionRefused) as ahead:
        accept(correlator, fence=3)
    assert ahead.value.reason == "unknown_fence"

    assert accept(correlator, fence=2).consumed == 1  # type: ignore[attr-defined]


def test_a_result_against_a_closed_correlation_is_refused_as_late() -> None:
    correlator = ChildWorkflowCorrelator()
    correlator.open("run-0001", "e.child", child())
    assert correlator.close("run-0001", "e.child").fence == 1

    for call in (
        lambda: accept(correlator, fence=1),
        lambda: correlator.close("run-0001", "e.child"),
    ):
        with pytest.raises(ExecutionRefused) as caught:
            call()
        assert caught.value.reason == "unknown_correlation"


def test_reopening_a_closed_correlation_keeps_the_late_result_stale() -> None:
    """The fence line is monotonic per parent step, not per open correlation.

    Otherwise a result from the closed epoch would carry fence 1 into a reopened
    correlation that also starts at 1, and a late result would be indistinguish-
    able from a live one.
    """
    correlator = ChildWorkflowCorrelator()
    correlator.open("run-0001", "e.child", child())
    correlator.close("run-0001", "e.child")

    reopened = correlator.open("run-0001", "e.child", child())

    assert reopened.fence == 2
    with pytest.raises(ExecutionRefused) as caught:
        accept(correlator, fence=1)
    assert caught.value.reason == "stale_fence"
    assert accept(correlator, fence=2).consumed == 1  # type: ignore[attr-defined]


def test_a_result_exceeding_the_correlation_budget_is_refused() -> None:
    correlator = ChildWorkflowCorrelator()
    correlator.open("run-0001", "e.child", child())
    accept(correlator, fence=1, cost=9)

    with pytest.raises(ExecutionRefused) as caught:
        accept(correlator, fence=1, cost=2)
    assert caught.value.reason == "budget_exceeded"

    with pytest.raises(ExecutionContractError) as invalid:
        accept(correlator, fence=1, cost=0)
    assert invalid.value.reason == "invalid_cost"

    # Neither refusal spent anything: the last admissible unit is still there.
    assert accept(correlator, fence=1, cost=1).consumed == 10  # type: ignore[attr-defined]


def test_correlations_are_keyed_by_the_exact_parent_run_and_step() -> None:
    correlator = ChildWorkflowCorrelator()
    correlator.open("run-0001", "e.child", child())

    for unknown in (("run-0002", "e.child"), ("run-0001", "f.other")):
        with pytest.raises(ExecutionRefused) as caught:
            correlator.accept(
                *unknown,
                child_workflow_id="workflow.summarise",
                child_version="1.0.0",
                child_workflow_hash=CHILD_HASH,
                fence=1,
                cost=1,
            )
        assert caught.value.reason == "unknown_correlation"

    # A second parent step keeps its own fence line.
    assert correlator.open("run-0001", "f.other", child()).fence == 1


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"workflow_id": "Workflow.Summarise"}, "invalid_identifier"),
        ({"version": "1.0"}, "invalid_version"),
        ({"workflow_hash": "sha256:nope"}, "invalid_digest"),
        ({"budget": 0}, "invalid_budget"),
    ],
)
def test_a_child_workflow_declaration_names_an_exact_target_and_budget(
    overrides: dict[str, object], reason: str
) -> None:
    fields: dict[str, object] = {
        "workflow_id": "workflow.summarise",
        "version": "1.0.0",
        "workflow_hash": CHILD_HASH,
        "budget": 10,
    }
    fields.update(overrides)

    with pytest.raises(ExecutionContractError) as caught:
        ChildWorkflowDefinition(**fields)  # type: ignore[arg-type]
    assert caught.value.reason == reason


# --------------------------------------------------------------------------
# Completion: a configured outcome and every evidence kind the rule names
# --------------------------------------------------------------------------


def test_completion_requires_both_the_outcome_and_its_evidence() -> None:
    evaluator = CompletionEvaluator(
        CompletionRule(
            required_outcomes=(OUTCOME_SUCCEEDED,),
            required_evidence_kinds=("component.output", "run.summary"),
        )
    )

    assert evaluator.evaluate(
        OUTCOME_SUCCEEDED, ("component.output", "run.summary", "extra.note")
    )

    with pytest.raises(ExecutionRefused) as outcome:
        evaluator.evaluate(OUTCOME_FAILED, ("component.output", "run.summary"))
    assert outcome.value.reason == "unconfigured_outcome"

    with pytest.raises(ExecutionRefused) as evidence:
        evaluator.evaluate(OUTCOME_SUCCEEDED, ("component.output",))
    assert evidence.value.reason == "missing_evidence"

    with pytest.raises(ExecutionRefused) as none_at_all:
        evaluator.evaluate(OUTCOME_SUCCEEDED, ())
    assert none_at_all.value.reason == "missing_evidence"


def test_a_completion_rule_configures_a_closed_outcome_and_named_evidence() -> None:
    both = CompletionEvaluator(
        CompletionRule((OUTCOME_SUCCEEDED, OUTCOME_FAILED), ("run.summary",))
    )

    assert both.evaluate(OUTCOME_FAILED, ("run.summary",))

    with pytest.raises(ExecutionContractError) as unknown:
        CompletionRule(("CANCELLED",), ("run.summary",))
    assert unknown.value.reason == "unknown_vocabulary_member"

    for empty in (
        lambda: CompletionRule((), ("run.summary",)),
        lambda: CompletionRule((OUTCOME_SUCCEEDED,), ()),
    ):
        with pytest.raises(ExecutionContractError) as caught:
            empty()
        assert caught.value.reason == "empty_collection"

    with pytest.raises(ExecutionContractError) as bad_outcome:
        both.evaluate("succeeded", ("run.summary",))
    assert bad_outcome.value.reason == "unknown_vocabulary_member"


# --------------------------------------------------------------------------
# Run oracle: replay-equivalent observations, restart, cancellation fence
# --------------------------------------------------------------------------


def test_the_plan_observation_records_every_step_once_and_replays_it() -> None:
    plan = materialise_workflow(workflow(*GOLDEN_STEPS))
    oracle = RunOracle("run-0001")

    first = oracle.observe_plan(plan)
    second = oracle.observe_plan(plan)

    assert tuple(observation.step_id for observation in first) == GOLDEN_ORDER
    assert tuple(observation.route for observation in first) == GOLDEN_ROUTES
    assert second == first
    assert tuple(observation.key for observation in second) == tuple(
        observation.key for observation in first
    )
    assert {observation.workflow_hash for observation in first} == {plan.content_hash}
    assert len(oracle.plan) == len(GOLDEN_ORDER)


def test_replanning_the_same_run_against_another_workflow_conflicts() -> None:
    oracle = RunOracle("run-0001")
    oracle.observe_plan(materialise_workflow(workflow(step("a.plan"))))

    with pytest.raises(ExecutionRefused) as caught:
        oracle.observe_plan(
            materialise_workflow(workflow(step("a.plan", EXECUTION_CLASS_AGENT)))
        )
    assert caught.value.reason == "plan_conflict"


def test_an_effect_route_yields_a_residual_proposal_that_is_never_dispatched() -> None:
    proposal = CapabilityProposal("artifact.write", DIGEST)
    plan = materialise_workflow(workflow(*GOLDEN_STEPS))
    oracle = RunOracle("run-0001")

    oracle.observe_plan(plan, effect_proposals=(("c.write", proposal),))

    assert oracle.residual_effect_proposals == (proposal,)
    # Replaying the same plan does not mint a second residual for one decision.
    oracle.observe_plan(plan, effect_proposals=(("c.write", proposal),))
    assert oracle.residual_effect_proposals == (proposal,)


def test_a_proposal_against_a_non_effect_step_is_refused_by_the_run() -> None:
    plan = materialise_workflow(workflow(*GOLDEN_STEPS))
    oracle = RunOracle("run-0001")

    with pytest.raises(ExecutionContractError) as wrong_step:
        oracle.observe_plan(
            plan,
            effect_proposals=(("a.plan", CapabilityProposal("artifact.write", DIGEST)),),
        )
    assert wrong_step.value.reason == "unexpected_effect_proposal"

    with pytest.raises(ExecutionContractError) as duplicate:
        oracle.observe_plan(
            plan,
            effect_proposals=(
                ("c.write", CapabilityProposal("artifact.write", DIGEST)),
                ("c.write", CapabilityProposal("artifact.write", OTHER_DIGEST)),
            ),
        )
    assert duplicate.value.reason == "duplicate_entry"
    assert oracle.residual_effect_proposals == ()


def test_a_branch_observation_is_recorded_once_and_replayed() -> None:
    gate = BranchDefinition("mode", BRANCH_OPERATOR_EQUALS, "fast")
    oracle = RunOracle("run-0001")

    first = oracle.observe_branch("b.compute", gate, (("mode", "fast"),))
    second = oracle.observe_branch("b.compute", gate, (("mode", "fast"),))

    assert (first.outcome, first.reason) == (BRANCH_MATCHED, "equals")
    assert second is first

    # The same step answering differently on a re-observation is the one thing a
    # replay-equivalent run may never do.
    with pytest.raises(ExecutionRefused) as caught:
        oracle.observe_branch("b.compute", gate, (("mode", "slow"),))
    assert caught.value.reason == "branch_conflict"


def test_a_blocked_branch_observation_is_recorded_as_blocked_not_dropped() -> None:
    gate = BranchDefinition("mode", BRANCH_OPERATOR_EQUALS, "fast")
    oracle = RunOracle("run-0001")

    observation = oracle.observe_branch("b.compute", gate, ())

    assert (observation.outcome, observation.reason) == (
        BRANCH_BLOCKED,
        "input_unavailable",
    )
    assert oracle.observe_branch("b.compute", gate, ()) is observation


def test_a_restarted_run_reconstructs_the_same_plan_from_the_sealed_definition() -> None:
    """Nothing but the sealed definition is needed to rebuild the run's plan."""
    definition = workflow(*GOLDEN_STEPS)
    gate = BranchDefinition("mode", BRANCH_OPERATOR_PRESENT)

    before = RunOracle("run-0001")
    original_plan = before.observe_plan(materialise_workflow(definition))
    original_branch = before.observe_branch("b.compute", gate, (("mode", "fast"),))

    # Restart: the process is gone, the sealed definition is all that survives.
    rebuilt = materialise_workflow(
        WorkflowDefinition(
            workflow_id=definition.workflow_id,
            version=definition.version,
            steps=definition.steps,
        ).sealed()
    )
    after = RunOracle("run-0001")
    replayed_plan = after.observe_plan(rebuilt)
    replayed_branch = after.observe_branch("b.compute", gate, (("mode", "fast"),))

    assert rebuilt == materialise_workflow(definition)
    assert [observation.key for observation in replayed_plan] == [
        observation.key for observation in original_plan
    ]
    assert replayed_branch.key == original_branch.key


def test_a_materialisation_is_the_restartable_unit_and_verifies_on_its_own() -> None:
    plan = materialise_workflow(workflow(*GOLDEN_STEPS))

    plan.verify_content_hash()
    for materialised in plan.steps:
        materialised.verify_content_hash()

    tampered = replace(plan, definition_hash=OTHER_DIGEST)
    with pytest.raises(ExecutionContractError) as caught:
        RunOracle("run-0001").observe_plan(tampered)
    assert caught.value.reason == "content_hash_mismatch"

    # An edited step no longer verifies inside a rebuilt materialisation either.
    with pytest.raises(ExecutionContractError) as edited:
        MaterialisedWorkflow(
            workflow_id=plan.workflow_id,
            version=plan.version,
            definition_hash=plan.definition_hash,
            steps=(replace(plan.steps[0], component_id="component.other"),),
        )
    assert edited.value.reason == "content_hash_mismatch"


def test_cancellation_rotates_the_fence_and_refuses_every_later_observation() -> None:
    plan = materialise_workflow(workflow(*GOLDEN_STEPS))
    gate = BranchDefinition("mode", BRANCH_OPERATOR_PRESENT)
    oracle = RunOracle("run-0001")
    oracle.observe_plan(plan)

    assert oracle.fence == 1
    assert not oracle.is_cancelled

    assert oracle.cancel() == 2
    assert oracle.is_cancelled

    for call in (
        lambda: oracle.observe_plan(plan),
        lambda: oracle.observe_branch("b.compute", gate, (("mode", "fast"),)),
    ):
        with pytest.raises(ExecutionRefused) as caught:
            call()
        assert caught.value.reason == "run_cancelled"

    # Cooperative, not destructive: what was already observed is still there.
    assert len(oracle.plan) == len(GOLDEN_ORDER)


def test_a_run_id_is_a_bounded_identifier() -> None:
    with pytest.raises(ExecutionContractError) as caught:
        RunOracle("Run-0001")
    assert caught.value.reason == "invalid_identifier"


# --------------------------------------------------------------------------
# The seam stays package-neutral
# --------------------------------------------------------------------------


def test_the_workflow_oracle_imports_no_storage_scheduler_recovery_or_platform() -> None:
    """An allowlist, not a denylist: a new coupling has to be added here first."""
    allowed = {
        "__future__",
        "dataclasses",
        "typing",
        "omnivia_core_runtime",
    }
    source = Path(workflow_module.__file__ or "")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.split(".")[0])

    assert imported <= allowed, imported - allowed
    text = source.read_text(encoding="utf-8")
    assert "omnivia_core_runtime.storage" not in text
    assert "omnivia_core_runtime.service" not in text


def test_the_oracle_is_still_documented_as_provisional_and_non_authoritative() -> None:
    """The "not a second runtime" statement is load-bearing and must not be dropped."""
    text = Path(workflow_module.__file__ or "").read_text(encoding="utf-8")

    assert "provisional in-memory EP2" in text
    assert "not a second runtime" in text
    assert "never writes canonical state" in text
