"""Clean-room tests for the T-0688 IP-08 `WorkflowEditOperation` / `WorkflowEditBatch` lane.

Like `RuntimeDefinitionBinding` and `RuntimeTransitionBundle` (T-0688 IP-06/IP-07), neither
`WorkflowEditOperation` nor `WorkflowEditBatch` carries `contractName`, so these tests live
alongside `test_t0688_runtime_transition_bundle.py` rather than the fixture-backed
`test_workflow_hardening_contracts.py` suite.

Independently authored OmniVia examples throughout; no external source, schema, or test is
read or reused.
"""

from __future__ import annotations

from hashlib import sha256 as _sha256
from typing import Any

import pytest

from omnivia_core.contracts.v1 import semantics_workflow as workflow
from omnivia_core.contracts.v1.compatibility import ContractSemanticError

_DIGEST = "sha256:" + "a" * 64


def _semantic_diff(**overrides: Any) -> dict[str, Any]:
    diff: dict[str, Any] = {
        "addedElements": [],
        "removedElements": [],
        "changedElements": [],
        "addedConnections": [],
        "removedConnections": [],
        "changedConnections": [],
        "addedPorts": [],
        "removedPorts": [],
        "changedPorts": [],
        "addedPolicy": [],
        "removedPolicy": [],
        "changedPolicy": [],
    }
    diff.update(overrides)
    return diff


def _precondition(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "preconditionKind": "expected_revision",
        "targetStableId": "element-t0688-harbour-1",
        "expectedDigest": _DIGEST,
    }
    record.update(overrides)
    return record


def _operation(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "operationId": "op-t0688-harbour-1",
        "targetStableId": "element-t0688-harbour-1",
        "operationKind": "updateElement",
        "preconditions": [_precondition()],
        "payloadDigest": _DIGEST,
        "semanticDiff": _semantic_diff(changedElements=["element-t0688-harbour-1"]),
        "compensation": {
            "reviewerAction": "Manually re-run the affected simulation",
            "reason": "Element rename is not losslessly invertible",
        },
        "selectiveApplyDisposition": "applied",
    }
    record.update(overrides)
    return record


def _batch(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "batchSchemaVersion": "1.0.0",
        "batchId": "batch-t0688-harbour-1",
        "draftRef": {"draftId": "draft-t0688-harbour-1"},
        "baseRevision": 0,
        "operations": [_operation()],
    }
    record.update(overrides)
    record["batchPayloadDigest"] = workflow.compute_workflow_edit_batch_payload_digest(
        record
    )
    return record


# --------------------------------------------------------------------------
# Valid records
# --------------------------------------------------------------------------


def test_minimal_valid_operation_validates() -> None:
    workflow.validate_workflow_edit_operation(_operation())


def test_minimal_valid_batch_validates() -> None:
    workflow.validate_workflow_edit_batch(_batch())


def test_add_element_permits_empty_preconditions() -> None:
    workflow.validate_workflow_edit_operation(
        _operation(operationKind="addElement", preconditions=[])
    )


# --------------------------------------------------------------------------
# operationKind, preconditions
# --------------------------------------------------------------------------


def test_operation_kind_is_closed() -> None:
    with pytest.raises(ContractSemanticError, match="not one of"):
        workflow.validate_workflow_edit_operation(
            _operation(operationKind="deleteElement")
        )


@pytest.mark.parametrize("kind", workflow.EDIT_OPERATION_KINDS)
def test_every_operation_kind_validates(kind: str) -> None:
    workflow.validate_workflow_edit_operation(_operation(operationKind=kind))


def test_non_add_element_forbids_empty_preconditions() -> None:
    with pytest.raises(
        ContractSemanticError, match="preconditions may be empty only for addElement"
    ):
        workflow.validate_workflow_edit_operation(
            _operation(operationKind="updateElement", preconditions=[])
        )


def test_precondition_requires_exactly_one_of_expected_digest_or_absence() -> None:
    with pytest.raises(
        ContractSemanticError, match="exactly one of expectedDigest or expectedAbsence"
    ):
        workflow.validate_workflow_edit_operation(
            _operation(
                preconditions=[
                    _precondition(expectedDigest=_DIGEST, expectedAbsence=True)
                ]
            )
        )
    with pytest.raises(
        ContractSemanticError, match="exactly one of expectedDigest or expectedAbsence"
    ):
        workflow.validate_workflow_edit_operation(
            _operation(
                preconditions=[
                    {"preconditionKind": "expected_revision", "targetStableId": "x"}
                ]
            )
        )


def test_precondition_expected_absence_must_be_true() -> None:
    with pytest.raises(ContractSemanticError, match="must be true"):
        workflow.validate_workflow_edit_operation(
            _operation(
                preconditions=[
                    {
                        "preconditionKind": "expected_revision",
                        "targetStableId": "element-t0688-harbour-1",
                        "expectedAbsence": False,
                    }
                ]
            )
        )


def test_precondition_rejects_positional_target_addressing() -> None:
    with pytest.raises(ContractSemanticError, match="not a well-formed Identifier"):
        workflow.validate_workflow_edit_operation(
            _operation(preconditions=[_precondition(targetStableId="")])
        )


# --------------------------------------------------------------------------
# semanticDiff: closed, Workflow-semantic, ordered, no duplicates
# --------------------------------------------------------------------------


def test_semantic_diff_closed_shape_refuses_unknown_fields() -> None:
    with pytest.raises(ContractSemanticError, match="unknown fields"):
        workflow.validate_workflow_edit_operation(
            _operation(semanticDiff=_semantic_diff(text="element renamed"))
        )


def test_semantic_diff_requires_every_category() -> None:
    diff = _semantic_diff()
    del diff["addedPolicy"]
    with pytest.raises(ContractSemanticError, match="missing addedPolicy"):
        workflow.validate_workflow_edit_operation(_operation(semanticDiff=diff))


def test_semantic_diff_lists_must_be_stable_ids() -> None:
    with pytest.raises(ContractSemanticError, match="is not an Identifier"):
        workflow.validate_workflow_edit_operation(
            _operation(semanticDiff=_semantic_diff(addedConnections=[123]))
        )


def test_semantic_diff_lists_must_not_repeat_a_stable_id() -> None:
    with pytest.raises(ContractSemanticError, match="must not repeat a stable ID"):
        workflow.validate_workflow_edit_operation(
            _operation(
                semanticDiff=_semantic_diff(
                    changedPorts=["port-t0688-harbour-1", "port-t0688-harbour-1"]
                )
            )
        )


# --------------------------------------------------------------------------
# inverse / compensation: exactly one, finite recursion
# --------------------------------------------------------------------------


def test_inverse_and_compensation_are_mutually_exclusive() -> None:
    with pytest.raises(
        ContractSemanticError, match="exactly one of inverse or compensation"
    ):
        workflow.validate_workflow_edit_operation(
            _operation(inverse=_operation(operationId="op-t0688-harbour-1-inverse"))
        )


def test_operation_requires_inverse_or_compensation() -> None:
    stripped = _operation()
    del stripped["compensation"]
    with pytest.raises(
        ContractSemanticError, match="exactly one of inverse or compensation"
    ):
        workflow.validate_workflow_edit_operation(stripped)


def test_invertible_operation_carries_exact_inverse() -> None:
    inverse = _operation(
        operationId="op-t0688-harbour-1-inverse",
        operationKind="updateElement",
    )
    del inverse["compensation"]
    inverse["compensation"] = {
        "reviewerAction": "Restore the prior element label",
        "reason": "Inverse is itself a further update, recorded here rather than recursively",
    }
    forward = _operation()
    del forward["compensation"]
    forward["inverse"] = inverse
    workflow.validate_workflow_edit_operation(forward)


def test_inverse_operation_must_not_itself_carry_an_inverse() -> None:
    nested_inverse = _operation(operationId="op-t0688-harbour-1-inverse-inverse")
    inverse = _operation(operationId="op-t0688-harbour-1-inverse")
    del inverse["compensation"]
    inverse["inverse"] = nested_inverse

    forward = _operation()
    del forward["compensation"]
    forward["inverse"] = inverse

    with pytest.raises(
        ContractSemanticError,
        match="an inverse operation must not itself carry an inverse",
    ):
        workflow.validate_workflow_edit_operation(forward)


def test_compensation_requires_reviewer_action_and_reason() -> None:
    with pytest.raises(ContractSemanticError, match="missing reviewerAction"):
        workflow.validate_workflow_edit_operation(
            _operation(compensation={"reason": "not invertible"})
        )
    with pytest.raises(ContractSemanticError, match="missing reason"):
        workflow.validate_workflow_edit_operation(
            _operation(compensation={"reviewerAction": "review manually"})
        )


# --------------------------------------------------------------------------
# selectiveApplyDisposition
# --------------------------------------------------------------------------


def test_selective_apply_disposition_is_closed() -> None:
    with pytest.raises(ContractSemanticError, match="not one of"):
        workflow.validate_workflow_edit_operation(
            _operation(selectiveApplyDisposition="partially_applied")
        )


def test_blocked_disposition_requires_diagnostic_ref() -> None:
    with pytest.raises(ContractSemanticError, match="missing diagnosticRef"):
        workflow.validate_workflow_edit_operation(
            _operation(selectiveApplyDisposition="blocked")
        )
    workflow.validate_workflow_edit_operation(
        _operation(
            selectiveApplyDisposition="blocked",
            diagnosticRef={"diagnosticCode": "WF_EDIT_PRECONDITION_FAILED"},
        )
    )


@pytest.mark.parametrize("disposition", ["applied", "skipped"])
def test_non_blocked_dispositions_forbid_diagnostic_ref(disposition: str) -> None:
    with pytest.raises(ContractSemanticError, match="must not carry diagnosticRef"):
        workflow.validate_workflow_edit_operation(
            _operation(
                selectiveApplyDisposition=disposition,
                diagnosticRef={"diagnosticCode": "WF_EDIT_PRECONDITION_FAILED"},
            )
        )


# --------------------------------------------------------------------------
# WorkflowEditBatch: closed shape and scalars
# --------------------------------------------------------------------------


def test_batch_closed_shape_refuses_unknown_fields() -> None:
    with pytest.raises(ContractSemanticError, match="unknown fields"):
        workflow.validate_workflow_edit_batch(_batch(contractName="WorkflowEditBatch"))


def test_batch_base_revision_must_be_non_negative_integer() -> None:
    with pytest.raises(ContractSemanticError, match="is not a non-negative integer"):
        workflow.validate_workflow_edit_batch(_batch(baseRevision=-1))


def test_batch_operations_must_not_be_empty() -> None:
    with pytest.raises(ContractSemanticError, match="operations must not be empty"):
        workflow.validate_workflow_edit_batch(_batch(operations=[]))


def test_batch_draft_ref_must_be_a_non_empty_reference() -> None:
    with pytest.raises(ContractSemanticError, match="must not be empty"):
        workflow.validate_workflow_edit_batch(_batch(draftRef={}))


# --------------------------------------------------------------------------
# WorkflowEditBatch: unique operation IDs, ordered digest
# --------------------------------------------------------------------------


def test_batch_operations_must_not_repeat_an_operation_id() -> None:
    op = _operation()
    with pytest.raises(ContractSemanticError, match="must not repeat an operationId"):
        workflow.validate_workflow_edit_batch(_batch(operations=[op, dict(op)]))


def test_batch_payload_digest_is_ordered_over_operations_only() -> None:
    op_one = _operation(operationId="op-t0688-harbour-1")
    op_two = _operation(
        operationId="op-t0688-harbour-2",
        targetStableId="element-t0688-harbour-2",
        semanticDiff=_semantic_diff(changedElements=["element-t0688-harbour-2"]),
    )
    forward = _batch(operations=[op_one, op_two])
    reordered_fields = {**forward, "operations": [op_two, op_one]}
    del reordered_fields["batchPayloadDigest"]
    reordered_digest = workflow.compute_workflow_edit_batch_payload_digest(
        reordered_fields
    )
    assert reordered_digest != forward["batchPayloadDigest"]


def test_batch_payload_digest_excludes_only_batch_payload_digest() -> None:
    batch = _batch()
    without_digest = {k: v for k, v in batch.items() if k != "batchPayloadDigest"}
    digest_a = workflow.compute_workflow_edit_batch_payload_digest(without_digest)
    digest_b = workflow.compute_workflow_edit_batch_payload_digest(
        {**without_digest, "batchPayloadDigest": "sha256:" + "f" * 64}
    )
    assert digest_a == digest_b == batch["batchPayloadDigest"]


def test_batch_payload_digest_refused_after_tampering_without_recompute() -> None:
    op_one = _operation(operationId="op-t0688-harbour-1")
    op_two = _operation(
        operationId="op-t0688-harbour-2",
        targetStableId="element-t0688-harbour-2",
        semanticDiff=_semantic_diff(changedElements=["element-t0688-harbour-2"]),
    )
    batch = _batch(operations=[op_one])
    tampered = {**batch, "operations": [op_one, op_two]}
    with pytest.raises(ContractSemanticError, match="does not match its recomputation"):
        workflow.validate_workflow_edit_batch(tampered)


# --------------------------------------------------------------------------
# evaluate_workflow_edit_batch: pure atomic evaluator
# --------------------------------------------------------------------------


def test_evaluate_accepts_whole_batch_atomically() -> None:
    batch = _batch()
    result = workflow.evaluate_workflow_edit_batch(
        batch,
        current_revision=0,
        precondition_check=lambda _: True,
        workflow_check=lambda: True,
    )
    assert result["batchId"] == batch["batchId"]
    assert result["acceptedRevision"] == 1
    assert result["operationIds"] == ("op-t0688-harbour-1",)
    assert result["batchPayloadDigest"] == batch["batchPayloadDigest"]


def test_evaluate_refuses_stale_base_revision() -> None:
    batch = _batch(baseRevision=0)
    with pytest.raises(ContractSemanticError, match="WF_EDIT_STALE_BASE"):
        workflow.evaluate_workflow_edit_batch(
            batch,
            current_revision=1,
            precondition_check=lambda _: True,
            workflow_check=lambda: True,
        )


def test_evaluate_refuses_a_failing_precondition_naming_operation_and_target() -> None:
    batch = _batch()
    with pytest.raises(
        ContractSemanticError,
        match=r"WF_EDIT_PRECONDITION_FAILED.*op-t0688-harbour-1.*element-t0688-harbour-1",
    ):
        workflow.evaluate_workflow_edit_batch(
            batch,
            current_revision=0,
            precondition_check=lambda _: False,
            workflow_check=lambda: True,
        )


def test_evaluate_checks_all_preconditions_of_every_operation_before_accepting() -> (
    None
):
    op_one = _operation(operationId="op-t0688-harbour-1")
    op_two = _operation(
        operationId="op-t0688-harbour-2",
        targetStableId="element-t0688-harbour-2",
        semanticDiff=_semantic_diff(changedElements=["element-t0688-harbour-2"]),
        preconditions=[_precondition(targetStableId="element-t0688-harbour-2")],
    )
    batch = _batch(operations=[op_one, op_two])
    seen: list[str] = []

    def check(precondition: dict[str, Any]) -> bool:
        seen.append(precondition["targetStableId"])
        return precondition["targetStableId"] != "element-t0688-harbour-2"

    with pytest.raises(ContractSemanticError, match="WF_EDIT_PRECONDITION_FAILED"):
        workflow.evaluate_workflow_edit_batch(
            batch,
            current_revision=0,
            precondition_check=check,
            workflow_check=lambda: True,
        )
    assert seen == ["element-t0688-harbour-1", "element-t0688-harbour-2"]


def test_evaluate_does_not_mutate_its_inputs() -> None:
    batch = _batch()
    snapshot = {
        k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
        for k, v in batch.items()
    }
    workflow.evaluate_workflow_edit_batch(
        batch,
        current_revision=0,
        precondition_check=lambda _: True,
        workflow_check=lambda: True,
    )
    assert batch["batchId"] == snapshot["batchId"]
    assert batch["operations"] == snapshot["operations"]
    assert batch["baseRevision"] == snapshot["baseRevision"]


def test_evaluate_first_validates_shape_before_checking_revision() -> None:
    malformed = _batch()
    malformed["operations"] = []
    with pytest.raises(ContractSemanticError, match="operations must not be empty"):
        workflow.evaluate_workflow_edit_batch(
            malformed,
            current_revision=99,
            precondition_check=lambda _: True,
            workflow_check=lambda: True,
        )


# --------------------------------------------------------------------------
# evaluate_workflow_edit_batch: workflow_check runs last, before acceptance
# --------------------------------------------------------------------------


def test_evaluate_invokes_workflow_check_after_preconditions_and_before_accept() -> (
    None
):
    batch = _batch()
    calls: list[str] = []

    def precondition_check(precondition: dict[str, Any]) -> bool:
        calls.append(f"precondition:{precondition['targetStableId']}")
        return True

    def workflow_check() -> bool:
        calls.append("workflow_check")
        return True

    result = workflow.evaluate_workflow_edit_batch(
        batch,
        current_revision=0,
        precondition_check=precondition_check,
        workflow_check=workflow_check,
    )
    assert calls == ["precondition:element-t0688-harbour-1", "workflow_check"]
    assert result["acceptedRevision"] == 1


def test_evaluate_refuses_batch_on_failing_workflow_check() -> None:
    batch = _batch()
    with pytest.raises(ContractSemanticError, match="Workflow Check failure"):
        workflow.evaluate_workflow_edit_batch(
            batch,
            current_revision=0,
            precondition_check=lambda _: True,
            workflow_check=lambda: False,
        )


@pytest.mark.parametrize("current_revision", [-1, True, 1.5, "0"])
def test_evaluate_refuses_invalid_current_revision(current_revision: object) -> None:
    with pytest.raises(
        ContractSemanticError, match="current_revision is not a non-negative integer"
    ):
        workflow.evaluate_workflow_edit_batch(
            _batch(),
            current_revision=current_revision,  # type: ignore[arg-type]
            precondition_check=lambda _: True,
            workflow_check=lambda: True,
        )


def test_evaluate_requires_literal_true_callback_results() -> None:
    with pytest.raises(ContractSemanticError, match="WF_EDIT_PRECONDITION_FAILED"):
        workflow.evaluate_workflow_edit_batch(
            _batch(),
            current_revision=0,
            precondition_check=lambda _: 1,  # type: ignore[return-value]
            workflow_check=lambda: True,
        )

    with pytest.raises(ContractSemanticError, match="Workflow Check failure"):
        workflow.evaluate_workflow_edit_batch(
            _batch(),
            current_revision=0,
            precondition_check=lambda _: True,
            workflow_check=lambda: 1,  # type: ignore[return-value]
        )


def test_evaluate_workflow_check_not_invoked_when_precondition_fails() -> None:
    batch = _batch()
    calls: list[str] = []

    def workflow_check() -> bool:
        calls.append("workflow_check")
        return True

    with pytest.raises(ContractSemanticError, match="WF_EDIT_PRECONDITION_FAILED"):
        workflow.evaluate_workflow_edit_batch(
            batch,
            current_revision=0,
            precondition_check=lambda _: False,
            workflow_check=workflow_check,
        )
    assert calls == []


def test_evaluate_does_not_mutate_inputs_on_failing_workflow_check() -> None:
    batch = _batch()
    snapshot = {
        k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
        for k, v in batch.items()
    }
    with pytest.raises(ContractSemanticError, match="Workflow Check failure"):
        workflow.evaluate_workflow_edit_batch(
            batch,
            current_revision=0,
            precondition_check=lambda _: True,
            workflow_check=lambda: False,
        )
    assert batch["batchId"] == snapshot["batchId"]
    assert batch["operations"] == snapshot["operations"]
    assert batch["baseRevision"] == snapshot["baseRevision"]


# --------------------------------------------------------------------------
# ComponentPortContract: valid v1 shapes and optional defaults
# --------------------------------------------------------------------------


def _port(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "portId": "port-t0688-harbour-1",
        "direction": "input",
        "semanticType": "harbour-cargo-manifest",
        "physicalSchema": {"schemaRef": "schema-t0688-harbour-1"},
        "cardinality": "single",
        "presence": "present",
        "classification": {"level": "internal"},
        "lineage": {"sourceRef": "component-t0688-harbour-1"},
    }
    record.update(overrides)
    return record


def test_minimal_valid_port_validates() -> None:
    workflow.validate_component_port_contract(_port())


def test_port_optional_fields_absent_is_valid_v1_behavior() -> None:
    port = _port()
    assert "deliveryMode" not in port
    assert "driver" not in port
    assert "fanOutPolicy" not in port
    workflow.validate_component_port_contract(port)


def test_port_optional_delivery_mode_and_fan_out_vocabulary() -> None:
    for mode in workflow.PORT_DELIVERY_MODES:
        workflow.validate_component_port_contract(_port(deliveryMode=mode))
    for policy in workflow.PORT_FAN_OUT_POLICIES:
        workflow.validate_component_port_contract(_port(fanOutPolicy=policy))
    for direction in workflow.PORT_DIRECTIONS:
        workflow.validate_component_port_contract(_port(direction=direction))
    workflow.validate_component_port_contract(_port(driver=True))
    workflow.validate_component_port_contract(_port(driver=False))


def test_port_closed_shape_refuses_unknown_fields() -> None:
    with pytest.raises(ContractSemanticError, match="unknown fields"):
        workflow.validate_component_port_contract(_port(extra="nope"))


def test_port_direction_is_closed() -> None:
    with pytest.raises(ContractSemanticError, match="not one of"):
        workflow.validate_component_port_contract(_port(direction="sideways"))


def test_port_delivery_mode_is_closed() -> None:
    with pytest.raises(ContractSemanticError, match="not one of"):
        workflow.validate_component_port_contract(_port(deliveryMode="parallel"))


def test_port_fan_out_policy_is_closed() -> None:
    with pytest.raises(ContractSemanticError, match="not one of"):
        workflow.validate_component_port_contract(_port(fanOutPolicy="scatter"))


def test_port_driver_must_be_boolean() -> None:
    with pytest.raises(ContractSemanticError, match="is not a boolean"):
        workflow.validate_component_port_contract(_port(driver="true"))


# --------------------------------------------------------------------------
# ComponentPortContract: dynamicDerivation
# --------------------------------------------------------------------------


def _dynamic_derivation(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "derivationExpression": "derive-from-manifest-schema",
        "resolvedPortSetDigest": "sha256:" + "b" * 64,
        "derivationDigest": "sha256:" + "c" * 64,
    }
    record.update(overrides)
    return record


def test_port_with_valid_dynamic_derivation_validates() -> None:
    workflow.validate_component_port_contract(
        _port(dynamicDerivation=_dynamic_derivation())
    )


def test_dynamic_derivation_exact_three_field_shape() -> None:
    assert set(_dynamic_derivation()) == {
        "derivationExpression",
        "resolvedPortSetDigest",
        "derivationDigest",
    }


def test_dynamic_derivation_missing_expression_is_unresolved() -> None:
    derivation = _dynamic_derivation()
    del derivation["derivationExpression"]
    with pytest.raises(ContractSemanticError, match="WF_PORT_DYNAMIC_UNRESOLVED"):
        workflow.validate_component_port_contract(_port(dynamicDerivation=derivation))


def test_dynamic_derivation_empty_expression_is_unresolved() -> None:
    with pytest.raises(ContractSemanticError, match="WF_PORT_DYNAMIC_UNRESOLVED"):
        workflow.validate_component_port_contract(
            _port(dynamicDerivation=_dynamic_derivation(derivationExpression=""))
        )


def test_dynamic_derivation_missing_resolved_port_set_digest_is_unresolved() -> None:
    derivation = _dynamic_derivation()
    del derivation["resolvedPortSetDigest"]
    with pytest.raises(ContractSemanticError, match="WF_PORT_DYNAMIC_UNRESOLVED"):
        workflow.validate_component_port_contract(_port(dynamicDerivation=derivation))


def test_dynamic_derivation_malformed_resolved_port_set_digest_is_unresolved() -> None:
    with pytest.raises(ContractSemanticError, match="WF_PORT_DYNAMIC_UNRESOLVED"):
        workflow.validate_component_port_contract(
            _port(
                dynamicDerivation=_dynamic_derivation(
                    resolvedPortSetDigest="not-a-digest"
                )
            )
        )


def test_dynamic_derivation_missing_derivation_digest_is_unresolved() -> None:
    derivation = _dynamic_derivation()
    del derivation["derivationDigest"]
    with pytest.raises(ContractSemanticError, match="WF_PORT_DYNAMIC_UNRESOLVED"):
        workflow.validate_component_port_contract(_port(dynamicDerivation=derivation))


def test_dynamic_derivation_malformed_derivation_digest_is_unresolved() -> None:
    with pytest.raises(ContractSemanticError, match="WF_PORT_DYNAMIC_UNRESOLVED"):
        workflow.validate_component_port_contract(
            _port(
                dynamicDerivation=_dynamic_derivation(derivationDigest="not-a-digest")
            )
        )


def test_dynamic_derivation_unknown_field_is_unresolved() -> None:
    derivation = _dynamic_derivation(extra="nope")
    with pytest.raises(ContractSemanticError, match="WF_PORT_DYNAMIC_UNRESOLVED"):
        workflow.validate_component_port_contract(_port(dynamicDerivation=derivation))


def test_dynamic_derivation_invented_resolved_ports_is_rejected_as_unknown() -> None:
    derivation = _dynamic_derivation(
        resolvedPorts=[_port(portId="port-t0688-harbour-derived-1")]
    )
    with pytest.raises(ContractSemanticError, match="WF_PORT_DYNAMIC_UNRESOLVED"):
        workflow.validate_component_port_contract(_port(dynamicDerivation=derivation))


# --------------------------------------------------------------------------
# ComponentPortSet
# --------------------------------------------------------------------------


def test_valid_port_set_validates() -> None:
    workflow.validate_component_port_set(
        [
            _port(portId="port-t0688-harbour-1"),
            _port(portId="port-t0688-harbour-2", direction="output"),
        ]
    )


def test_port_set_must_not_be_empty() -> None:
    with pytest.raises(ContractSemanticError, match="must not be empty"):
        workflow.validate_component_port_set([])


def test_port_set_rejects_duplicate_port_ids() -> None:
    with pytest.raises(ContractSemanticError, match="must not repeat a portId"):
        workflow.validate_component_port_set([_port(), _port()])


def test_port_set_allows_single_driver_input() -> None:
    workflow.validate_component_port_set(
        [
            _port(portId="port-t0688-harbour-1", driver=True),
            _port(portId="port-t0688-harbour-2", direction="output"),
        ]
    )


def test_port_set_rejects_multiple_driver_inputs() -> None:
    with pytest.raises(
        ContractSemanticError, match="at most one input port may declare driver"
    ):
        workflow.validate_component_port_set(
            [
                _port(portId="port-t0688-harbour-1", driver=True),
                _port(portId="port-t0688-harbour-2", driver=True),
            ]
        )


def test_port_set_rejects_output_driver() -> None:
    with pytest.raises(
        ContractSemanticError, match="only an input port may declare driver"
    ):
        workflow.validate_component_port_set(
            [_port(portId="port-t0688-harbour-1", direction="output", driver=True)]
        )


def test_port_set_does_not_mutate_inputs() -> None:
    ports = [
        _port(portId="port-t0688-harbour-1"),
        _port(portId="port-t0688-harbour-2", direction="output"),
    ]
    snapshot = [dict(p) for p in ports]
    workflow.validate_component_port_set(ports)
    assert ports == snapshot


# --------------------------------------------------------------------------
# OwirSourceProjection
# --------------------------------------------------------------------------


def _owir_text() -> str:
    return (
        "element el-t0688-harbour-1\n"
        "  kind: task\n"
        "element el-t0688-harbour-2\n"
        "  kind: gateway\n"
    )


def _owir_projection(**overrides: Any) -> dict[str, Any]:
    text = overrides.pop("text", _owir_text())
    record: dict[str, Any] = {
        "projectionSchemaVersion": "1.0.0",
        "projectionId": "owir-t0688-harbour-1",
        "sourceRef": {"draftId": "draft-t0688-harbour-1"},
        "sourceRevision": 3,
        "sourceDefinitionDigest": _DIGEST,
        "generated": True,
        "readOnly": True,
        "canonical": False,
        "stableElementIds": ["el-t0688-harbour-1", "el-t0688-harbour-2"],
        "text": text,
        "projectionDigest": f"sha256:{_sha256(text.encode('utf-8')).hexdigest()}",
    }
    record.update(overrides)
    return record


def test_minimal_valid_owir_projection_validates() -> None:
    workflow.validate_owir_source_projection(_owir_projection())


def test_owir_projection_generated_read_only_not_canonical_are_locked() -> None:
    with pytest.raises(ContractSemanticError, match="must be true"):
        workflow.validate_owir_source_projection(_owir_projection(generated=False))
    with pytest.raises(ContractSemanticError, match="must be true"):
        workflow.validate_owir_source_projection(_owir_projection(readOnly=False))
    with pytest.raises(ContractSemanticError, match="must be false"):
        workflow.validate_owir_source_projection(_owir_projection(canonical=True))


def test_owir_projection_closed_shape_refuses_unknown_fields() -> None:
    with pytest.raises(ContractSemanticError, match="unknown fields"):
        workflow.validate_owir_source_projection(
            _owir_projection(contractName="OwirSourceProjection")
        )


def test_owir_projection_source_revision_must_be_non_negative_int() -> None:
    with pytest.raises(ContractSemanticError, match="is not a non-negative integer"):
        workflow.validate_owir_source_projection(_owir_projection(sourceRevision=-1))


def test_owir_projection_source_definition_digest_must_be_a_digest() -> None:
    with pytest.raises(ContractSemanticError, match="not a well-formed Digest"):
        workflow.validate_owir_source_projection(
            _owir_projection(sourceDefinitionDigest="not-a-digest")
        )


def test_owir_projection_stable_element_ids_must_not_be_empty() -> None:
    with pytest.raises(
        ContractSemanticError, match="stableElementIds must not be empty"
    ):
        workflow.validate_owir_source_projection(_owir_projection(stableElementIds=[]))


def test_owir_projection_stable_element_ids_must_not_repeat() -> None:
    with pytest.raises(ContractSemanticError, match="must not repeat a stable ID"):
        workflow.validate_owir_source_projection(
            _owir_projection(
                stableElementIds=["el-t0688-harbour-1", "el-t0688-harbour-1"]
            )
        )


def test_owir_projection_stable_element_ids_order_is_retained() -> None:
    record = _owir_projection()
    assert record["stableElementIds"] == ["el-t0688-harbour-1", "el-t0688-harbour-2"]
    workflow.validate_owir_source_projection(record)


def test_owir_projection_requires_exact_element_line_for_each_stable_id() -> None:
    text = "element el-t0688-harbour-1\n"
    with pytest.raises(ContractSemanticError, match="element el-t0688-harbour-2"):
        workflow.validate_owir_source_projection(
            _owir_projection(
                text=text,
                stableElementIds=["el-t0688-harbour-1", "el-t0688-harbour-2"],
                projectionDigest=f"sha256:{_sha256(text.encode('utf-8')).hexdigest()}",
            )
        )


def test_owir_projection_substring_is_not_enough() -> None:
    text = "element el-t0688-harbour-1-extended\n"
    with pytest.raises(ContractSemanticError, match="element el-t0688-harbour-1"):
        workflow.validate_owir_source_projection(
            _owir_projection(
                text=text,
                stableElementIds=["el-t0688-harbour-1"],
                projectionDigest=f"sha256:{_sha256(text.encode('utf-8')).hexdigest()}",
            )
        )


def test_owir_projection_trailing_tokens_are_not_an_exact_element_line() -> None:
    text = "element el-t0688-harbour-1 trailing\n"
    with pytest.raises(ContractSemanticError, match="element el-t0688-harbour-1"):
        workflow.validate_owir_source_projection(
            _owir_projection(
                text=text,
                stableElementIds=["el-t0688-harbour-1"],
                projectionDigest=f"sha256:{_sha256(text.encode('utf-8')).hexdigest()}",
            )
        )


def test_owir_projection_digest_recomputation() -> None:
    record = _owir_projection()
    assert (
        record["projectionDigest"]
        == f"sha256:{_sha256(record['text'].encode('utf-8')).hexdigest()}"
    )
    workflow.validate_owir_source_projection(record)


def test_owir_projection_digest_tamper_is_refused() -> None:
    with pytest.raises(ContractSemanticError, match="projectionDigest does not match"):
        workflow.validate_owir_source_projection(
            _owir_projection(projectionDigest="sha256:" + "0" * 64)
        )


def test_owir_projection_not_in_workflow_record_validators() -> None:
    assert "OwirSourceProjection" not in workflow.WORKFLOW_RECORD_VALIDATORS


def test_owir_projection_exposes_no_parse_apply_merge_function() -> None:
    for name in ("parse", "apply", "merge"):
        assert not hasattr(workflow, f"{name}_owir_source_projection")


def test_owir_matches_revision_accepts_current() -> None:
    record = _owir_projection()
    workflow.validate_owir_source_projection_matches_revision(
        record, current_revision=3, current_definition_digest=_DIGEST
    )


def test_owir_matches_revision_refuses_stale_revision() -> None:
    record = _owir_projection()
    with pytest.raises(
        ContractSemanticError, match="WF_OWIR_PROJECTION_STALE.*must be regenerated"
    ):
        workflow.validate_owir_source_projection_matches_revision(
            record, current_revision=4, current_definition_digest=_DIGEST
        )


def test_owir_matches_revision_refuses_stale_definition_digest() -> None:
    record = _owir_projection()
    with pytest.raises(
        ContractSemanticError, match="WF_OWIR_PROJECTION_STALE.*must be regenerated"
    ):
        workflow.validate_owir_source_projection_matches_revision(
            record, current_revision=3, current_definition_digest="sha256:" + "9" * 64
        )


def test_owir_matches_revision_validates_shape_first() -> None:
    record = _owir_projection(stableElementIds=[])
    with pytest.raises(
        ContractSemanticError, match="stableElementIds must not be empty"
    ):
        workflow.validate_owir_source_projection_matches_revision(
            record, current_revision=3, current_definition_digest=_DIGEST
        )


@pytest.mark.parametrize("current_revision", [-1, True, 1.5, "3"])
def test_owir_matches_revision_refuses_invalid_current_revision(
    current_revision: object,
) -> None:
    with pytest.raises(
        ContractSemanticError, match="current_revision is not a non-negative integer"
    ):
        workflow.validate_owir_source_projection_matches_revision(
            _owir_projection(),
            current_revision=current_revision,  # type: ignore[arg-type]
            current_definition_digest=_DIGEST,
        )


def test_owir_matches_revision_refuses_invalid_current_definition_digest() -> None:
    with pytest.raises(
        ContractSemanticError, match="current_definition_digest.*well-formed Digest"
    ):
        workflow.validate_owir_source_projection_matches_revision(
            _owir_projection(),
            current_revision=3,
            current_definition_digest="not-a-digest",
        )


def test_owir_matches_revision_does_not_mutate_record() -> None:
    record = _owir_projection()
    snapshot = dict(record)
    workflow.validate_owir_source_projection_matches_revision(
        record, current_revision=3, current_definition_digest=_DIGEST
    )
    assert record == snapshot
