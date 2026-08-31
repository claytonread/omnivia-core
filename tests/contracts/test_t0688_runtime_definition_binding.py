"""Clean-room tests for the T-0688 IP-06 `RuntimeDefinitionBinding` contract lane.

`RuntimeDefinitionBinding` is the immutable, content-addressed pin a Runtime Run executes
against. It is deliberately not a T-0679 `contractName`-tagged record, so these tests are kept
separate from `test_workflow_hardening_contracts.py` rather than added to its fixture-backed
suite.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnivia_core.contracts.v1 import semantics_workflow as workflow
from omnivia_core.contracts.v1.compatibility import ContractSemanticError

_DIGEST = "sha256:" + "a" * 64
_OTHER_DIGEST = "sha256:" + "b" * 64


def _binding(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "bindingSchemaVersion": "1.0.0",
        "bindingId": "binding-atlas-1",
        "workflowId": "workflow-atlas",
        "workflowVersion": "3.2.1",
        "releaseRef": {"releaseId": "release-atlas-9"},
        "definitionDigest": _DIGEST,
        "executionProfileDigest": _DIGEST,
        "effectivePolicyDigest": _DIGEST,
        "componentImplementationDigests": {"component-fetch": _DIGEST},
        "resourceBindingSnapshots": [
            {
                "resourceRequirementId": "requirement-db",
                "resourceRef": {"resourceId": "resource-db-1"},
                "snapshotRef": {"snapshotId": "snapshot-db-1"},
                "snapshotDigest": _DIGEST,
            }
        ],
        "boundAt": "2026-01-01T00:00:00Z",
        "boundBy": {"actorId": "actor-runtime"},
    }
    record.update(overrides)
    return record


def _projection(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "runId": "run-atlas-1",
        "legacyBinding": False,
        "bindingRef": {"bindingId": "binding-atlas-1"},
    }
    record.update(overrides)
    return record


def _resume(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "decision": "allow",
        "runId": "run-atlas-1",
        "evidence": {"evidenceId": "evidence-atlas-1"},
    }
    record.update(overrides)
    return record


# --------------------------------------------------------------------------
# RuntimeDefinitionBinding: complete shape, model pair, and fixture alias
# --------------------------------------------------------------------------


def test_complete_binding_with_model_pair_validates() -> None:
    binding = _binding(
        modelPolicySnapshotRef={"snapshotId": "model-snapshot-1"},
        modelPolicySnapshotDigest=_OTHER_DIGEST,
    )
    workflow.validate_immutable_execution_binding(binding)


def test_binding_without_model_pair_validates() -> None:
    workflow.validate_immutable_execution_binding(_binding())


def test_fixture_facing_alias_is_the_same_validator_and_never_registered() -> None:
    assert workflow.validate_runtime_definition_binding is workflow.validate_immutable_execution_binding
    workflow.validate_runtime_definition_binding(_binding())
    assert "RuntimeDefinitionBinding" not in workflow.WORKFLOW_RECORD_VALIDATORS
    with pytest.raises(ContractSemanticError, match="not a known T-0679 contract"):
        workflow.validate_workflow_record(_binding(contractName="RuntimeDefinitionBinding"))


def test_unknown_fields_are_refused() -> None:
    for field in ("contractName", "legacyBinding", "partial", "degraded"):
        with pytest.raises(ContractSemanticError, match="unknown fields"):
            workflow.validate_immutable_execution_binding(_binding(**{field: "x"}))


# --------------------------------------------------------------------------
# bindingSchemaVersion
# --------------------------------------------------------------------------


def test_unsupported_schema_major_is_refused() -> None:
    with pytest.raises(ContractSemanticError, match="major version 2 is not supported"):
        workflow.validate_immutable_execution_binding(_binding(bindingSchemaVersion="2.0.0"))


@pytest.mark.parametrize("malformed", ["1.0", "1", "v1.0.0", "1.x.0", ""])
def test_malformed_schema_version_is_refused(malformed: str) -> None:
    with pytest.raises(ContractSemanticError, match="not a well-formed exact ReleaseVersion"):
        workflow.validate_immutable_execution_binding(_binding(bindingSchemaVersion=malformed))


# --------------------------------------------------------------------------
# workflowVersion: exact ReleaseVersion, not a range or floating form
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "floating", ["^3.2.1", "~3.2.1", ">=3.2.1", "3.2.x", "3.x", "latest", "3.2"]
)
def test_floating_or_ranged_workflow_version_is_refused(floating: str) -> None:
    with pytest.raises(ContractSemanticError, match="not a well-formed exact ReleaseVersion"):
        workflow.validate_immutable_execution_binding(_binding(workflowVersion=floating))


def test_exact_workflow_version_is_accepted() -> None:
    workflow.validate_immutable_execution_binding(_binding(workflowVersion="10.0.0-rc.1+build.5"))


# --------------------------------------------------------------------------
# releaseRef: an exact Release reference, not a release-version scalar
# --------------------------------------------------------------------------


def test_release_ref_accepts_a_non_empty_reference_mapping() -> None:
    workflow.validate_immutable_execution_binding(
        _binding(releaseRef={"releaseId": "release-atlas-9", "channel": "stable"})
    )


def test_release_ref_rejects_a_scalar_release_version() -> None:
    with pytest.raises(ContractSemanticError, match="expected a mapping"):
        workflow.validate_immutable_execution_binding(_binding(releaseRef="3.2.1"))


def test_release_ref_rejects_an_empty_reference() -> None:
    with pytest.raises(ContractSemanticError, match="releaseRef must not be empty"):
        workflow.validate_immutable_execution_binding(_binding(releaseRef={}))


# --------------------------------------------------------------------------
# componentImplementationDigests
# --------------------------------------------------------------------------


def test_empty_component_map_is_refused() -> None:
    with pytest.raises(ContractSemanticError, match="componentImplementationDigests must not be empty"):
        workflow.validate_immutable_execution_binding(_binding(componentImplementationDigests={}))


def test_malformed_component_id_is_refused() -> None:
    with pytest.raises(ContractSemanticError, match="Component Identifier"):
        workflow.validate_immutable_execution_binding(
            _binding(componentImplementationDigests={"": _DIGEST})
        )


def test_malformed_component_digest_is_refused() -> None:
    with pytest.raises(ContractSemanticError, match="not a well-formed Digest"):
        workflow.validate_immutable_execution_binding(
            _binding(componentImplementationDigests={"component-fetch": "not-a-digest"})
        )


# --------------------------------------------------------------------------
# resourceBindingSnapshots: structurally optional, closed shape, no duplicates
# --------------------------------------------------------------------------


def test_empty_resource_binding_snapshots_is_structurally_allowed() -> None:
    workflow.validate_immutable_execution_binding(_binding(resourceBindingSnapshots=[]))


def test_malformed_resource_binding_snapshot_member_is_refused() -> None:
    with pytest.raises(ContractSemanticError, match="unknown fields"):
        workflow.validate_immutable_execution_binding(
            _binding(
                resourceBindingSnapshots=[
                    {
                        "resourceRequirementId": "requirement-db",
                        "resourceRef": {"resourceId": "resource-db-1"},
                        "snapshotRef": {"snapshotId": "snapshot-db-1"},
                        "snapshotDigest": _DIGEST,
                        "extra": "field",
                    }
                ]
            )
        )


def test_duplicate_resource_requirement_ids_are_refused() -> None:
    snapshot = {
        "resourceRequirementId": "requirement-db",
        "resourceRef": {"resourceId": "resource-db-1"},
        "snapshotRef": {"snapshotId": "snapshot-db-1"},
        "snapshotDigest": _DIGEST,
    }
    with pytest.raises(ContractSemanticError, match="must not repeat a resourceRequirementId"):
        workflow.validate_immutable_execution_binding(
            _binding(resourceBindingSnapshots=[snapshot, dict(snapshot)])
        )


# --------------------------------------------------------------------------
# modelPolicySnapshotRef / modelPolicySnapshotDigest: half-present is refused
# --------------------------------------------------------------------------


def test_half_present_model_pair_is_refused() -> None:
    with pytest.raises(ContractSemanticError, match="must both be present or both be absent"):
        workflow.validate_immutable_execution_binding(
            _binding(modelPolicySnapshotRef={"snapshotId": "model-snapshot-1"})
        )
    with pytest.raises(ContractSemanticError, match="must both be present or both be absent"):
        workflow.validate_immutable_execution_binding(
            _binding(modelPolicySnapshotDigest=_OTHER_DIGEST)
        )


# --------------------------------------------------------------------------
# RuntimeDefinitionBindingProjection
# --------------------------------------------------------------------------


def test_legacy_projection_with_absent_binding_ref_validates() -> None:
    workflow.validate_runtime_definition_binding_projection(
        {"runId": "run-atlas-1", "legacyBinding": True}
    )


def test_legacy_projection_accepts_heterogeneous_exact_reference_mappings() -> None:
    workflow.validate_runtime_definition_binding_projection(
        {
            "runId": "run-atlas-1",
            "legacyBinding": True,
            "historicalExactRefs": [
                {"versionId": "workflow-version-atlas-1"},
                {"releaseId": "release-atlas-9"},
                {"digest": _DIGEST},
                {"componentId": "component-fetch", "digest": _OTHER_DIGEST},
            ],
        }
    )


def test_legacy_projection_rejects_a_bindingRef() -> None:
    with pytest.raises(ContractSemanticError, match="a legacy projection names no bindingRef"):
        workflow.validate_runtime_definition_binding_projection(
            {
                "runId": "run-atlas-1",
                "legacyBinding": True,
                "bindingRef": {"bindingId": "binding-atlas-1"},
            }
        )


def test_legacy_projection_rejects_a_scalar_historical_ref() -> None:
    with pytest.raises(ContractSemanticError, match="expected a mapping"):
        workflow.validate_runtime_definition_binding_projection(
            {
                "runId": "run-atlas-1",
                "legacyBinding": True,
                "historicalExactRefs": ["3.2.1"],
            }
        )


def test_legacy_projection_rejects_an_empty_historical_ref() -> None:
    with pytest.raises(ContractSemanticError, match="historicalExactRefs\\[0\\] must not be empty"):
        workflow.validate_runtime_definition_binding_projection(
            {"runId": "run-atlas-1", "legacyBinding": True, "historicalExactRefs": [{}]}
        )


def test_non_legacy_projection_requires_binding_ref() -> None:
    with pytest.raises(ContractSemanticError, match="missing bindingRef"):
        workflow.validate_runtime_definition_binding_projection(
            {"runId": "run-atlas-1", "legacyBinding": False}
        )
    workflow.validate_runtime_definition_binding_projection(_projection())


def test_non_legacy_projection_rejects_historical_refs() -> None:
    with pytest.raises(ContractSemanticError, match="a non-legacy projection names no historicalExactRefs"):
        workflow.validate_runtime_definition_binding_projection(
            _projection(historicalExactRefs=[{"releaseId": "release-atlas-9"}])
        )


def test_projection_rejects_invalid_cross_branch_fields() -> None:
    with pytest.raises(ContractSemanticError, match="unknown fields"):
        workflow.validate_runtime_definition_binding_projection(
            _projection() | {"legacyRunId": "run-atlas-1"}
        )


# --------------------------------------------------------------------------
# RuntimeBindingResumeDecision
# --------------------------------------------------------------------------


def test_valid_allow_refuse_and_reconcile_decisions() -> None:
    workflow.validate_runtime_binding_resume_decision(_resume(decision="allow"))
    workflow.validate_runtime_binding_resume_decision(
        _resume(decision="refuse", diagnostic="RT_BINDING_DRIFT")
    )
    workflow.validate_runtime_binding_resume_decision(
        _resume(
            decision="reconcile",
            decidingActor={"actorId": "actor-operator"},
            reason="drift reviewed and accepted",
            outcome="restore_exact",
        )
    )


@pytest.mark.parametrize("diagnostic", ["RT_BINDING_DRIFT", "RT_BINDING_REVOKED"])
def test_refuse_diagnostic_accepts_only_the_two_closed_codes(diagnostic: str) -> None:
    workflow.validate_runtime_binding_resume_decision(_resume(decision="refuse", diagnostic=diagnostic))


def test_refuse_diagnostic_rejects_any_other_code() -> None:
    with pytest.raises(ContractSemanticError, match="not one of"):
        workflow.validate_runtime_binding_resume_decision(
            _resume(decision="refuse", diagnostic="RT_BINDING_OTHER")
        )


def test_reconcile_requires_actor_reason_and_outcome_together() -> None:
    base = _resume(decision="reconcile")
    with pytest.raises(ContractSemanticError, match="missing decidingActor"):
        workflow.validate_runtime_binding_resume_decision(base)
    with pytest.raises(ContractSemanticError, match="missing reason"):
        workflow.validate_runtime_binding_resume_decision(
            {**base, "decidingActor": {"actorId": "actor-operator"}}
        )
    with pytest.raises(ContractSemanticError, match="missing outcome"):
        workflow.validate_runtime_binding_resume_decision(
            {
                **base,
                "decidingActor": {"actorId": "actor-operator"},
                "reason": "drift reviewed",
            }
        )


def test_reconcile_rejects_a_replacement_binding_or_digest() -> None:
    complete = _resume(
        decision="reconcile",
        decidingActor={"actorId": "actor-operator"},
        reason="drift reviewed and accepted",
        outcome="restore_exact",
    )
    with pytest.raises(ContractSemanticError, match="unknown fields"):
        workflow.validate_runtime_binding_resume_decision(
            {**complete, "replacementBinding": {"bindingId": "binding-atlas-2"}}
        )
    with pytest.raises(ContractSemanticError, match="unknown fields"):
        workflow.validate_runtime_binding_resume_decision(
            {**complete, "replacementDigest": _OTHER_DIGEST}
        )


def test_allow_and_refuse_decisions_forbid_reconciliation_fields() -> None:
    with pytest.raises(ContractSemanticError, match="an allow decision names no diagnostic"):
        workflow.validate_runtime_binding_resume_decision(
            _resume(decision="allow", diagnostic="RT_BINDING_DRIFT")
        )
    with pytest.raises(ContractSemanticError, match="a refuse decision names no decidingActor"):
        workflow.validate_runtime_binding_resume_decision(
            _resume(
                decision="refuse",
                diagnostic="RT_BINDING_DRIFT",
                decidingActor={"actorId": "actor-operator"},
            )
        )
