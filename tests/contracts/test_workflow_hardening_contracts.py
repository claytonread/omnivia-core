"""Clean-room tests for the T-0679 Workflow hardening semantic validators.

The fixture is deliberately test-local rather than listed in the public
Application Contract fixture manifest: G4 proves Core validation behavior for
the ratified DOC-004 Appendix AB records without turning this slice into a
generated schema/publication rollout.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from omnivia_core.contracts.v1 import semantics_workflow as workflow
from omnivia_core.contracts.v1.compatibility import ContractSemanticError

FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "workflow-hardening-records-v1.json"
)


def _records() -> dict[str, Any]:
    parsed = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    records = parsed["records"]
    assert isinstance(records, dict)
    return records


def _record(name: str) -> dict[str, Any]:
    record = copy.deepcopy(_records()[name])
    assert isinstance(record, dict)
    return record


def _set(record: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    node: Any = record
    parts = path.split(".")
    for part in parts[:-1]:
        node = node[int(part)] if part.isdigit() else node[part]
    node[parts[-1]] = value
    return record


def _delete(record: dict[str, Any], path: str) -> dict[str, Any]:
    node: Any = record
    parts = path.split(".")
    for part in parts[:-1]:
        node = node[int(part)] if part.isdigit() else node[part]
    del node[parts[-1]]
    return record


@pytest.mark.parametrize("record", _records().values(), ids=_records().keys())
def test_every_clean_room_record_validates(record: dict[str, Any]) -> None:
    workflow.validate_workflow_record(record)


def test_published_validator_registry_is_immutable() -> None:
    with pytest.raises(TypeError):
        workflow.WORKFLOW_RECORD_VALIDATORS["WorkflowValue"] = workflow.validate_absent_value  # type: ignore[index]


def test_workflow_value_preserves_absent_null_and_present_as_separate_states() -> None:
    workflow.validate_workflow_value(_record("workflowValueNull"))
    workflow.validate_workflow_value(_record("workflowValueEmpty"))
    workflow.validate_workflow_value(_record("workflowValueRedacted"))
    workflow.validate_absent_value(_record("absentValue"))

    with pytest.raises(ContractSemanticError, match="present values must carry value"):
        workflow.validate_workflow_value(_delete(_record("workflowValuePresent"), "value"))
    with pytest.raises(ContractSemanticError, match="present values must not carry null"):
        workflow.validate_workflow_value(_set(_record("workflowValuePresent"), "value", None))
    with pytest.raises(ContractSemanticError, match="absent must not carry value"):
        workflow.validate_workflow_value(_set(_record("workflowValueNull"), "presence", "absent"))
    with pytest.raises(ContractSemanticError, match="must not carry value"):
        workflow.validate_absent_value(_set(_record("absentValue"), "value", None))
    with pytest.raises(ContractSemanticError, match="empty values must carry an empty value"):
        workflow.validate_workflow_value(_set(_record("workflowValueEmpty"), "value", ["item"]))
    with pytest.raises(ContractSemanticError, match="redacted must not carry value"):
        workflow.validate_workflow_value(_set(_record("workflowValueRedacted"), "value", "secret"))
    with pytest.raises(ContractSemanticError, match="redacted requires diagnostic"):
        workflow.validate_workflow_value(_delete(_record("workflowValueRedacted"), "diagnostic"))


def test_start_run_requires_runnable_not_merely_definition_valid() -> None:
    workflow.validate_workflow_check_readiness_extension(_record("workflowCheckValidNotRunnable"))
    with pytest.raises(ContractSemanticError, match="not runnable"):
        workflow.validate_start_workflow_run_readiness(_record("workflowCheckValidNotRunnable"))

    with pytest.raises(ContractSemanticError, match="requires every ComponentImplementationBinding"):
        workflow.validate_workflow_check_readiness_extension(
            _set(_record("workflowCheckValidNotRunnable"), "runnable", True)
        )
    with pytest.raises(ContractSemanticError, match="runnable requires definitionValid"):
        workflow.validate_workflow_check_readiness_extension(
            _set(_record("workflowCheckRunnable"), "definitionValid", False)
        )
    with pytest.raises(ContractSemanticError, match="non-available bindings require diagnostics"):
        workflow.validate_workflow_check_readiness_extension(
            _delete(
                _record("workflowCheckValidNotRunnable"),
                "implementationBindings.0.diagnostics",
            )
        )


def test_suggested_fix_cannot_exceed_proposal_authority() -> None:
    with pytest.raises(ContractSemanticError, match="authorityCeiling must be proposal_only"):
        workflow.validate_suggested_workflow_fix(
            _set(_record("suggestedWorkflowFix"), "authorityCeiling", "approved_for_publish")
        )
    with pytest.raises(ContractSemanticError, match="unknown fields"):
        workflow.validate_suggested_workflow_fix(
            _set(_record("suggestedWorkflowFix"), "publishes", True)
        )


def test_simulation_cannot_claim_production_readiness_or_browser_effects() -> None:
    with pytest.raises(ContractSemanticError, match="productionReadinessContribution must be false"):
        workflow.validate_simulation_result(
            _set(_record("simulationResult"), "productionReadinessContribution", True)
        )
    with pytest.raises(ContractSemanticError, match="effectsPerformed must be false"):
        workflow.validate_simulation_result(
            _set(_record("simulationResult"), "browserEnvelope.effectsPerformed", True)
        )


def test_branch_policy_and_loop_plan_are_run_start_frozen_semantics() -> None:
    with pytest.raises(ContractSemanticError, match="threshold is only valid"):
        workflow.validate_branch_aggregate_policy(
            _set(_record("branchAggregatePolicy"), "mode", "all")
        )
    with pytest.raises(ContractSemanticError, match="missing threshold"):
        workflow.validate_branch_aggregate_policy(
            _delete(_record("branchAggregatePolicy"), "threshold")
        )
    with pytest.raises(ContractSemanticError, match="frozenAtRunStart must be true"):
        workflow.validate_loop_plan(_set(_record("loopPlan"), "frozenAtRunStart", False))
    with pytest.raises(ContractSemanticError, match="iterationLedgerRequired must be true"):
        workflow.validate_loop_plan(_set(_record("loopPlan"), "iterationLedgerRequired", False))
    with pytest.raises(ContractSemanticError, match="concurrencyLimit exceeds maximumIterations"):
        workflow.validate_loop_plan(_set(_record("loopPlan"), "concurrencyLimit", 26))


def test_attempt_settlement_keeps_indeterminate_and_failed_effects_visible() -> None:
    with pytest.raises(ContractSemanticError, match="indeterminate effects settle as indeterminate"):
        workflow.validate_attempt_settlement(
            _set(_record("attemptSettlementIndeterminate"), "outcome", "failed")
        )
    with pytest.raises(ContractSemanticError, match="may not be absorbed as success"):
        workflow.validate_attempt_settlement(
            _set(_record("attemptSettlementFailed"), "outcome", "succeeded")
        )


def test_cancellation_request_acknowledgement_and_settlement_are_separate() -> None:
    requested_with_settlement = _set(
        _record("cancellationRecordRequestedOnly"),
        "finalSettlement",
        _record("attemptSettlementFailed"),
    )
    with pytest.raises(ContractSemanticError, match="request alone is not final settlement"):
        workflow.validate_cancellation_record(requested_with_settlement)

    with pytest.raises(ContractSemanticError, match="requires providerAcknowledgement"):
        workflow.validate_cancellation_record(
            _delete(_record("cancellationRecordSettled"), "providerAcknowledgement")
        )


def test_artifact_receipt_separates_deterministic_checks_from_visual_review() -> None:
    skipped = _record("artifactReceiptSkippedVisual")
    assert skipped["visual_review"] == "skipped"
    assert skipped["deterministicChecks"][0]["result"] == "passed"
    workflow.validate_workflow_artifact_receipt(skipped)

    with pytest.raises(ContractSemanticError, match="visual_review is not one"):
        workflow.validate_workflow_artifact_receipt(
            _set(_record("artifactReceiptSkippedVisual"), "visual_review", "implicitly_passed")
        )
    with pytest.raises(ContractSemanticError, match="deliveredAtomically must be true"):
        workflow.validate_workflow_artifact_receipt(
            _set(_record("artifactReceiptPassedVisual"), "deliveredAtomically", False)
        )


def test_migration_receipt_produces_a_new_draft_or_version_without_rewriting() -> None:
    with pytest.raises(ContractSemanticError, match="rewrittenPublishedVersionInPlace must be false"):
        workflow.validate_migration_receipt(
            _set(_record("migrationReceiptDraft"), "rewrittenPublishedVersionInPlace", True)
        )
    with pytest.raises(ContractSemanticError, match="exactly one"):
        workflow.validate_migration_receipt(_delete(_record("migrationReceiptDraft"), "producedDraft"))
    with pytest.raises(ContractSemanticError, match="exactly one"):
        workflow.validate_migration_receipt(
            _set(_record("migrationReceiptDraft"), "producedVersion", {"versionId": "new-version"})
        )
    with pytest.raises(ContractSemanticError, match="must not rewrite sourceVersion"):
        workflow.validate_migration_receipt(
            _set(
                _delete(_record("migrationReceiptDraft"), "producedDraft"),
                "producedVersion",
                {"versionId": "workflow-version-atlas-1"},
            )
        )


def test_workflow_version_diff_is_derived_and_does_not_claim_runtime_causality() -> None:
    with pytest.raises(ContractSemanticError, match="runtimeCausalityClaimed must be false"):
        workflow.validate_workflow_version_diff(
            _set(_record("workflowVersionDiff"), "runtimeCausalityClaimed", True)
        )
    with pytest.raises(ContractSemanticError, match="classifications\\[0\\]"):
        workflow.validate_workflow_version_diff(
            _set(_record("workflowVersionDiff"), "classifications", ["runtime_cause"])
        )
    with pytest.raises(ContractSemanticError, match="must be distinct"):
        workflow.validate_workflow_version_diff(
            _set(
                _record("workflowVersionDiff"),
                "toVersion",
                {"versionId": "workflow-version-atlas-1"},
            )
        )
