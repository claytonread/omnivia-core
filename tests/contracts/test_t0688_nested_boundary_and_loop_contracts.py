"""Clean-room tests for the T-0688 IP-09 Nested Workflow boundary and complete LoopPlan lane.

Covers `WEFT-BL-011` (REF-004 §D.3, DOC-004 §AD.7) and `WEFT-BL-012` (DOC-004 §AD.5,
REF-004 §D.4): boundary shape, the two-stage diagnose-first rollout, exact time-bounded
exceptions, every `LoopPlan` contradiction, and the complete iteration ledger.

Like the other T-0688 candidate records, none of these shapes carries `contractName` and none
is dispatched from `WORKFLOW_RECORD_VALIDATORS`, so they live here rather than in the
fixture-backed `test_workflow_hardening_contracts.py` suite.

Independently authored OmniVia examples throughout; no external source, schema, or test is
read or reused.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from omnivia_core.contracts.v1 import semantics_workflow as workflow
from omnivia_core.contracts.v1.compatibility import ContractSemanticError

_DIGEST = "sha256:" + "a" * 64
_OTHER_DIGEST = "sha256:" + "b" * 64

_NOW = "2026-09-01T12:00:00Z"
_EARLIER = "2026-08-01T12:00:00Z"
_LATER = "2026-10-01T12:00:00Z"


# --------------------------------------------------------------------------
# Nested Workflow boundary fixtures
# --------------------------------------------------------------------------


def _port(
    port_id: str = "port-t0688-tide-in", direction: str = "input", **overrides: Any
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "portId": port_id,
        "direction": direction,
        "semanticType": "tide-reading",
        "physicalSchema": {"schemaId": "schema-t0688-tide"},
        "cardinality": "single",
        "presence": "present",
        "classification": {"classificationId": "class-t0688-open"},
        "lineage": {"lineageId": "lineage-t0688-tide"},
    }
    record.update(overrides)
    return record


def _aggregation(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "policyOutcome": "satisfied",
        "evidenceOutcome": "satisfied",
        "completionOutcome": "satisfied",
        "reviewOutcome": "satisfied",
        "completionEvidence": {"evidenceId": "ev-t0688-harbour-completion"},
    }
    record.update(overrides)
    return record


def _crossing(
    reference_id: str = "ref-t0688-tide-1", **overrides: Any
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "referenceId": reference_id,
        "referenceKind": "connection",
        "fromStableId": "element-t0688-tide-gate",
        "toStableId": "element-t0688-harbour-gauge",
        "viaBoundaryPortId": "port-t0688-tide-in",
    }
    record.update(overrides)
    return record


def _bypass(
    reference_id: str = "ref-t0688-bypass-1", **overrides: Any
) -> dict[str, Any]:
    record = _crossing(reference_id, **overrides)
    record.pop("viaBoundaryPortId", None)
    return record


def _boundary(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "boundarySchemaVersion": "1.0.0",
        "parentWorkflowId": "wf-t0688-harbour-parent",
        "parentWorkflowVersion": "2.1.0",
        "childWorkflowStableId": "child-t0688-tide-gate",
        "boundaryPorts": [_port(), _port("port-t0688-tide-out", "output")],
        "childExternalReferences": [_crossing()],
        "aggregation": _aggregation(),
    }
    record.update(overrides)
    return record


def _exception(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "exceptionId": "exc-t0688-harbour-1",
        "workflowId": "wf-t0688-harbour-parent",
        "workflowVersion": "2.1.0",
        "expiresAt": _LATER,
        "decidingActor": {"principalId": "principal-t0688-harbourmaster"},
        "evidence": {"evidenceId": "ev-t0688-exception-1"},
    }
    record.update(overrides)
    return record


def _evaluate(boundary: dict[str, Any], **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "rollout_stage": "R0",
        "publication_posture": "new",
        "at_instant": _NOW,
    }
    kwargs.update(overrides)
    return workflow.evaluate_nested_workflow_boundary(boundary, **kwargs)


# --------------------------------------------------------------------------
# Boundary shape
# --------------------------------------------------------------------------


def test_nested_boundary_accepts_a_fully_declared_boundary() -> None:
    workflow.validate_nested_workflow_boundary(_boundary())


def test_nested_boundary_is_a_closed_shape() -> None:
    # There is no controller Component, proxy node or runtime interceptor at this boundary,
    # so a member naming one is refused as unknown rather than quietly accepted.
    with pytest.raises(
        ContractSemanticError, match=r"unknown fields \['controllerComponentId'\]"
    ):
        workflow.validate_nested_workflow_boundary(
            _boundary(controllerComponentId="component-t0688-mediator")
        )


@pytest.mark.parametrize(
    "field",
    sorted(_boundary()),
)
def test_nested_boundary_requires_every_member(field: str) -> None:
    record = _boundary()
    del record[field]
    with pytest.raises(ContractSemanticError):
        workflow.validate_nested_workflow_boundary(record)


def test_nested_boundary_requires_at_least_one_declared_port() -> None:
    with pytest.raises(ContractSemanticError, match="must not be empty"):
        workflow.validate_nested_workflow_boundary(_boundary(boundaryPorts=[]))


def test_nested_boundary_refuses_a_repeated_reference_id() -> None:
    record = _boundary(childExternalReferences=[_crossing(), _crossing()])
    with pytest.raises(ContractSemanticError, match="must not repeat a referenceId"):
        workflow.validate_nested_workflow_boundary(record)


@pytest.mark.parametrize("kind", ["shared_state", "ambient_value", "side_channel"])
def test_nested_boundary_refuses_a_side_channel_claiming_a_port(kind: str) -> None:
    # Shared state, an ambient value and a side channel are bypasses by construction: a claim
    # that one of them crossed a declared typed port is refused, not believed.
    record = _boundary(childExternalReferences=[_crossing(referenceKind=kind)])
    with pytest.raises(
        ContractSemanticError, match="crosses no declared typed boundary port"
    ):
        workflow.validate_nested_workflow_boundary(record)


def test_nested_boundary_treats_a_bypass_as_valid_shape_not_a_shape_error() -> None:
    # At R0 a bypass changes nothing at all, so shape validation must not refuse it.
    workflow.validate_nested_workflow_boundary(
        _boundary(childExternalReferences=[_bypass()])
    )


def test_nested_boundary_validation_does_not_mutate_the_record() -> None:
    record = _boundary(childExternalReferences=[_bypass(), _crossing()])
    before = copy.deepcopy(record)
    workflow.validate_nested_workflow_boundary(record)
    _evaluate(record, rollout_stage="R1")
    assert record == before


# --------------------------------------------------------------------------
# Boundary aggregation: policy, Evidence, completion, recursive Review
# --------------------------------------------------------------------------


def test_boundary_aggregation_requires_evidence_for_a_satisfied_completion() -> None:
    aggregation = _aggregation()
    del aggregation["completionEvidence"]
    with pytest.raises(ContractSemanticError, match="names completionEvidence"):
        workflow.validate_nested_workflow_boundary(_boundary(aggregation=aggregation))


def test_boundary_aggregation_refuses_completion_evidence_without_a_satisfied_completion() -> (
    None
):
    aggregation = _aggregation(completionOutcome="unsatisfied")
    with pytest.raises(
        ContractSemanticError, match="only valid for a satisfied completionOutcome"
    ):
        workflow.validate_nested_workflow_boundary(_boundary(aggregation=aggregation))


def test_boundary_aggregation_keeps_an_indeterminate_outcome_explicit() -> None:
    aggregation = _aggregation(reviewOutcome="indeterminate")
    with pytest.raises(ContractSemanticError, match="name indeterminateReason"):
        workflow.validate_nested_workflow_boundary(_boundary(aggregation=aggregation))

    aggregation["indeterminateReason"] = {"diagnosticId": "diag-t0688-review-pending"}
    workflow.validate_nested_workflow_boundary(_boundary(aggregation=aggregation))


def test_boundary_aggregation_refuses_an_indeterminate_reason_without_one() -> None:
    aggregation = _aggregation(indeterminateReason={"diagnosticId": "diag-t0688-none"})
    with pytest.raises(
        ContractSemanticError, match="only valid for an indeterminate outcome"
    ):
        workflow.validate_nested_workflow_boundary(_boundary(aggregation=aggregation))


@pytest.mark.parametrize(
    "dimension",
    ["policyOutcome", "evidenceOutcome", "completionOutcome", "reviewOutcome"],
)
def test_boundary_aggregation_preserves_an_indeterminate_descendant(
    dimension: str,
) -> None:
    descendant = _aggregation(
        **{dimension: "indeterminate"},
        indeterminateReason={"diagnosticId": "diag-t0688-child-pending"},
    )
    if dimension == "completionOutcome":
        del descendant["completionEvidence"]

    parent = _aggregation(descendantAggregations=[descendant])
    with pytest.raises(ContractSemanticError, match="must remain\n?\\s*indeterminate"):
        workflow.validate_nested_workflow_boundary(_boundary(aggregation=parent))

    # Composed explicitly, the same descendant is accepted.
    composed = _aggregation(
        **{dimension: "indeterminate"},
        indeterminateReason={"diagnosticId": "diag-t0688-child-pending"},
        descendantAggregations=[descendant],
    )
    if dimension == "completionOutcome":
        del composed["completionEvidence"]
    workflow.validate_nested_workflow_boundary(_boundary(aggregation=composed))


def test_boundary_aggregation_refuses_absorbing_an_unsatisfied_descendant() -> None:
    descendant = _aggregation(policyOutcome="unsatisfied")
    parent = _aggregation(descendantAggregations=[descendant])
    with pytest.raises(ContractSemanticError, match="may not aggregate as satisfied"):
        workflow.validate_nested_workflow_boundary(_boundary(aggregation=parent))


def test_boundary_aggregation_composes_recursively_through_descendants() -> None:
    grandchild = _aggregation(evidenceOutcome="unsatisfied")
    child = _aggregation(
        evidenceOutcome="unsatisfied", descendantAggregations=[grandchild]
    )
    parent = _aggregation(evidenceOutcome="unsatisfied", descendantAggregations=[child])
    workflow.validate_nested_workflow_boundary(_boundary(aggregation=parent))


def test_boundary_aggregation_refuses_unbounded_nesting() -> None:
    node = _aggregation()
    for _ in range(40):
        node = _aggregation(descendantAggregations=[node])
    with pytest.raises(ContractSemanticError, match="nest beyond the bound"):
        workflow.validate_nested_workflow_boundary(_boundary(aggregation=node))


# --------------------------------------------------------------------------
# Rollout: every stage x posture branch
# --------------------------------------------------------------------------


def test_rollout_stages_are_exactly_r0_and_r1() -> None:
    assert workflow.NESTED_BOUNDARY_ROLLOUT_STAGES == ("R0", "R1")
    with pytest.raises(ContractSemanticError, match="rollout_stage is not one of"):
        _evaluate(_boundary(), rollout_stage="R2")


@pytest.mark.parametrize("posture", ["new", "republished", "published_active"])
def test_r0_always_warns_and_never_blocks(posture: str) -> None:
    result = _evaluate(
        _boundary(childExternalReferences=[_bypass()]),
        rollout_stage="R0",
        publication_posture=posture,
    )
    assert result["publicationRefused"] is False
    assert [diagnostic["severity"] for diagnostic in result["diagnostics"]] == [
        "warning"
    ]


@pytest.mark.parametrize("posture", ["new", "republished"])
def test_r1_blocks_new_and_republished_publication(posture: str) -> None:
    result = _evaluate(
        _boundary(childExternalReferences=[_bypass()]),
        rollout_stage="R1",
        publication_posture=posture,
    )
    assert result["publicationRefused"] is True
    assert [diagnostic["severity"] for diagnostic in result["diagnostics"]] == [
        "blocker"
    ]


def test_r1_leaves_a_published_active_version_pinned() -> None:
    # REF-004 §D.3.5: an already published and active Version keeps its published posture, so
    # an active Run continues against its pinned Version.
    result = _evaluate(
        _boundary(childExternalReferences=[_bypass()]),
        rollout_stage="R1",
        publication_posture="published_active",
    )
    assert result["publicationRefused"] is False
    assert [diagnostic["severity"] for diagnostic in result["diagnostics"]] == [
        "warning"
    ]


@pytest.mark.parametrize("stage", ["R0", "R1"])
@pytest.mark.parametrize("posture", ["new", "republished", "published_active"])
def test_a_conforming_boundary_is_never_diagnosed_or_refused(
    stage: str, posture: str
) -> None:
    result = _evaluate(_boundary(), rollout_stage=stage, publication_posture=posture)
    assert result["diagnostics"] == ()
    assert result["publicationRefused"] is False


def test_a_reference_through_an_undeclared_port_is_a_bypass() -> None:
    record = _boundary(
        childExternalReferences=[_crossing(viaBoundaryPortId="port-t0688-undeclared")]
    )
    result = _evaluate(record, rollout_stage="R1")
    assert result["publicationRefused"] is True


def test_diagnostics_carry_the_exact_code_and_location_only() -> None:
    record = _boundary(childExternalReferences=[_bypass()])
    (diagnostic,) = _evaluate(record)["diagnostics"]
    assert dict(diagnostic) == {
        "code": "WF_NESTED_BOUNDARY_BYPASS",
        "severity": "warning",
        "parentWorkflowId": "wf-t0688-harbour-parent",
        "parentWorkflowVersion": "2.1.0",
        "childWorkflowStableId": "child-t0688-tide-gate",
        "referenceId": "ref-t0688-bypass-1",
        "referenceKind": "connection",
        "fromStableId": "element-t0688-tide-gate",
        "toStableId": "element-t0688-harbour-gauge",
    }
    assert workflow.WF_NESTED_BOUNDARY_BYPASS == "WF_NESTED_BOUNDARY_BYPASS"


def test_diagnostics_are_ordered_deterministically_by_reference_id() -> None:
    references = [
        _bypass("ref-t0688-zulu"),
        _bypass("ref-t0688-alpha"),
        _crossing("ref-t0688-mike"),
        _bypass("ref-t0688-november", referenceKind="shared_state"),
    ]
    result = _evaluate(_boundary(childExternalReferences=references))
    assert [diagnostic["referenceId"] for diagnostic in result["diagnostics"]] == [
        "ref-t0688-alpha",
        "ref-t0688-november",
        "ref-t0688-zulu",
    ]


def test_evaluation_refuses_a_malformed_instant() -> None:
    with pytest.raises(
        ContractSemanticError, match="at_instant is not a well-formed Timestamp"
    ):
        _evaluate(_boundary(), at_instant="the day after the storm")


# --------------------------------------------------------------------------
# Exact, time-bounded exceptions
# --------------------------------------------------------------------------


def test_exception_record_is_exact_attributed_and_time_bounded() -> None:
    workflow.validate_nested_boundary_exception(_exception())


@pytest.mark.parametrize(
    "field", ["workflowVersion", "expiresAt", "decidingActor", "evidence"]
)
def test_an_open_ended_or_unattributed_exception_is_not_an_exception(
    field: str,
) -> None:
    record = _exception()
    del record[field]
    with pytest.raises(ContractSemanticError, match=f"missing {field}"):
        workflow.validate_nested_boundary_exception(record)


def test_exception_record_is_a_closed_shape() -> None:
    with pytest.raises(
        ContractSemanticError, match=r"unknown fields \['workflowVersionRange'\]"
    ):
        workflow.validate_nested_boundary_exception(
            _exception(workflowVersionRange=">=2.0.0")
        )


def test_an_exact_unexpired_exception_suppresses_refusal_but_not_the_diagnostic() -> (
    None
):
    result = _evaluate(
        _boundary(childExternalReferences=[_bypass()]),
        rollout_stage="R1",
        exceptions=[_exception()],
    )
    assert result["publicationRefused"] is False
    assert [diagnostic["severity"] for diagnostic in result["diagnostics"]] == [
        "warning"
    ]
    assert result["diagnostics"][0]["code"] == "WF_NESTED_BOUNDARY_BYPASS"


@pytest.mark.parametrize(
    "override",
    [
        {"expiresAt": _EARLIER},
        {"expiresAt": _NOW},
        {"workflowVersion": "2.2.0"},
        {"workflowId": "wf-t0688-other-parent"},
    ],
    ids=["expired", "expiring-exactly-now", "wrong-version", "wrong-workflow"],
)
def test_an_expired_or_inexact_exception_does_not_suppress(
    override: dict[str, Any],
) -> None:
    result = _evaluate(
        _boundary(childExternalReferences=[_bypass()]),
        rollout_stage="R1",
        exceptions=[_exception(**override)],
    )
    assert result["publicationRefused"] is True
    assert result["diagnostics"][0]["severity"] == "blocker"


def test_an_invalid_exception_is_refused_rather_than_honoured() -> None:
    open_ended = _exception()
    del open_ended["expiresAt"]
    with pytest.raises(ContractSemanticError, match="missing expiresAt"):
        _evaluate(
            _boundary(childExternalReferences=[_bypass()]),
            rollout_stage="R1",
            exceptions=[open_ended],
        )


def test_an_exception_is_irrelevant_at_r0_where_nothing_is_enforced() -> None:
    result = _evaluate(
        _boundary(childExternalReferences=[_bypass()]),
        rollout_stage="R0",
        exceptions=[_exception(expiresAt=_EARLIER)],
    )
    assert result["publicationRefused"] is False


# --------------------------------------------------------------------------
# Complete LoopPlan fixtures
# --------------------------------------------------------------------------


def _identity(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {"ruleKind": "elementDigest", "stableAcrossReplay": True}
    record.update(overrides)
    return record


def _source(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "sourceKind": "collection",
        "sourcePortRef": {"portId": "port-t0688-berths"},
    }
    record.update(overrides)
    return record


def _plan(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "loopStableId": "loop-t0688-berth-sweep",
        "iterationIdentity": _identity(),
        "mode": "sequential",
        "source": _source(),
        "done": {"conditionKind": "sourceExhausted", "deterministic": True},
        "maximumIterations": 12,
        "orderGuarantee": "iterationIdentityOrder",
        "cancellationPolicy": "cancelRemaining",
        "partialSuccessPolicy": "failLoop",
    }
    record.update(overrides)
    return record


def _parallel_plan(**overrides: Any) -> dict[str, Any]:
    return _plan(**{"mode": "parallel", "maximumConcurrency": 4, **overrides})


# --------------------------------------------------------------------------
# LoopPlan: the closed shape
# --------------------------------------------------------------------------


def test_complete_loop_plan_accepts_a_minimal_sequential_plan() -> None:
    workflow.validate_complete_loop_plan(_plan())


def test_complete_loop_plan_accepts_a_fully_declared_parallel_plan() -> None:
    workflow.validate_complete_loop_plan(
        _parallel_plan(
            iterationIdentity=_identity(ruleKind="elementKeyPath", keyPath="/berthId"),
            source=_source(
                sourceKind="stream",
                completionSignal={"signalId": "signal-t0688-tide-closed"},
            ),
            zipSources=[_source(sourcePortRef={"portId": "port-t0688-drafts"})],
            zipMismatchPolicy="refuse",
            gather={
                "gatherId": "gather-t0688-berths",
                "ordering": "iterationIdentityOrder",
            },
            done={
                "conditionKind": "predicate",
                "deterministic": True,
                "predicateRef": {"predicateId": "pred-t0688-all-berths-clear"},
            },
            cancellationPolicy="drainInFlight",
            partialSuccessPolicy="recordAndContinue",
        )
    )


def test_complete_loop_plan_is_a_closed_shape() -> None:
    with pytest.raises(
        ContractSemanticError, match=r"unknown fields \['retryPolicy'\]"
    ):
        workflow.validate_complete_loop_plan(_plan(retryPolicy="exponential"))


@pytest.mark.parametrize("field", sorted(_plan()))
def test_complete_loop_plan_requires_every_frozen_and_declared_member(
    field: str,
) -> None:
    record = _plan()
    del record[field]
    with pytest.raises(ContractSemanticError):
        workflow.validate_complete_loop_plan(record)


def test_complete_loop_plan_does_not_mutate_the_record() -> None:
    record = _parallel_plan()
    before = copy.deepcopy(record)
    workflow.validate_complete_loop_plan(record)
    assert record == before


# --------------------------------------------------------------------------
# LoopPlan: every contradiction refuses with WF_LOOP_PLAN_INVALID
# --------------------------------------------------------------------------


def _assert_loop_invalid(record: dict[str, Any], match: str) -> None:
    with pytest.raises(ContractSemanticError) as raised:
        workflow.validate_complete_loop_plan(record)
    message = str(raised.value)
    assert message.startswith("WF_LOOP_PLAN_INVALID: "), message
    assert match in message, message


def test_identity_rule_must_be_stable_across_replay() -> None:
    _assert_loop_invalid(
        _plan(iterationIdentity=_identity(stableAcrossReplay=False)),
        "stable across replay",
    )


@pytest.mark.parametrize(
    "identity",
    [
        _identity(ruleKind="elementKeyPath"),
        _identity(ruleKind="elementDigest", keyPath="/berthId"),
    ],
    ids=["key-path-rule-without-path", "path-without-key-path-rule"],
)
def test_identity_rule_key_path_is_required_exactly_for_the_key_path_rule(
    identity: dict[str, Any],
) -> None:
    _assert_loop_invalid(
        _plan(iterationIdentity=identity), "keyPath is required exactly"
    )


def test_a_stream_source_declares_its_completion_signal() -> None:
    _assert_loop_invalid(_plan(source=_source(sourceKind="stream")), "completionSignal")


def test_a_collection_source_declares_no_completion_signal() -> None:
    _assert_loop_invalid(
        _plan(source=_source(completionSignal={"signalId": "signal-t0688-none"})),
        "completionSignal",
    )


def test_zipped_sources_require_an_explicit_mismatch_policy() -> None:
    _assert_loop_invalid(
        _plan(zipSources=[_source()]), "zipMismatchPolicy is required exactly"
    )


def test_a_mismatch_policy_without_zipped_sources_is_contradictory() -> None:
    _assert_loop_invalid(
        _plan(zipMismatchPolicy="refuse"), "zipMismatchPolicy is required exactly"
    )


def test_empty_zipped_sources_are_not_a_declaration() -> None:
    _assert_loop_invalid(_plan(zipSources=[]), "must not be declared empty")


def test_carry_is_permitted_only_for_a_sequential_loop() -> None:
    carry = {
        "carryId": "carry-t0688-running-draft",
        "initialValueRef": {"valueId": "v-t0688-0"},
    }
    workflow.validate_complete_loop_plan(_plan(carry=carry))
    _assert_loop_invalid(
        _parallel_plan(carry=carry), "carry is permitted only when mode is sequential"
    )


def test_gather_ordering_must_agree_with_the_order_guarantee() -> None:
    workflow.validate_complete_loop_plan(
        _plan(gather={"gatherId": "g-t0688-1", "ordering": "iterationIdentityOrder"})
    )
    _assert_loop_invalid(
        _plan(gather={"gatherId": "g-t0688-1", "ordering": "settlementOrder"}),
        "gather ordering is inconsistent with orderGuarantee",
    )


def test_a_done_condition_must_be_deterministic() -> None:
    _assert_loop_invalid(
        _plan(done={"conditionKind": "sourceExhausted", "deterministic": False}),
        "must be deterministic",
    )


@pytest.mark.parametrize(
    "done",
    [
        {"conditionKind": "predicate", "deterministic": True},
        {
            "conditionKind": "sourceExhausted",
            "deterministic": True,
            "predicateRef": {"predicateId": "pred-t0688-stray"},
        },
    ],
    ids=["predicate-without-ref", "ref-without-predicate"],
)
def test_a_predicate_condition_names_its_predicate_exactly(
    done: dict[str, Any],
) -> None:
    _assert_loop_invalid(_plan(done=done), "predicateRef is required exactly")


@pytest.mark.parametrize(
    "done",
    [
        {"conditionKind": "iterationCount", "deterministic": True},
        {
            "conditionKind": "sourceExhausted",
            "deterministic": True,
            "iterationCount": 3,
        },
    ],
    ids=["count-without-value", "value-without-count-condition"],
)
def test_an_iteration_count_condition_names_its_count_exactly(
    done: dict[str, Any],
) -> None:
    _assert_loop_invalid(_plan(done=done), "iterationCount is required exactly")


@pytest.mark.parametrize("value", [0, -1, 1.5, True, "12"])
def test_maximum_iterations_must_be_a_positive_integer(value: Any) -> None:
    with pytest.raises(
        ContractSemanticError, match="maximumIterations is not a positive integer"
    ):
        workflow.validate_complete_loop_plan(_plan(maximumIterations=value))


def test_parallel_mode_requires_a_concurrency_bound() -> None:
    record = _parallel_plan()
    del record["maximumConcurrency"]
    _assert_loop_invalid(
        record, "maximumConcurrency is required exactly when mode is parallel"
    )


def test_sequential_mode_refuses_a_concurrency_bound() -> None:
    _assert_loop_invalid(
        _plan(maximumConcurrency=2),
        "maximumConcurrency is required exactly when mode is parallel",
    )


def test_the_concurrency_bound_is_bounded_by_the_iteration_bound() -> None:
    _assert_loop_invalid(
        _parallel_plan(maximumIterations=4, maximumConcurrency=8),
        "maximumConcurrency exceeds maximumIterations",
    )


@pytest.mark.parametrize("value", [0, -1, True])
def test_the_concurrency_bound_must_be_a_positive_integer(value: Any) -> None:
    with pytest.raises(
        ContractSemanticError, match="maximumConcurrency is not a positive integer"
    ):
        workflow.validate_complete_loop_plan(_parallel_plan(maximumConcurrency=value))


@pytest.mark.parametrize(
    ("field", "allowed"),
    [
        ("mode", workflow.LOOP_MODES),
        ("orderGuarantee", workflow.LOOP_ORDER_GUARANTEES),
        ("cancellationPolicy", workflow.LOOP_CANCELLATION_POLICIES),
        ("partialSuccessPolicy", workflow.LOOP_PARTIAL_SUCCESS_POLICIES),
    ],
)
def test_closed_enumerations_refuse_an_undeclared_member(
    field: str, allowed: tuple[str, ...]
) -> None:
    assert allowed  # the enumeration is closed and non-empty
    with pytest.raises(ContractSemanticError, match=f"{field} is not one of"):
        workflow.validate_complete_loop_plan(
            _plan(**{field: "whateverTheRuntimeDecides"})
        )


# --------------------------------------------------------------------------
# The historical LoopPlan validator is untouched
# --------------------------------------------------------------------------


_LEGACY_PLAN = {
    "contractName": "LoopPlan",
    "loopPlanId": "loop-plan-t0688-harbour",
    "loopComponentId": "component-t0688-harbour-loop",
    "frozenAtRunStart": True,
    "maximumIterations": 4,
    "concurrencyLimit": 2,
    "deterministicOrder": ["iteration-harbour-1", "iteration-harbour-2"],
    "iterationLedgerRequired": True,
}


def test_the_historical_loop_plan_validator_keeps_its_exact_behaviour() -> None:
    workflow.validate_loop_plan(copy.deepcopy(_LEGACY_PLAN))
    workflow.validate_workflow_record(copy.deepcopy(_LEGACY_PLAN))
    with pytest.raises(ContractSemanticError, match="frozenAtRunStart must be true"):
        workflow.validate_loop_plan({**_LEGACY_PLAN, "frozenAtRunStart": False})
    with pytest.raises(
        ContractSemanticError, match="iterationLedgerRequired must be true"
    ):
        workflow.validate_loop_plan({**_LEGACY_PLAN, "iterationLedgerRequired": False})
    with pytest.raises(
        ContractSemanticError, match="concurrencyLimit exceeds maximumIterations"
    ):
        workflow.validate_loop_plan({**_LEGACY_PLAN, "concurrencyLimit": 9})


def test_the_two_loop_plan_records_are_separate_shapes() -> None:
    with pytest.raises(ContractSemanticError, match="unknown fields"):
        workflow.validate_loop_plan(_plan())
    with pytest.raises(ContractSemanticError, match="unknown fields"):
        workflow.validate_complete_loop_plan(copy.deepcopy(_LEGACY_PLAN))


def test_the_candidate_shapes_stay_out_of_the_record_registry() -> None:
    assert (
        workflow.WORKFLOW_RECORD_VALIDATORS["LoopPlan"] is workflow.validate_loop_plan
    )
    registered = set(workflow.WORKFLOW_RECORD_VALIDATORS.values())
    for validator in (
        workflow.validate_complete_loop_plan,
        workflow.validate_loop_iteration_ledger,
        workflow.validate_nested_workflow_boundary,
        workflow.validate_nested_boundary_exception,
    ):
        assert validator not in registered
    for name in (
        "NestedWorkflowBoundary",
        "LoopIterationLedger",
        "NestedBoundaryException",
    ):
        assert name not in workflow.WORKFLOW_RECORD_VALIDATORS


# --------------------------------------------------------------------------
# The complete iteration ledger
# --------------------------------------------------------------------------


def _entry(identity: str = "iteration-t0688-1", **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "iterationIdentity": identity,
        "outcomeClass": "succeeded",
        "launchedAt": _EARLIER,
        "launchBundleRef": {"bundleId": "bundle-t0688-launch-1"},
        "inputsDigest": _DIGEST,
        "schedulingIntentsDigest": _OTHER_DIGEST,
        "outputsDigest": _DIGEST,
        "settledAt": _NOW,
        "effectSettlements": [],
    }
    record.update(overrides)
    return record


def _ledger(
    entries: list[dict[str, Any]] | None = None, **overrides: Any
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "ledgerSchemaVersion": "1.0.0",
        "loopStableId": "loop-t0688-berth-sweep",
        "entries": [_entry()] if entries is None else entries,
    }
    record.update(overrides)
    return record


def _late_entry(**overrides: Any) -> dict[str, Any]:
    return _entry(
        "iteration-t0688-late",
        **{
            "outcomeClass": "late",
            "settledAt": _LATER,
            "evidenceRef": {"evidenceId": "ev-t0688-late-1"},
            "appliedToRunState": False,
            **overrides,
        },
    )


def test_the_ledger_is_complete_for_every_outcome_class() -> None:
    assert workflow.LOOP_ITERATION_OUTCOME_CLASSES == (
        "succeeded",
        "failed",
        "cancelled",
        "skipped",
        "late",
    )
    entries = [
        _entry("iteration-t0688-ok"),
        _entry(
            "iteration-t0688-failed",
            outcomeClass="failed",
            outputsDigest=None,
            failureRef={"failureId": "fail-t0688-1"},
            effectSettlements=[
                {
                    "effectRequestId": "effect-t0688-1",
                    "settlementClass": "committed",
                    "verifiedReceiptRef": {"receiptId": "receipt-t0688-1"},
                    "completionContribution": {"contributionId": "contrib-t0688-1"},
                }
            ],
        ),
        _entry(
            "iteration-t0688-cancelled",
            outcomeClass="cancelled",
            outputsDigest=None,
            cancellationDisposition="acknowledged",
        ),
        _entry("iteration-t0688-skipped", outcomeClass="skipped", outputsDigest=None),
        _late_entry(),
    ]
    for entry in entries:
        if entry.get("outputsDigest") is None:
            entry.pop("outputsDigest")
    workflow.validate_loop_iteration_ledger(_ledger(entries, loopSettledAt=_NOW))


def test_the_ledger_is_a_closed_shape() -> None:
    with pytest.raises(ContractSemanticError, match=r"unknown fields \['replayHint'\]"):
        workflow.validate_loop_iteration_ledger(_ledger(replayHint="reuse"))
    with pytest.raises(ContractSemanticError, match=r"unknown fields \['retryCount'\]"):
        workflow.validate_loop_iteration_ledger(_ledger([_entry(retryCount=1)]))


def test_iteration_identities_are_stable_and_unique() -> None:
    with pytest.raises(
        ContractSemanticError, match="must not repeat an iterationIdentity"
    ):
        workflow.validate_loop_iteration_ledger(_ledger([_entry(), _entry()]))
    with pytest.raises(
        ContractSemanticError, match="iterationIdentity is not a well-formed Identifier"
    ):
        workflow.validate_loop_iteration_ledger(_ledger([_entry("")]))


@pytest.mark.parametrize(
    "field",
    [
        "iterationIdentity",
        "outcomeClass",
        "launchedAt",
        "launchBundleRef",
        "inputsDigest",
        "schedulingIntentsDigest",
        "settledAt",
        "effectSettlements",
    ],
)
def test_an_entry_records_its_atomic_launch_and_settlement(field: str) -> None:
    record = _entry()
    del record[field]
    with pytest.raises(ContractSemanticError, match=f"missing {field}"):
        workflow.validate_loop_iteration_ledger(_ledger([record]))


def test_a_carrying_iteration_records_its_carry_digest() -> None:
    workflow.validate_loop_iteration_ledger(_ledger([_entry(carryDigest=_DIGEST)]))
    with pytest.raises(
        ContractSemanticError, match="carryDigest is not a well-formed Digest"
    ):
        workflow.validate_loop_iteration_ledger(
            _ledger([_entry(carryDigest="carry-1")])
        )


def test_settlement_never_precedes_launch() -> None:
    with pytest.raises(ContractSemanticError, match="settledAt precedes launchedAt"):
        workflow.validate_loop_iteration_ledger(
            _ledger([_entry(launchedAt=_NOW, settledAt=_EARLIER)])
        )


@pytest.mark.parametrize("outcome", ["succeeded", "late"])
def test_a_result_outcome_records_outputs_and_no_failure(outcome: str) -> None:
    base = _late_entry() if outcome == "late" else _entry()
    settled = {"loopSettledAt": _NOW} if outcome == "late" else {}

    without_outputs = {
        key: value for key, value in base.items() if key != "outputsDigest"
    }
    with pytest.raises(ContractSemanticError, match=f"{outcome} records outputsDigest"):
        workflow.validate_loop_iteration_ledger(_ledger([without_outputs], **settled))

    with pytest.raises(ContractSemanticError, match=f"{outcome} records outputsDigest"):
        workflow.validate_loop_iteration_ledger(
            _ledger([{**base, "failureRef": {"failureId": "fail-t0688-1"}}], **settled)
        )


def test_a_failed_outcome_records_a_failure_reference_and_no_outputs() -> None:
    failed = {key: value for key, value in _entry().items() if key != "outputsDigest"}
    with pytest.raises(ContractSemanticError, match="failed records failureRef"):
        workflow.validate_loop_iteration_ledger(
            _ledger([{**failed, "outcomeClass": "failed"}])
        )
    with pytest.raises(ContractSemanticError, match="failed records failureRef"):
        workflow.validate_loop_iteration_ledger(
            _ledger([{**_entry(), "outcomeClass": "failed", "failureRef": {"f": "x"}}])
        )


@pytest.mark.parametrize("outcome", ["cancelled", "skipped"])
def test_an_unsettled_outcome_records_neither_outputs_nor_failure(outcome: str) -> None:
    disposition = (
        {"cancellationDisposition": "acknowledged"} if outcome == "cancelled" else {}
    )
    with pytest.raises(
        ContractSemanticError,
        match=f"{outcome} records neither outputsDigest nor failureRef",
    ):
        workflow.validate_loop_iteration_ledger(
            _ledger([_entry(outcomeClass=outcome, **disposition)])
        )


def test_cancellation_disposition_is_required_exactly_for_a_cancelled_iteration() -> (
    None
):
    cancelled = {
        key: value for key, value in _entry().items() if key != "outputsDigest"
    }
    cancelled["outcomeClass"] = "cancelled"
    with pytest.raises(
        ContractSemanticError, match="cancellationDisposition is required exactly"
    ):
        workflow.validate_loop_iteration_ledger(_ledger([cancelled]))
    with pytest.raises(
        ContractSemanticError, match="cancellationDisposition is required exactly"
    ):
        workflow.validate_loop_iteration_ledger(
            _ledger([_entry(cancellationDisposition="acknowledged")])
        )


def test_a_late_result_requires_a_settled_loop_it_arrives_after() -> None:
    with pytest.raises(ContractSemanticError, match="requires a settled loop"):
        workflow.validate_loop_iteration_ledger(_ledger([_late_entry()]))
    with pytest.raises(ContractSemanticError, match="settles after the loop settled"):
        workflow.validate_loop_iteration_ledger(
            _ledger([_late_entry()], loopSettledAt="2026-11-01T12:00:00Z")
        )


def test_a_late_result_is_recorded_as_evidence_and_never_applied() -> None:
    without_evidence = {
        key: value for key, value in _late_entry().items() if key != "evidenceRef"
    }
    with pytest.raises(ContractSemanticError, match="recorded as Evidence"):
        workflow.validate_loop_iteration_ledger(
            _ledger([without_evidence], loopSettledAt=_NOW)
        )

    without_flag = {
        key: value for key, value in _late_entry().items() if key != "appliedToRunState"
    }
    with pytest.raises(ContractSemanticError, match="marked not applied to Run state"):
        workflow.validate_loop_iteration_ledger(
            _ledger([without_flag], loopSettledAt=_NOW)
        )

    with pytest.raises(ContractSemanticError, match="marked not applied to Run state"):
        workflow.validate_loop_iteration_ledger(
            _ledger([_late_entry(appliedToRunState=True)], loopSettledAt=_NOW)
        )


def test_only_a_late_result_is_marked_not_applied() -> None:
    with pytest.raises(
        ContractSemanticError, match="only a late result is marked not applied"
    ):
        workflow.validate_loop_iteration_ledger(
            _ledger([_entry(appliedToRunState=False)])
        )


def test_effect_settlements_are_validated_per_entry() -> None:
    with pytest.raises(
        ContractSemanticError, match="committed requires verifiedReceiptRef"
    ):
        workflow.validate_loop_iteration_ledger(
            _ledger(
                [
                    _entry(
                        effectSettlements=[
                            {
                                "effectRequestId": "effect-t0688-1",
                                "settlementClass": "committed",
                                "completionContribution": {
                                    "contributionId": "c-t0688-1"
                                },
                            }
                        ]
                    )
                ]
            )
        )


def test_ledger_validation_does_not_mutate_the_record() -> None:
    record = _ledger([_entry(), _late_entry()], loopSettledAt=_NOW)
    before = copy.deepcopy(record)
    workflow.validate_loop_iteration_ledger(record)
    assert record == before
