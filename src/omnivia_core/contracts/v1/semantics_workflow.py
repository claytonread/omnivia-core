"""Semantic validation for the T-0679 Workflow hardening records.

The records validated here are the OmniVia-authored contract names ratified in
DOC-004 Appendix AB. They intentionally validate plain wire mappings rather than
generated dataclasses: the G4 slice is a small additive Core oracle for fixture
and runtime-readiness semantics, not a generated schema rollout.

Standard library only. Nothing in this module may depend on runtime, storage,
HTTP, MCP, CLI, Platform, Dev, or a validation framework.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from hashlib import sha256
from types import MappingProxyType
from typing import Final

from omnivia_core.contracts.v1.canonical_json import canonical_bytes
from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.generated import (
    is_content_checksum,
    is_identifier,
    is_open_code,
    is_release_version,
    is_timestamp,
)

__all__ = [
    "ATTEMPT_STATES",
    "BOUNDARY_AGGREGATION_OUTCOMES",
    "BRANCH_AGGREGATE_MODES",
    "COMPONENT_IMPLEMENTATION_BINDING_STATES",
    "EDIT_OPERATION_KINDS",
    "EFFECT_DISPOSITIONS",
    "EFFECT_SETTLEMENT_CLASSES",
    "LOOP_CANCELLATION_POLICIES",
    "LOOP_DONE_CONDITION_KINDS",
    "LOOP_ITERATION_IDENTITY_RULE_KINDS",
    "LOOP_ITERATION_OUTCOME_CLASSES",
    "LOOP_LATE_RESULT_POLICIES",
    "LOOP_MODES",
    "LOOP_ORDER_GUARANTEES",
    "LOOP_PARTIAL_SUCCESS_POLICIES",
    "LOOP_SOURCE_KINDS",
    "LOOP_ZIP_MISMATCH_POLICIES",
    "NESTED_BOUNDARY_PUBLICATION_POSTURES",
    "NESTED_BOUNDARY_REFERENCE_KINDS",
    "NESTED_BOUNDARY_ROLLOUT_STAGES",
    "PORT_DELIVERY_MODES",
    "PORT_DIRECTIONS",
    "PORT_FAN_OUT_POLICIES",
    "RUNTIME_BINDING_RECONCILE_OUTCOMES",
    "RUNTIME_BINDING_REFUSAL_DIAGNOSTICS",
    "RUNTIME_BINDING_RESUME_DECISIONS",
    "SELECTIVE_APPLY_DISPOSITIONS",
    "VISUAL_REVIEW_STATES",
    "WF_LOOP_PLAN_INVALID",
    "WF_NESTED_BOUNDARY_BYPASS",
    "WORKFLOW_DIFF_CLASSES",
    "WORKFLOW_RECORD_VALIDATORS",
    "WORKFLOW_VALUE_CARDINALITIES",
    "WORKFLOW_VALUE_PRESENCES",
    "WorkflowRecordValidator",
    "compute_transition_bundle_payload_digest",
    "compute_workflow_edit_batch_payload_digest",
    "evaluate_nested_workflow_boundary",
    "evaluate_workflow_edit_batch",
    "validate_absent_value",
    "validate_attempt_settlement",
    "validate_branch_aggregate_policy",
    "validate_cancellation_record",
    "validate_complete_loop_plan",
    "validate_component_implementation_binding",
    "validate_component_port_contract",
    "validate_component_port_set",
    "validate_immutable_execution_binding",
    "validate_loop_iteration_ledger",
    "validate_loop_plan",
    "validate_migration_receipt",
    "validate_nested_boundary_exception",
    "validate_nested_workflow_boundary",
    "validate_owir_source_projection",
    "validate_owir_source_projection_matches_revision",
    "validate_runtime_binding_resume_decision",
    "validate_runtime_definition_binding",
    "validate_runtime_definition_binding_projection",
    "validate_runtime_journal_event",
    "validate_simulation_result",
    "validate_start_workflow_run_readiness",
    "validate_suggested_workflow_fix",
    "validate_transition_bundle",
    "validate_workflow_artifact_receipt",
    "validate_workflow_check_readiness_extension",
    "validate_workflow_edit_batch",
    "validate_workflow_edit_operation",
    "validate_workflow_record",
    "validate_workflow_value",
    "validate_workflow_version_diff",
]

WorkflowRecordValidator = Callable[[object], None]

WORKFLOW_VALUE_PRESENCES: Final[tuple[str, ...]] = (
    "present",
    "absent",
    "null_value",
    "empty",
    "redacted",
    "unavailable",
    "failed",
)
WORKFLOW_VALUE_CARDINALITIES: Final[tuple[str, ...]] = ("single", "many")

COMPONENT_IMPLEMENTATION_BINDING_STATES: Final[tuple[str, ...]] = (
    "available",
    "missing",
    "revoked",
    "incompatible",
)

FIX_PRECONDITION_KINDS: Final[tuple[str, ...]] = (
    "expected_revision",
    "contract_available",
    "permission_available",
    "diagnostic_still_present",
)
POST_APPLY_CHECK_PROFILES: Final[tuple[str, ...]] = (
    "draft_save",
    "version_creation",
    "publication",
    "runtime_load",
)

BRANCH_AGGREGATE_MODES: Final[tuple[str, ...]] = (
    "all",
    "any",
    "threshold",
    "first_deterministic",
)
LOOP_LATE_RESULT_POLICIES: Final[tuple[str, ...]] = (
    "ignore_with_evidence",
    "record_only",
    "fail_if_material",
    "indeterminate_if_material",
)

EFFECT_DISPOSITIONS: Final[tuple[str, ...]] = (
    "none",
    "succeeded",
    "failed",
    "cancelled",
    "compensated",
    "indeterminate",
)
ATTEMPT_STATES: Final[tuple[str, ...]] = (
    "created",
    "leased",
    "running",
    "succeeded",
    "failed",
    "timed_out",
    "cancelled",
    "lost",
    "indeterminate",
)
CANCELLATION_IN_FLIGHT_STATES: Final[tuple[str, ...]] = (
    "requested",
    "acknowledged",
    "not_supported",
    "completed_before_cancel",
    "indeterminate",
)

VISUAL_REVIEW_STATES: Final[tuple[str, ...]] = ("passed", "skipped", "failed")
ARTIFACT_CHECK_RESULTS: Final[tuple[str, ...]] = ("passed", "failed")

WORKFLOW_DIFF_CLASSES: Final[tuple[str, ...]] = (
    "topology",
    "semantic",
    "geometry",
    "presentation",
    "evidence_only",
)

_VALUE_FIELDS: Final = frozenset(
    {
        "contractName",
        "valueId",
        "semanticType",
        "physicalSchema",
        "cardinality",
        "presence",
        "classification",
        "lineage",
        "valueDigest",
        "value",
        "diagnostic",
        "evidence",
    }
)
_ABSENT_FIELDS: Final = frozenset(
    {"contractName", "kind", "reason", "subject", "provenance", "diagnostic", "value"}
)
_BINDING_FIELDS: Final = frozenset(
    {
        "bindingId",
        "workflowComponentId",
        "component",
        "implementation",
        "signatureDigest",
        "capability",
        "state",
        "diagnostics",
    }
)
_CHECK_FIELDS: Final = frozenset(
    {"contractName", "definitionValid", "runnable", "implementationBindings", "diagnostics"}
)
_FIX_FIELDS: Final = frozenset(
    {
        "contractName",
        "suggestedFixId",
        "diagnosticCode",
        "subject",
        "preconditions",
        "previewDiff",
        "editBatch",
        "postApplyCheckProfile",
        "undoOperation",
        "authorityCeiling",
        "evidence",
    }
)
_SIMULATION_FIELDS: Final = frozenset(
    {
        "contractName",
        "simulationId",
        "workflow",
        "scope",
        "effectMode",
        "reachedTargets",
        "notReachedTargets",
        "output",
        "diagnostics",
        "evidence",
        "browserEnvelope",
        "productionReadinessContribution",
    }
)
_BROWSER_ENVELOPE_FIELDS: Final = frozenset(
    {"attempt", "resolvedTargets", "observations", "effectsPerformed"}
)
_BRANCH_POLICY_FIELDS: Final = frozenset(
    {"contractName", "policyId", "branchGroupId", "mode", "threshold", "lateResultPolicy"}
)
_LOOP_PLAN_FIELDS: Final = frozenset(
    {
        "contractName",
        "loopPlanId",
        "loopComponentId",
        "frozenAtRunStart",
        "maximumIterations",
        "concurrencyLimit",
        "deterministicOrder",
        "iterationLedgerRequired",
    }
)
_ATTEMPT_SETTLEMENT_FIELDS: Final = frozenset(
    {
        "contractName",
        "attemptId",
        "outcome",
        "effectDisposition",
        "diagnostic",
        "reconciliation",
        "settledAt",
    }
)
_CANCELLATION_FIELDS: Final = frozenset(
    {
        "contractName",
        "cancellationId",
        "workflowRunId",
        "requestedAt",
        "requestEvidence",
        "providerAcknowledgement",
        "inFlightState",
        "finalSettlement",
    }
)
_ARTIFACT_RECEIPT_FIELDS: Final = frozenset(
    {
        "contractName",
        "receiptId",
        "sourceDigest",
        "artifactDigest",
        "deterministicChecks",
        "visual_review",
        "deliveredAtomically",
        "evidence",
    }
)
_ARTIFACT_CHECK_FIELDS: Final = frozenset({"checkId", "result", "diagnostic"})
_MIGRATION_FIELDS: Final = frozenset(
    {
        "contractName",
        "receiptId",
        "migrationRunId",
        "sourceVersion",
        "producedDraft",
        "producedVersion",
        "idempotencyKey",
        "rewrittenPublishedVersionInPlace",
        "evidence",
    }
)
_VERSION_DIFF_FIELDS: Final = frozenset(
    {
        "contractName",
        "diffId",
        "fromVersion",
        "toVersion",
        "stableIdMap",
        "classifications",
        "changes",
        "runtimeCausalityClaimed",
    }
)

# --- T-0688 IP-06: RuntimeDefinitionBinding public contract semantics ---------
#
# `RuntimeDefinitionBinding` is deliberately not a T-0679 `contractName`-tagged record: it is
# the immutable, content-addressed pin a Runtime Run executes against, not a mutable Workflow
# authoring surface object. Carrying `contractName` (or a `legacyBinding`/partial-degraded
# marker) would let a caller mistake it for one of the mutable records above, so those fields
# are refused as unknown rather than merely left unchecked. It is therefore validated and
# exposed directly and is deliberately not entered into `WORKFLOW_RECORD_VALIDATORS`: that
# registry dispatches on `contractName`, and a real binding payload can never carry one.
#
# Field representation follows the vocabulary already established elsewhere in this module and
# in :mod:`semantics_runtime`: an `*Id` field is a canonical `Identifier`
# (:func:`is_identifier`); a `*Digest` field is a content digest (:func:`is_content_checksum`);
# `workflowVersion` names an exact, non-floating build and is a `ReleaseVersion`
# (:func:`is_release_version`), the same scalar
# :class:`semantics_runtime.RunDefinitionRef.definition_version` already uses for an exact
# pinned version; `releaseRef` names a Release by pointer, not by version scalar, so it is a
# non-empty `Reference` mapping (:func:`_reference`) like `resourceRef`, `snapshotRef`,
# `modelPolicySnapshotRef`, `boundBy`, `bindingRef`, `evidence`, and the reconciling actor, and
# exactly as `component`, `implementation`, and `capability` already are in
# `ComponentImplementationBinding` above; and each entry of `historicalExactRefs` is likewise a
# non-empty `Reference` mapping rather than a `ReleaseVersion`, because a caller-proven exact
# pin may itself name a Workflow Version, a Release, a definition digest, or a Component
# implementation digest -- this projection only carries what the caller already proved, it does
# not itself constrain which kind of exact reference that was.

_RUNTIME_DEFINITION_BINDING_FIELDS: Final = frozenset(
    {
        "bindingSchemaVersion",
        "bindingId",
        "workflowId",
        "workflowVersion",
        "releaseRef",
        "definitionDigest",
        "executionProfileDigest",
        "effectivePolicyDigest",
        "componentImplementationDigests",
        "resourceBindingSnapshots",
        "modelPolicySnapshotRef",
        "modelPolicySnapshotDigest",
        "boundAt",
        "boundBy",
    }
)
_RESOURCE_BINDING_SNAPSHOT_FIELDS: Final = frozenset(
    {"resourceRequirementId", "resourceRef", "snapshotRef", "snapshotDigest"}
)
_BINDING_SCHEMA_SUPPORTED_MAJORS: Final = frozenset({1})

_PROJECTION_FIELDS: Final = frozenset(
    {"runId", "legacyBinding", "bindingRef", "historicalExactRefs"}
)

_RESUME_DECISION_FIELDS: Final = frozenset(
    {"decision", "runId", "evidence", "diagnostic", "decidingActor", "reason", "outcome"}
)
RUNTIME_BINDING_RESUME_DECISIONS: Final[tuple[str, ...]] = ("allow", "refuse", "reconcile")
RUNTIME_BINDING_REFUSAL_DIAGNOSTICS: Final[tuple[str, ...]] = (
    "RT_BINDING_DRIFT",
    "RT_BINDING_REVOKED",
)
RUNTIME_BINDING_RECONCILE_OUTCOMES: Final[tuple[str, ...]] = (
    "restore_exact",
    "terminate_or_refuse",
    "governed_new_subject",
)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ContractSemanticError(f"{label}: expected a mapping")
    if any(not isinstance(key, str) for key in value):
        raise ContractSemanticError(f"{label}: field names must be strings")
    return value


def _only_fields(fields: Mapping[str, object], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(fields) - allowed)
    if unknown:
        raise ContractSemanticError(f"{label}: unknown fields {unknown!r}")


def _contract(fields: Mapping[str, object], expected: str, label: str) -> None:
    name = fields.get("contractName", expected)
    if name != expected:
        raise ContractSemanticError(f"{label}: contractName must be {expected!r}")


def _present(fields: Mapping[str, object], key: str, label: str) -> object:
    if key not in fields:
        raise ContractSemanticError(f"{label}: missing {key}")
    return fields[key]


def _identifier(fields: Mapping[str, object], key: str, label: str) -> str:
    value = _present(fields, key, label)
    if not is_identifier(value):
        raise ContractSemanticError(f"{label}: {key} is not a well-formed Identifier")
    assert isinstance(value, str)
    return value


def _open_code(fields: Mapping[str, object], key: str, label: str) -> str:
    value = _present(fields, key, label)
    if not is_open_code(value):
        raise ContractSemanticError(f"{label}: {key} is not a well-formed DiagnosticCode")
    assert isinstance(value, str)
    return value


def _digest(fields: Mapping[str, object], key: str, label: str) -> str:
    value = _present(fields, key, label)
    if not is_content_checksum(value):
        raise ContractSemanticError(f"{label}: {key} is not a well-formed Digest")
    assert isinstance(value, str)
    return value


def _timestamp(fields: Mapping[str, object], key: str, label: str) -> str:
    value = _present(fields, key, label)
    if not is_timestamp(value):
        raise ContractSemanticError(f"{label}: {key} is not a well-formed Timestamp")
    assert isinstance(value, str)
    return value


def _boolean(fields: Mapping[str, object], key: str, label: str) -> bool:
    value = _present(fields, key, label)
    if not isinstance(value, bool):
        raise ContractSemanticError(f"{label}: {key} is not a boolean")
    return value


def _string(fields: Mapping[str, object], key: str, label: str) -> str:
    value = _present(fields, key, label)
    if isinstance(value, bool) or not isinstance(value, str) or not value:
        raise ContractSemanticError(f"{label}: {key} is not a non-empty string")
    return value


def _is_empty_value(value: object) -> bool:
    return value == "" or value == [] or value == {}


def _literal_false(fields: Mapping[str, object], key: str, label: str) -> None:
    if _boolean(fields, key, label):
        raise ContractSemanticError(f"{label}: {key} must be false")


def _literal_true(fields: Mapping[str, object], key: str, label: str) -> None:
    if not _boolean(fields, key, label):
        raise ContractSemanticError(f"{label}: {key} must be true")


def _positive_int(fields: Mapping[str, object], key: str, label: str) -> int:
    value = _present(fields, key, label)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractSemanticError(f"{label}: {key} is not a positive integer")
    return value


def _member(fields: Mapping[str, object], key: str, label: str, allowed: tuple[str, ...]) -> str:
    value = _present(fields, key, label)
    if not isinstance(value, str) or value not in allowed:
        raise ContractSemanticError(f"{label}: {key} is not one of {allowed!r}")
    return value


def _sequence(fields: Mapping[str, object], key: str, label: str) -> Sequence[object]:
    value = _present(fields, key, label)
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractSemanticError(f"{label}: {key} is not an array")
    return value


def _optional_sequence(fields: Mapping[str, object], key: str, label: str) -> Sequence[object]:
    if key not in fields:
        return ()
    return _sequence(fields, key, label)


def _reference_value(value: object, label: str, empty_message: str) -> Mapping[str, object]:
    reference = _mapping(value, label)
    if not reference:
        raise ContractSemanticError(empty_message)
    return reference


def _reference(fields: Mapping[str, object], key: str, label: str) -> Mapping[str, object]:
    return _reference_value(
        _present(fields, key, label),
        f"{label}.{key}",
        f"{label}: {key} must not be empty",
    )


def _reference_identity(reference: Mapping[str, object]) -> str | None:
    for key in ("versionId", "draftId", "workflowId", "referenceId", "id"):
        value = reference.get(key)
        if isinstance(value, str):
            return value
    return None


def validate_workflow_value(record: object) -> None:
    """Validate `WorkflowValue` presence without collapsing absence into values."""
    label = "WorkflowValue"
    fields = _mapping(record, label)
    _only_fields(fields, _VALUE_FIELDS, label)
    _contract(fields, "WorkflowValue", label)
    _identifier(fields, "valueId", label)
    _identifier(fields, "semanticType", label)
    _reference(fields, "physicalSchema", label)
    _member(fields, "cardinality", label, WORKFLOW_VALUE_CARDINALITIES)
    presence = _member(fields, "presence", label, WORKFLOW_VALUE_PRESENCES)
    _reference(fields, "classification", label)
    _reference(fields, "lineage", label)
    if "valueDigest" in fields:
        _digest(fields, "valueDigest", label)
    if "evidence" in fields:
        _sequence(fields, "evidence", label)

    if presence == "present" and "value" not in fields:
        raise ContractSemanticError(f"{label}: present values must carry value")
    if presence == "present" and fields["value"] is None:
        raise ContractSemanticError(f"{label}: present values must not carry null")
    if presence == "null_value" and fields.get("value", object()) is not None:
        raise ContractSemanticError(f"{label}: null_value must carry value null")
    if presence == "empty" and not _is_empty_value(fields.get("value", object())):
        raise ContractSemanticError(f"{label}: empty values must carry an empty value")
    if presence == "absent" and "value" in fields:
        raise ContractSemanticError(f"{label}: absent must not carry value")
    if presence in {"redacted", "unavailable", "failed"} and "diagnostic" not in fields:
        raise ContractSemanticError(f"{label}: {presence} requires diagnostic")
    if presence in {"redacted", "unavailable", "failed"} and "value" in fields:
        raise ContractSemanticError(f"{label}: {presence} must not carry value")


def validate_absent_value(record: object) -> None:
    """Validate `AbsentValue` as a distinct union member, not JSON null."""
    label = "AbsentValue"
    fields = _mapping(record, label)
    _only_fields(fields, _ABSENT_FIELDS, label)
    _contract(fields, "AbsentValue", label)
    if _present(fields, "kind", label) != "absent":
        raise ContractSemanticError(f"{label}: kind must be 'absent'")
    _member(
        fields,
        "reason",
        label,
        ("not_provided", "not_reached", "not_applicable", "withheld", "unavailable"),
    )
    _reference(fields, "subject", label)
    _reference(fields, "provenance", label)
    if "value" in fields:
        raise ContractSemanticError(f"{label}: must not carry value")


def validate_component_implementation_binding(record: object) -> str:
    """Validate exact `ComponentImplementationBinding` shape and return its state."""
    label = "ComponentImplementationBinding"
    fields = _mapping(record, label)
    _only_fields(fields, _BINDING_FIELDS, label)
    _identifier(fields, "bindingId", label)
    _identifier(fields, "workflowComponentId", label)
    _reference(fields, "component", label)
    _reference(fields, "implementation", label)
    _digest(fields, "signatureDigest", label)
    _reference(fields, "capability", label)
    state = _member(fields, "state", label, COMPONENT_IMPLEMENTATION_BINDING_STATES)
    diagnostics = _optional_sequence(fields, "diagnostics", label)
    for index, diagnostic in enumerate(diagnostics):
        _mapping(diagnostic, f"{label}.diagnostics[{index}]")
    if state != "available" and not diagnostics:
        raise ContractSemanticError(f"{label}: non-available bindings require diagnostics")
    return state


def validate_workflow_check_readiness_extension(record: object) -> None:
    """Validate `definitionValid` versus `runnable` readiness semantics."""
    label = "WorkflowCheckReadinessExtension"
    fields = _mapping(record, label)
    _only_fields(fields, _CHECK_FIELDS, label)
    _contract(fields, "WorkflowCheckReadinessExtension", label)
    definition_valid = _boolean(fields, "definitionValid", label)
    runnable = _boolean(fields, "runnable", label)
    states = [
        validate_component_implementation_binding(binding)
        for binding in _sequence(fields, "implementationBindings", label)
    ]
    for index, diagnostic in enumerate(_sequence(fields, "diagnostics", label)):
        _mapping(diagnostic, f"{label}.diagnostics[{index}]")
    if runnable and not definition_valid:
        raise ContractSemanticError(f"{label}: runnable requires definitionValid")
    if runnable and any(state != "available" for state in states):
        raise ContractSemanticError(
            f"{label}: runnable requires every ComponentImplementationBinding to be available"
        )


def validate_start_workflow_run_readiness(check_result: object) -> None:
    """Refuse Start Run when the latest applicable Workflow Check is not runnable."""
    validate_workflow_check_readiness_extension(check_result)
    fields = _mapping(check_result, "WorkflowCheckReadinessExtension")
    if not fields["runnable"]:
        raise ContractSemanticError("StartWorkflowRun: latest applicable Workflow Check is not runnable")


def validate_suggested_workflow_fix(record: object) -> None:
    """Validate that `SuggestedWorkflowFix` remains proposal-only."""
    label = "SuggestedWorkflowFix"
    fields = _mapping(record, label)
    _only_fields(fields, _FIX_FIELDS, label)
    _contract(fields, "SuggestedWorkflowFix", label)
    _identifier(fields, "suggestedFixId", label)
    _open_code(fields, "diagnosticCode", label)
    _reference(fields, "subject", label)
    for index, precondition in enumerate(_sequence(fields, "preconditions", label)):
        precondition_fields = _mapping(precondition, f"{label}.preconditions[{index}]")
        _member(precondition_fields, "kind", f"{label}.preconditions[{index}]", FIX_PRECONDITION_KINDS)
        _reference(precondition_fields, "reference", f"{label}.preconditions[{index}]")
    validate_workflow_version_diff(_present(fields, "previewDiff", label))
    edit_batch = _reference(fields, "editBatch", label)
    _identifier(edit_batch, "editBatchId", f"{label}.editBatch")
    _identifier(edit_batch, "semanticRevision", f"{label}.editBatch")
    _member(fields, "postApplyCheckProfile", label, POST_APPLY_CHECK_PROFILES)
    if "undoOperation" in fields:
        undo = _reference(fields, "undoOperation", label)
        _identifier(undo, "editBatchId", f"{label}.undoOperation")
        _identifier(undo, "semanticRevision", f"{label}.undoOperation")
    if _present(fields, "authorityCeiling", label) != "proposal_only":
        raise ContractSemanticError(f"{label}: authorityCeiling must be proposal_only")
    if "evidence" in fields:
        _sequence(fields, "evidence", label)


def validate_simulation_result(record: object) -> None:
    """Validate Simulation isolation from production readiness and effects."""
    label = "SimulationResult"
    fields = _mapping(record, label)
    _only_fields(fields, _SIMULATION_FIELDS, label)
    _contract(fields, "SimulationResult", label)
    _identifier(fields, "simulationId", label)
    _reference(fields, "workflow", label)
    _reference(fields, "scope", label)
    _string(fields, "effectMode", label)
    _sequence(fields, "reachedTargets", label)
    _sequence(fields, "notReachedTargets", label)
    if "output" in fields:
        validate_workflow_value(_present(fields, "output", label))
    _sequence(fields, "diagnostics", label)
    if "evidence" in fields:
        _sequence(fields, "evidence", label)
    if "browserEnvelope" in fields:
        envelope_label = "BrowserScopedSimulationEnvelope"
        envelope = _mapping(fields["browserEnvelope"], envelope_label)
        _only_fields(envelope, _BROWSER_ENVELOPE_FIELDS, envelope_label)
        _sequence(envelope, "resolvedTargets", envelope_label)
        _sequence(envelope, "observations", envelope_label)
        _literal_false(envelope, "effectsPerformed", envelope_label)
    _literal_false(fields, "productionReadinessContribution", label)


def validate_branch_aggregate_policy(record: object) -> None:
    """Validate frozen branch aggregation policy semantics."""
    label = "BranchAggregatePolicy"
    fields = _mapping(record, label)
    _only_fields(fields, _BRANCH_POLICY_FIELDS, label)
    _contract(fields, "BranchAggregatePolicy", label)
    _identifier(fields, "policyId", label)
    _identifier(fields, "branchGroupId", label)
    mode = _member(fields, "mode", label, BRANCH_AGGREGATE_MODES)
    _member(fields, "lateResultPolicy", label, LOOP_LATE_RESULT_POLICIES)
    if mode == "threshold":
        _positive_int(fields, "threshold", label)
    elif "threshold" in fields:
        raise ContractSemanticError(f"{label}: threshold is only valid for threshold mode")


def validate_loop_plan(record: object) -> None:
    """Validate that a `LoopPlan` is frozen at Run start with an iteration ledger."""
    label = "LoopPlan"
    fields = _mapping(record, label)
    _only_fields(fields, _LOOP_PLAN_FIELDS, label)
    _contract(fields, "LoopPlan", label)
    _identifier(fields, "loopPlanId", label)
    _identifier(fields, "loopComponentId", label)
    _literal_true(fields, "frozenAtRunStart", label)
    maximum_iterations = _positive_int(fields, "maximumIterations", label)
    concurrency_limit = _positive_int(fields, "concurrencyLimit", label)
    if concurrency_limit > maximum_iterations:
        raise ContractSemanticError(f"{label}: concurrencyLimit exceeds maximumIterations")
    order = _sequence(fields, "deterministicOrder", label)
    if not order:
        raise ContractSemanticError(f"{label}: deterministicOrder must not be empty")
    for index, item in enumerate(order):
        if not is_identifier(item):
            raise ContractSemanticError(f"{label}: deterministicOrder[{index}] is not an Identifier")
    _literal_true(fields, "iterationLedgerRequired", label)


def validate_attempt_settlement(record: object) -> None:
    """Validate explicit `AttemptSettlement` and `EffectDisposition` semantics."""
    label = "AttemptSettlement"
    fields = _mapping(record, label)
    _only_fields(fields, _ATTEMPT_SETTLEMENT_FIELDS, label)
    _contract(fields, "AttemptSettlement", label)
    _identifier(fields, "attemptId", label)
    outcome = _member(fields, "outcome", label, ATTEMPT_STATES)
    disposition = _member(fields, "effectDisposition", label, EFFECT_DISPOSITIONS)
    _timestamp(fields, "settledAt", label)
    if disposition == "indeterminate" and outcome != "indeterminate":
        raise ContractSemanticError(f"{label}: indeterminate effects settle as indeterminate")
    if disposition in {"failed", "cancelled"} and outcome == "succeeded":
        raise ContractSemanticError(f"{label}: failed or cancelled effects may not be absorbed as success")


def validate_cancellation_record(record: object) -> None:
    """Validate request, acknowledgement, in-flight state and settlement separation."""
    label = "CancellationRecord"
    fields = _mapping(record, label)
    _only_fields(fields, _CANCELLATION_FIELDS, label)
    _contract(fields, "CancellationRecord", label)
    _identifier(fields, "cancellationId", label)
    _identifier(fields, "workflowRunId", label)
    _timestamp(fields, "requestedAt", label)
    state = _member(fields, "inFlightState", label, CANCELLATION_IN_FLIGHT_STATES)
    final_settlement = fields.get("finalSettlement")
    if final_settlement is not None:
        validate_attempt_settlement(final_settlement)
    if state == "requested" and final_settlement is not None:
        raise ContractSemanticError(f"{label}: a cancellation request alone is not final settlement")
    if state == "acknowledged" and "providerAcknowledgement" not in fields:
        raise ContractSemanticError(f"{label}: acknowledged state requires providerAcknowledgement")


def validate_workflow_artifact_receipt(record: object) -> None:
    """Validate the deterministic-check versus `visual_review` boundary."""
    label = "WorkflowArtifactReceipt"
    fields = _mapping(record, label)
    _only_fields(fields, _ARTIFACT_RECEIPT_FIELDS, label)
    _contract(fields, "WorkflowArtifactReceipt", label)
    _identifier(fields, "receiptId", label)
    _digest(fields, "sourceDigest", label)
    _digest(fields, "artifactDigest", label)
    checks = _sequence(fields, "deterministicChecks", label)
    if not checks:
        raise ContractSemanticError(f"{label}: deterministicChecks must not be empty")
    for index, check in enumerate(checks):
        check_label = f"{label}.deterministicChecks[{index}]"
        check_fields = _mapping(check, check_label)
        _only_fields(check_fields, _ARTIFACT_CHECK_FIELDS, check_label)
        _identifier(check_fields, "checkId", check_label)
        _member(check_fields, "result", check_label, ARTIFACT_CHECK_RESULTS)
    _member(fields, "visual_review", label, VISUAL_REVIEW_STATES)
    _literal_true(fields, "deliveredAtomically", label)


def validate_migration_receipt(record: object) -> None:
    """Validate idempotent migration output without published-version rewrite."""
    label = "MigrationReceipt"
    fields = _mapping(record, label)
    _only_fields(fields, _MIGRATION_FIELDS, label)
    _contract(fields, "MigrationReceipt", label)
    _identifier(fields, "receiptId", label)
    _identifier(fields, "migrationRunId", label)
    source = _reference(fields, "sourceVersion", label)
    _identifier(fields, "idempotencyKey", label)
    _literal_false(fields, "rewrittenPublishedVersionInPlace", label)
    has_draft = "producedDraft" in fields
    has_version = "producedVersion" in fields
    if has_draft == has_version:
        raise ContractSemanticError(f"{label}: exactly one of producedDraft or producedVersion is required")
    produced = _reference(fields, "producedDraft" if has_draft else "producedVersion", label)
    if _reference_identity(source) is not None and _reference_identity(source) == _reference_identity(produced):
        raise ContractSemanticError(f"{label}: produced target must not rewrite sourceVersion in place")


def validate_workflow_version_diff(record: object) -> None:
    """Validate derived diff classification without Runtime causality claims."""
    label = "WorkflowVersionDiff"
    fields = _mapping(record, label)
    _only_fields(fields, _VERSION_DIFF_FIELDS, label)
    _contract(fields, "WorkflowVersionDiff", label)
    _identifier(fields, "diffId", label)
    from_version = _reference(fields, "fromVersion", label)
    to_version = _reference(fields, "toVersion", label)
    if _reference_identity(from_version) is not None and _reference_identity(from_version) == _reference_identity(to_version):
        raise ContractSemanticError(f"{label}: fromVersion and toVersion must be distinct")
    _mapping(_present(fields, "stableIdMap", label), f"{label}.stableIdMap")
    classifications = _sequence(fields, "classifications", label)
    if not classifications:
        raise ContractSemanticError(f"{label}: classifications must not be empty")
    for index, item in enumerate(classifications):
        if not isinstance(item, str) or item not in WORKFLOW_DIFF_CLASSES:
            raise ContractSemanticError(f"{label}: classifications[{index}] is not a WorkflowDiffClass")
    for index, change in enumerate(_sequence(fields, "changes", label)):
        _mapping(change, f"{label}.changes[{index}]")
    _literal_false(fields, "runtimeCausalityClaimed", label)


def _release_version(fields: Mapping[str, object], key: str, label: str) -> str:
    value = _present(fields, key, label)
    if not is_release_version(value):
        raise ContractSemanticError(f"{label}: {key} is not a well-formed exact ReleaseVersion")
    assert isinstance(value, str)
    return value


def _binding_schema_version(fields: Mapping[str, object], key: str, label: str) -> str:
    value = _release_version(fields, key, label)
    major = int(value.split(".", 1)[0])
    if major not in _BINDING_SCHEMA_SUPPORTED_MAJORS:
        raise ContractSemanticError(f"{label}: {key} major version {major} is not supported")
    return value


def _component_implementation_digests(
    fields: Mapping[str, object], key: str, label: str
) -> Mapping[str, object]:
    digests = _mapping(_present(fields, key, label), f"{label}.{key}")
    if not digests:
        raise ContractSemanticError(f"{label}: {key} must not be empty")
    for component_id, digest in digests.items():
        if not is_identifier(component_id):
            raise ContractSemanticError(
                f"{label}: {key} key {component_id!r} is not a well-formed Component Identifier"
            )
        if not is_content_checksum(digest):
            raise ContractSemanticError(f"{label}: {key}[{component_id!r}] is not a well-formed Digest")
    return digests


def _resource_binding_snapshot(entry: object, label: str) -> str:
    fields = _mapping(entry, label)
    _only_fields(fields, _RESOURCE_BINDING_SNAPSHOT_FIELDS, label)
    requirement_id = _identifier(fields, "resourceRequirementId", label)
    _reference(fields, "resourceRef", label)
    _reference(fields, "snapshotRef", label)
    _digest(fields, "snapshotDigest", label)
    return requirement_id


def validate_immutable_execution_binding(record: object) -> None:
    """Validate the T-0688 `RuntimeDefinitionBinding` closed record.

    A binding is a content-addressed pin: every digest, every exact version and every
    Component implementation named here is what a Run actually executed against, not what
    Workflow authoring currently publishes. `resourceBindingSnapshots` may legitimately be
    empty -- a definition that declares no Resource requirements has none to snapshot -- but
    whether *this* definition declares any is a fact this pure, standard-library validator has
    no way to see without the definition itself, so an empty array is accepted structurally and
    that cross-check is left to the Runtime integration lane.
    """
    label = "RuntimeDefinitionBinding"
    fields = _mapping(record, label)
    _only_fields(fields, _RUNTIME_DEFINITION_BINDING_FIELDS, label)
    _binding_schema_version(fields, "bindingSchemaVersion", label)
    _identifier(fields, "bindingId", label)
    _identifier(fields, "workflowId", label)
    _release_version(fields, "workflowVersion", label)
    _reference(fields, "releaseRef", label)
    _digest(fields, "definitionDigest", label)
    _digest(fields, "executionProfileDigest", label)
    _digest(fields, "effectivePolicyDigest", label)
    _component_implementation_digests(fields, "componentImplementationDigests", label)
    requirement_ids = [
        _resource_binding_snapshot(entry, f"{label}.resourceBindingSnapshots[{index}]")
        for index, entry in enumerate(_sequence(fields, "resourceBindingSnapshots", label))
    ]
    if len(requirement_ids) != len(set(requirement_ids)):
        raise ContractSemanticError(
            f"{label}: resourceBindingSnapshots must not repeat a resourceRequirementId"
        )

    has_model_ref = "modelPolicySnapshotRef" in fields
    has_model_digest = "modelPolicySnapshotDigest" in fields
    if has_model_ref != has_model_digest:
        raise ContractSemanticError(
            f"{label}: modelPolicySnapshotRef and modelPolicySnapshotDigest must both be "
            "present or both be absent"
        )
    if has_model_ref:
        _reference(fields, "modelPolicySnapshotRef", label)
        _digest(fields, "modelPolicySnapshotDigest", label)

    _timestamp(fields, "boundAt", label)
    _reference(fields, "boundBy", label)


# The T-0688 fixture-facing name and the canonical contract name are one validator: no wire
# shape distinguishes them, so a second implementation would only be a second place to drift.
validate_runtime_definition_binding = validate_immutable_execution_binding


def validate_runtime_definition_binding_projection(record: object) -> None:
    """Validate the T-0688 `RuntimeDefinitionBindingProjection` closed read-side record.

    `legacyBinding` is the discriminator: a run bound before this projection existed names no
    live `bindingRef` (there is nothing current to point at) and may instead carry
    `historicalExactRefs`, prior exact release pins a caller already proved rather than a
    binding this projection invents on the run's behalf. A non-legacy projection is the
    opposite: it must name its live `bindingRef` and carries no historical list at all.
    """
    label = "RuntimeDefinitionBindingProjection"
    fields = _mapping(record, label)
    _only_fields(fields, _PROJECTION_FIELDS, label)
    _identifier(fields, "runId", label)
    legacy = _boolean(fields, "legacyBinding", label)

    if legacy:
        if "bindingRef" in fields:
            raise ContractSemanticError(f"{label}: a legacy projection names no bindingRef")
        for index, ref in enumerate(_optional_sequence(fields, "historicalExactRefs", label)):
            entry_label = f"{label}.historicalExactRefs[{index}]"
            _reference_value(ref, entry_label, f"{entry_label} must not be empty")
    else:
        if "historicalExactRefs" in fields:
            raise ContractSemanticError(
                f"{label}: a non-legacy projection names no historicalExactRefs"
            )
        _reference(fields, "bindingRef", label)


def validate_runtime_binding_resume_decision(record: object) -> None:
    """Validate the T-0688 `RuntimeBindingResumeDecision` closed record.

    Exactly one of three decisions, and none of them ever carries a replacement binding: this
    is a pure decision about resuming against the binding already recorded, never a channel for
    swapping it. `allow` and `refuse` carry no reconciliation apparatus at all; `refuse` names
    exactly one closed diagnostic; `reconcile` is the only branch with an actor, a reason and an
    outcome, and it requires all three together.
    """
    label = "RuntimeBindingResumeDecision"
    fields = _mapping(record, label)
    _only_fields(fields, _RESUME_DECISION_FIELDS, label)
    decision = _member(fields, "decision", label, RUNTIME_BINDING_RESUME_DECISIONS)
    _identifier(fields, "runId", label)
    _reference(fields, "evidence", label)

    if decision == "allow":
        for forbidden in ("diagnostic", "decidingActor", "reason", "outcome"):
            if forbidden in fields:
                raise ContractSemanticError(f"{label}: an allow decision names no {forbidden}")
    elif decision == "refuse":
        _member(fields, "diagnostic", label, RUNTIME_BINDING_REFUSAL_DIAGNOSTICS)
        for forbidden in ("decidingActor", "reason", "outcome"):
            if forbidden in fields:
                raise ContractSemanticError(f"{label}: a refuse decision names no {forbidden}")
    else:
        if "diagnostic" in fields:
            raise ContractSemanticError(f"{label}: a reconcile decision names no diagnostic")
        _reference(fields, "decidingActor", label)
        _string(fields, "reason", label)
        _member(fields, "outcome", label, RUNTIME_BINDING_RECONCILE_OUTCOMES)


# --- T-0688 IP-07: RuntimeTransitionBundle public contract semantics -------------------
#
# `RuntimeTransitionBundle` is, like `RuntimeDefinitionBinding` above, deliberately not a
# T-0679 `contractName`-tagged record: it is the single write a Runtime Run applies against
# an expected aggregate revision, not a mutable Workflow authoring surface object. It is
# therefore validated and exposed directly and is deliberately kept out of
# `WORKFLOW_RECORD_VALIDATORS`, which dispatches on `contractName` that a real bundle can
# never carry. The unrelated FX-WEFT-BUNDLE red probe (`test_t0688_red_baseline.py`) invents
# a *different* shape under the invented `contractName` `"WorkflowTransitionBundle"`; that
# probe is refused by `validate_workflow_record` on `contractName` alone and this module never
# touches it.
#
# `RuntimeJournalEvent` is the append-only entry a bundle carries. This validator checks the
# one record's own shape and that its `runId` agrees with the enclosing bundle; it cannot
# recompute `previousIntegrityLink` from a genesis link or a predecessor event, because doing
# that requires the persisted Run/event context this pure, standard-library module never has
# access to -- that recomputation belongs to the later storage verifier.
#
# `eventKind` reuses the repository's existing `OpenCode` open wire vocabulary
# (:func:`is_open_code`), the same primitive `RuntimeEvent.event_kind` already validates
# against in :mod:`semantics_runtime`, rather than inventing a second closed enumeration that
# could drift from Runtime Appendix B.

_TRANSITION_BUNDLE_FIELDS: Final = frozenset(
    {
        "bundleSchemaVersion",
        "bundleId",
        "runId",
        "attemptRef",
        "expectedAggregateRevision",
        "event",
        "boundaryResults",
        "activations",
        "schedulingIntents",
        "evidenceRefs",
        "waitConsequences",
        "loopConsequences",
        "effectSettlements",
        "producedAggregateRevision",
        "payloadDigest",
    }
)
_TRANSITION_BUNDLE_SCHEMA_SUPPORTED_MAJORS: Final = frozenset({1})
_TRANSITION_BUNDLE_PLAIN_ARRAY_FIELDS: Final = (
    "boundaryResults",
    "activations",
    "schedulingIntents",
    "waitConsequences",
    "loopConsequences",
)

_RUNTIME_JOURNAL_EVENT_FIELDS: Final = frozenset(
    {
        "eventId",
        "runId",
        "sequence",
        "previousIntegrityLink",
        "eventKind",
        "recordedAt",
        "payloadDigest",
    }
)

_EFFECT_SETTLEMENT_FIELDS: Final = frozenset(
    {"effectRequestId", "settlementClass", "verifiedReceiptRef", "completionContribution"}
)
EFFECT_SETTLEMENT_CLASSES: Final[tuple[str, ...]] = ("committed", "not_committed", "unknown")

_TRANSITION_BUNDLE_DIGEST_ALGORITHM: Final = "sha256"


def _non_negative_int(fields: Mapping[str, object], key: str, label: str) -> int:
    value = _present(fields, key, label)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractSemanticError(f"{label}: {key} is not a non-negative integer")
    return value


def _transition_bundle_schema_version(fields: Mapping[str, object], key: str, label: str) -> str:
    value = _release_version(fields, key, label)
    major = int(value.split(".", 1)[0])
    if major not in _TRANSITION_BUNDLE_SCHEMA_SUPPORTED_MAJORS:
        raise ContractSemanticError(f"{label}: {key} major version {major} is not supported")
    return value


def validate_runtime_journal_event(record: object, *, run_id: str | None = None) -> None:
    """Validate the T-0688 `RuntimeJournalEvent` closed record.

    `run_id`, when given, is the enclosing bundle's `runId`: this event's own `runId` must
    equal it. Genesis-link recomputation and predecessor-chain verification require persisted
    Run/event context this pure validator does not have, so a single well-formed
    `previousIntegrityLink` digest is all that is checked here -- proving the chain itself is
    the storage verifier's job.
    """
    label = "RuntimeJournalEvent"
    fields = _mapping(record, label)
    _only_fields(fields, _RUNTIME_JOURNAL_EVENT_FIELDS, label)
    _identifier(fields, "eventId", label)
    event_run_id = _identifier(fields, "runId", label)
    _non_negative_int(fields, "sequence", label)
    _digest(fields, "previousIntegrityLink", label)
    _open_code(fields, "eventKind", label)
    _timestamp(fields, "recordedAt", label)
    _digest(fields, "payloadDigest", label)
    if run_id is not None and event_run_id != run_id:
        raise ContractSemanticError(f"{label}: runId does not match the enclosing bundle runId")


def _effect_settlement(entry: object, label: str) -> None:
    fields = _mapping(entry, label)
    _only_fields(fields, _EFFECT_SETTLEMENT_FIELDS, label)
    _identifier(fields, "effectRequestId", label)
    settlement_class = _member(fields, "settlementClass", label, EFFECT_SETTLEMENT_CLASSES)
    _reference(fields, "completionContribution", label)
    has_receipt = "verifiedReceiptRef" in fields
    if settlement_class == "committed":
        if not has_receipt:
            raise ContractSemanticError(f"{label}: committed requires verifiedReceiptRef")
        _reference(fields, "verifiedReceiptRef", label)
    elif has_receipt:
        raise ContractSemanticError(f"{label}: {settlement_class} must not carry verifiedReceiptRef")


def compute_transition_bundle_payload_digest(record: object) -> str:
    """Compute the T-0688 `RuntimeTransitionBundle.payloadDigest`.

    The digest covers Core's existing RFC 8785 canonical JSON serialization
    (:func:`canonical_json.canonical_bytes`) of every bundle member except `payloadDigest`
    itself, using the `sha256:` content-checksum convention. It fails closed: a member that
    cannot be canonically serialized (a duplicate key, a non-finite number, anything outside
    the I-JSON value domain) raises `ContractSemanticError` rather than being silently
    coerced or skipped.
    """
    label = "RuntimeTransitionBundle"
    fields = _mapping(record, label)
    payload = {key: value for key, value in fields.items() if key != "payloadDigest"}
    try:
        digest_bytes = canonical_bytes(payload, label)
    except ContractSemanticError as error:
        raise ContractSemanticError(
            f"{label}: payloadDigest cannot be computed: {error}"
        ) from error
    return f"{_TRANSITION_BUNDLE_DIGEST_ALGORITHM}:{sha256(digest_bytes).hexdigest()}"


def validate_transition_bundle(record: object) -> None:
    """Validate the T-0688 `RuntimeTransitionBundle` closed record.

    A bundle is the single write a Runtime Run applies against the aggregate revision it
    expected: `producedAggregateRevision` must be exactly `expectedAggregateRevision + 1`, and
    `payloadDigest` must be the exact recomputation of :func:`compute_transition_bundle_payload_digest`
    over every other member. It carries no `contractName` and is deliberately absent from
    `WORKFLOW_RECORD_VALIDATORS` (see this section's module-level note).
    """
    label = "RuntimeTransitionBundle"
    fields = _mapping(record, label)
    _only_fields(fields, _TRANSITION_BUNDLE_FIELDS, label)
    _transition_bundle_schema_version(fields, "bundleSchemaVersion", label)
    _identifier(fields, "bundleId", label)
    run_id = _identifier(fields, "runId", label)
    if "attemptRef" in fields:
        _reference(fields, "attemptRef", label)
    expected_revision = _non_negative_int(fields, "expectedAggregateRevision", label)
    validate_runtime_journal_event(_present(fields, "event", label), run_id=run_id)

    for key in _TRANSITION_BUNDLE_PLAIN_ARRAY_FIELDS:
        for index, entry in enumerate(_optional_sequence(fields, key, label)):
            entry_label = f"{label}.{key}[{index}]"
            _reference_value(entry, entry_label, f"{entry_label} must not be empty")

    for index, ref in enumerate(_optional_sequence(fields, "evidenceRefs", label)):
        entry_label = f"{label}.evidenceRefs[{index}]"
        _reference_value(ref, entry_label, f"{entry_label} must not be empty")

    for index, settlement in enumerate(_optional_sequence(fields, "effectSettlements", label)):
        _effect_settlement(settlement, f"{label}.effectSettlements[{index}]")

    produced_revision = _non_negative_int(fields, "producedAggregateRevision", label)
    if produced_revision != expected_revision + 1:
        raise ContractSemanticError(
            f"{label}: producedAggregateRevision must be exactly expectedAggregateRevision + 1"
        )

    _digest(fields, "payloadDigest", label)
    expected_digest = compute_transition_bundle_payload_digest(fields)
    if fields["payloadDigest"] != expected_digest:
        raise ContractSemanticError(f"{label}: payloadDigest does not match its recomputation")


# --- T-0688 IP-08: WorkflowEditOperation / WorkflowEditBatch public contract semantics -----
#
# DOC-004 Appendix AD.3 and REF-004 Appendix D.1 define Workflow Definition authoring as
# ordered atomic batches of typed operations against a stable-ID Workflow Draft. Neither
# `WorkflowEditOperation` nor `WorkflowEditBatch` carries `contractName`: like
# `RuntimeDefinitionBinding` and `RuntimeTransitionBundle` above, an edit batch is not a T-0679
# mutable authoring surface object, so it is validated and exposed directly and is deliberately
# kept out of `WORKFLOW_RECORD_VALIDATORS`.
#
# `evaluate_workflow_edit_batch` is a pure, standard-library evaluator: it validates shape,
# then checks `baseRevision` against a caller-supplied `current_revision` and each
# `EditPrecondition` through a caller-supplied `precondition_check` callable, because whether a
# digest or absence actually holds against Draft state is a fact this module has no access to.
# It never mutates its inputs and never partially applies a batch: every precondition of every
# operation is checked before any accepted result is returned (AD.3.3 rules 1-3).

EDIT_OPERATION_KINDS: Final[tuple[str, ...]] = (
    "addElement",
    "removeElement",
    "updateElement",
    "addConnection",
    "removeConnection",
    "updatePort",
    "updateLoopPlan",
    "updateBoundary",
    "updateMetadata",
)
SELECTIVE_APPLY_DISPOSITIONS: Final[tuple[str, ...]] = ("applied", "skipped", "blocked")

_EDIT_PRECONDITION_FIELDS: Final = frozenset(
    {"preconditionKind", "targetStableId", "expectedDigest", "expectedAbsence"}
)
_SEMANTIC_DIFF_FIELDS: Final = frozenset(
    {
        "addedElements",
        "removedElements",
        "changedElements",
        "addedConnections",
        "removedConnections",
        "changedConnections",
        "addedPorts",
        "removedPorts",
        "changedPorts",
        "addedPolicy",
        "removedPolicy",
        "changedPolicy",
    }
)
_COMPENSATION_FIELDS: Final = frozenset({"reviewerAction", "reason"})
_EDIT_OPERATION_FIELDS: Final = frozenset(
    {
        "operationId",
        "targetStableId",
        "operationKind",
        "preconditions",
        "payloadDigest",
        "semanticDiff",
        "inverse",
        "compensation",
        "selectiveApplyDisposition",
        "diagnosticRef",
    }
)
_EDIT_BATCH_FIELDS: Final = frozenset(
    {
        "batchSchemaVersion",
        "batchId",
        "draftRef",
        "baseRevision",
        "operations",
        "batchPayloadDigest",
    }
)

WF_EDIT_STALE_BASE: Final = "WF_EDIT_STALE_BASE"
WF_EDIT_PRECONDITION_FAILED: Final = "WF_EDIT_PRECONDITION_FAILED"


def _edit_precondition(record: object, label: str) -> None:
    fields = _mapping(record, label)
    _only_fields(fields, _EDIT_PRECONDITION_FIELDS, label)
    _string(fields, "preconditionKind", label)
    _identifier(fields, "targetStableId", label)
    has_digest = "expectedDigest" in fields
    has_absence = "expectedAbsence" in fields
    if has_digest == has_absence:
        raise ContractSemanticError(
            f"{label}: exactly one of expectedDigest or expectedAbsence is required"
        )
    if has_digest:
        _digest(fields, "expectedDigest", label)
    else:
        _literal_true(fields, "expectedAbsence", label)


def _stable_id_list(fields: Mapping[str, object], key: str, label: str) -> list[str]:
    ids: list[str] = []
    for index, item in enumerate(_sequence(fields, key, label)):
        if not is_identifier(item):
            raise ContractSemanticError(f"{label}: {key}[{index}] is not an Identifier")
        assert isinstance(item, str)
        ids.append(item)
    if len(ids) != len(set(ids)):
        raise ContractSemanticError(f"{label}: {key} must not repeat a stable ID")
    return ids


def _semantic_diff(record: object, label: str) -> None:
    fields = _mapping(record, label)
    _only_fields(fields, _SEMANTIC_DIFF_FIELDS, label)
    for key in _SEMANTIC_DIFF_FIELDS:
        _stable_id_list(fields, key, label)


def _compensation_descriptor(record: object, label: str) -> None:
    fields = _mapping(record, label)
    _only_fields(fields, _COMPENSATION_FIELDS, label)
    _string(fields, "reviewerAction", label)
    _string(fields, "reason", label)


def _validate_workflow_edit_operation(record: object, label: str, *, allow_inverse: bool) -> None:
    fields = _mapping(record, label)
    _only_fields(fields, _EDIT_OPERATION_FIELDS, label)
    _identifier(fields, "operationId", label)
    _identifier(fields, "targetStableId", label)
    kind = _member(fields, "operationKind", label, EDIT_OPERATION_KINDS)
    preconditions = _sequence(fields, "preconditions", label)
    for index, precondition in enumerate(preconditions):
        _edit_precondition(precondition, f"{label}.preconditions[{index}]")
    if not preconditions and kind != "addElement":
        raise ContractSemanticError(f"{label}: preconditions may be empty only for addElement")
    _digest(fields, "payloadDigest", label)
    _semantic_diff(_present(fields, "semanticDiff", label), f"{label}.semanticDiff")

    has_inverse = "inverse" in fields
    has_compensation = "compensation" in fields
    if not allow_inverse and has_inverse:
        raise ContractSemanticError(f"{label}: an inverse operation must not itself carry an inverse")
    if has_inverse == has_compensation:
        raise ContractSemanticError(f"{label}: exactly one of inverse or compensation is required")
    if has_compensation:
        _compensation_descriptor(_present(fields, "compensation", label), f"{label}.compensation")
    else:
        _validate_workflow_edit_operation(fields["inverse"], f"{label}.inverse", allow_inverse=False)

    disposition = _member(
        fields, "selectiveApplyDisposition", label, SELECTIVE_APPLY_DISPOSITIONS
    )
    if disposition == "blocked":
        _reference(fields, "diagnosticRef", label)
    elif "diagnosticRef" in fields:
        raise ContractSemanticError(f"{label}: {disposition} must not carry diagnosticRef")


def validate_workflow_edit_operation(record: object) -> None:
    """Validate the T-0688 `WorkflowEditOperation` closed record (DOC-004 §AD.3.1)."""
    _validate_workflow_edit_operation(record, "WorkflowEditOperation", allow_inverse=True)


def compute_workflow_edit_batch_payload_digest(record: object) -> str:
    """Compute the T-0688 `WorkflowEditBatch.batchPayloadDigest` (DOC-004 §AD.3.2).

    The digest covers Core's existing RFC 8785 canonical JSON serialization
    (:func:`canonical_json.canonical_bytes`) of exactly `operations`, using the `sha256:`
    content-checksum convention. It fails closed: a payload that cannot be canonically
    serialized raises `ContractSemanticError` rather than being silently coerced or skipped.
    """
    label = "WorkflowEditBatch"
    fields = _mapping(record, label)
    operations = _present(fields, "operations", label)
    try:
        digest_bytes = canonical_bytes(operations, label)
    except ContractSemanticError as error:
        raise ContractSemanticError(
            f"{label}: batchPayloadDigest cannot be computed: {error}"
        ) from error
    return f"sha256:{sha256(digest_bytes).hexdigest()}"


def validate_workflow_edit_batch(record: object) -> None:
    """Validate the T-0688 `WorkflowEditBatch` closed record (DOC-004 §AD.3.2)."""
    label = "WorkflowEditBatch"
    fields = _mapping(record, label)
    _only_fields(fields, _EDIT_BATCH_FIELDS, label)
    _release_version(fields, "batchSchemaVersion", label)
    _identifier(fields, "batchId", label)
    _reference(fields, "draftRef", label)
    _non_negative_int(fields, "baseRevision", label)

    operations = _sequence(fields, "operations", label)
    if not operations:
        raise ContractSemanticError(f"{label}: operations must not be empty")
    operation_ids: list[str] = []
    for index, operation in enumerate(operations):
        operation_label = f"{label}.operations[{index}]"
        _validate_workflow_edit_operation(operation, operation_label, allow_inverse=True)
        operation_fields = _mapping(operation, operation_label)
        operation_ids.append(operation_fields["operationId"])  # type: ignore[arg-type]
    if len(operation_ids) != len(set(operation_ids)):
        raise ContractSemanticError(f"{label}: operations must not repeat an operationId")

    _digest(fields, "batchPayloadDigest", label)
    expected_digest = compute_workflow_edit_batch_payload_digest(fields)
    if fields["batchPayloadDigest"] != expected_digest:
        raise ContractSemanticError(f"{label}: batchPayloadDigest does not match its recomputation")


def evaluate_workflow_edit_batch(
    batch: object,
    *,
    current_revision: int,
    precondition_check: Callable[[Mapping[str, object]], bool],
    workflow_check: Callable[[], bool],
) -> Mapping[str, object]:
    """Evaluate a `WorkflowEditBatch` for atomic acceptance (DOC-004 §AD.3.3, REF-004 §D.1).

    Validates the batch's shape first, then refuses a stale `baseRevision` and any failing
    `EditPrecondition` (as reported by the caller-supplied `precondition_check`) before ever
    returning an accepted description. Never rebases, never merges, never partially applies:
    every precondition of every operation is checked, in order, before this function returns.
    `workflow_check` is invoked exactly once, only after every precondition has passed, and
    still before an accepted description is returned; a false result refuses the whole batch.
    Neither `batch` nor its members are mutated; the returned mapping is a fresh, immutable
    description of the accepted commit.
    """
    validate_workflow_edit_batch(batch)
    label = "WorkflowEditBatch"
    fields = _mapping(batch, label)

    if isinstance(current_revision, bool) or not isinstance(current_revision, int) or current_revision < 0:
        raise ContractSemanticError(f"{label}: current_revision is not a non-negative integer")

    if fields["baseRevision"] != current_revision:
        raise ContractSemanticError(
            f"{WF_EDIT_STALE_BASE}: {label}: baseRevision does not match the current revision"
        )

    for operation in fields["operations"]:  # type: ignore[union-attr]
        operation_fields = _mapping(operation, f"{label}.operations")
        operation_id = operation_fields["operationId"]
        for precondition in operation_fields["preconditions"]:  # type: ignore[union-attr]
            precondition_fields = _mapping(precondition, f"{label}.operations.preconditions")
            if precondition_check(precondition_fields) is not True:
                raise ContractSemanticError(
                    f"{WF_EDIT_PRECONDITION_FAILED}: {label}: operation {operation_id!r} "
                    f"precondition against {precondition_fields['targetStableId']!r} failed"
                )

    if workflow_check() is not True:
        raise ContractSemanticError(f"{label}: Workflow Check failure refuses the batch")

    accepted_revision = current_revision + 1
    operation_ids = tuple(
        _mapping(operation, f"{label}.operations")["operationId"]
        for operation in fields["operations"]  # type: ignore[union-attr]
    )
    return MappingProxyType(
        {
            "batchId": fields["batchId"],
            "acceptedRevision": accepted_revision,
            "batchPayloadDigest": fields["batchPayloadDigest"],
            "operationIds": operation_ids,
        }
    )


# --- T-0688 IP-08: ComponentPortContract public contract semantics ------------------------
#
# DOC-004 Appendix AD.4 and REF-004 Appendix D.2 define a Component's ports as an exact closed
# shape reusing the repository's existing `WorkflowValue` semantic/schema/cardinality/presence
# vocabularies (`is_identifier`, `_reference`, `WORKFLOW_VALUE_CARDINALITIES`,
# `WORKFLOW_VALUE_PRESENCES`) rather than inventing a second parallel vocabulary. Like
# `RuntimeDefinitionBinding`, `RuntimeTransitionBundle`, and `WorkflowEditBatch` above, a port
# contract carries no `contractName` and is deliberately kept out of
# `WORKFLOW_RECORD_VALIDATORS`.
#
# `deliveryMode`, `driver`, and `fanOutPolicy` are optional additions: their absence is valid v1
# behavior and semantically defaults to `whole`/`false`/`none` without this module rewriting the
# caller's record to inject a default value.
#
# `dynamicDerivation`, per DOC-004 Appendix AD.4, is the exact closed three-member record
# `{derivationExpression, resolvedPortSetDigest, derivationDigest}`. It does not itself execute
# `derivationExpression`, and it does not carry the resolved ports: it records the
# publication-time resolved-set digest for a port set resolved elsewhere. The complete derived
# ports are ordinary `ComponentPortContract` records in the containing port set (AD.6 `ports`)
# and receive the same required-field validation as any other port there; this module has no
# preimage to recompute either digest against, so it validates format only: a non-empty
# `derivationExpression` and two well-formed content digests. Any malformed, empty, or unknown
# field raises `ContractSemanticError` tagged `WF_PORT_DYNAMIC_UNRESOLVED`.

WF_PORT_DYNAMIC_UNRESOLVED: Final = "WF_PORT_DYNAMIC_UNRESOLVED"

PORT_DELIVERY_MODES: Final[tuple[str, ...]] = ("whole", "chunked", "streamed")
PORT_DIRECTIONS: Final[tuple[str, ...]] = ("input", "output")
PORT_FAN_OUT_POLICIES: Final[tuple[str, ...]] = ("none", "broadcast", "partitioned")

_PORT_REQUIRED_FIELDS: Final = frozenset(
    {
        "portId",
        "direction",
        "semanticType",
        "physicalSchema",
        "cardinality",
        "presence",
        "classification",
        "lineage",
    }
)
_PORT_OPTIONAL_FIELDS: Final = frozenset(
    {"deliveryMode", "driver", "fanOutPolicy", "dynamicDerivation"}
)
_PORT_FIELDS: Final = _PORT_REQUIRED_FIELDS | _PORT_OPTIONAL_FIELDS

_DYNAMIC_DERIVATION_FIELDS: Final = frozenset(
    {"derivationExpression", "resolvedPortSetDigest", "derivationDigest"}
)


def _validate_dynamic_derivation(record: object, label: str) -> None:
    if not isinstance(record, Mapping):
        raise ContractSemanticError(f"{WF_PORT_DYNAMIC_UNRESOLVED}: {label}: expected a mapping")
    if any(not isinstance(key, str) for key in record):
        raise ContractSemanticError(f"{WF_PORT_DYNAMIC_UNRESOLVED}: {label}: field names must be strings")
    unknown = sorted(set(record) - _DYNAMIC_DERIVATION_FIELDS)
    if unknown:
        raise ContractSemanticError(f"{WF_PORT_DYNAMIC_UNRESOLVED}: {label}: unknown fields {unknown!r}")

    expression = record.get("derivationExpression")
    if isinstance(expression, bool) or not isinstance(expression, str) or not expression:
        raise ContractSemanticError(
            f"{WF_PORT_DYNAMIC_UNRESOLVED}: {label}: derivationExpression is not a non-empty string"
        )

    if not is_content_checksum(record.get("resolvedPortSetDigest")):
        raise ContractSemanticError(
            f"{WF_PORT_DYNAMIC_UNRESOLVED}: {label}: resolvedPortSetDigest is not a well-formed Digest"
        )
    if not is_content_checksum(record.get("derivationDigest")):
        raise ContractSemanticError(
            f"{WF_PORT_DYNAMIC_UNRESOLVED}: {label}: derivationDigest is not a well-formed Digest"
        )


def validate_component_port_contract(record: object) -> None:
    """Validate the T-0688 `ComponentPortContract` closed record (DOC-004 §AD.4, REF-004 §D.2)."""
    label = "ComponentPortContract"
    fields = _mapping(record, label)
    _only_fields(fields, _PORT_FIELDS, label)
    _identifier(fields, "portId", label)
    _member(fields, "direction", label, PORT_DIRECTIONS)
    _identifier(fields, "semanticType", label)
    _reference(fields, "physicalSchema", label)
    _member(fields, "cardinality", label, WORKFLOW_VALUE_CARDINALITIES)
    _member(fields, "presence", label, WORKFLOW_VALUE_PRESENCES)
    _reference(fields, "classification", label)
    _reference(fields, "lineage", label)

    if "deliveryMode" in fields:
        _member(fields, "deliveryMode", label, PORT_DELIVERY_MODES)
    if "driver" in fields:
        _boolean(fields, "driver", label)
    if "fanOutPolicy" in fields:
        _member(fields, "fanOutPolicy", label, PORT_FAN_OUT_POLICIES)
    if "dynamicDerivation" in fields:
        _validate_dynamic_derivation(fields["dynamicDerivation"], f"{label}.dynamicDerivation")


def validate_component_port_set(records: object) -> None:
    """Validate a non-empty, ordered `ComponentPortContract` collection (DOC-004 §AD.4).

    Port IDs must be unique across the set, and at most one *input* port may declare
    `driver: true`; an output port's `driver: true` does not satisfy this rule and is refused,
    since a driver is what starts a Run and an output cannot be what starts it.
    """
    label = "ComponentPortSet"
    if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
        raise ContractSemanticError(f"{label}: expected an array of ComponentPortContract")
    if not records:
        raise ContractSemanticError(f"{label}: must not be empty")

    port_ids: list[str] = []
    driver_input_count = 0
    for index, port in enumerate(records):
        entry_label = f"{label}[{index}]"
        try:
            validate_component_port_contract(port)
        except ContractSemanticError as error:
            raise ContractSemanticError(f"{entry_label}: {error}") from error
        fields = _mapping(port, entry_label)
        port_ids.append(fields["portId"])  # type: ignore[arg-type]
        if fields.get("driver") is True:
            if fields["direction"] != "input":
                raise ContractSemanticError(
                    f"{entry_label}: only an input port may declare driver: true"
                )
            driver_input_count += 1

    if len(port_ids) != len(set(port_ids)):
        raise ContractSemanticError(f"{label}: must not repeat a portId")
    if driver_input_count > 1:
        raise ContractSemanticError(f"{label}: at most one input port may declare driver: true")


# --- T-0688 IP-08: OWIR source projection semantics (REF-004 §D.5) ------------------------
#
# OWIR (REF-004 §D.5) is generated, read-only, never canonical, and never round-tripped: it is
# a stable-ID, revision-bound textual projection for review, diff and Assistant context, not an
# authoring language. `OwirSourceProjection` records only the provenance facts §D.5 requires --
# which exact revision and definition digest this text was generated from, that it is
# `generated`/`readOnly`/not `canonical`, which stable IDs it projects, and a recomputable digest
# of its own text -- so a caller can prove a projection is current and unmutated. It exposes no
# parse/apply/merge function, because §D.5.2 forbids any path back from OWIR text to a Definition
# change.

WF_OWIR_PROJECTION_STALE: Final = "WF_OWIR_PROJECTION_STALE"

_OWIR_PROJECTION_FIELDS: Final = frozenset(
    {
        "projectionSchemaVersion",
        "projectionId",
        "sourceRef",
        "sourceRevision",
        "sourceDefinitionDigest",
        "generated",
        "readOnly",
        "canonical",
        "stableElementIds",
        "text",
        "projectionDigest",
    }
)


def _owir_element_line_present(text: str, stable_id: str) -> bool:
    for line in text.splitlines():
        if line == f"element {stable_id}":
            return True
    return False


def validate_owir_source_projection(record: object) -> None:
    """Validate the T-0688 `OwirSourceProjection` closed record (REF-004 §D.5).

    Every `stableElementIds` entry must be visibly present in `text` as a complete `element
    <stableId>` line token, not merely a substring, and `projectionDigest` must be the exact
    recomputation of the UTF-8 bytes of `text` -- proving the projection was not silently edited
    after generation.
    """
    label = "OwirSourceProjection"
    fields = _mapping(record, label)
    _only_fields(fields, _OWIR_PROJECTION_FIELDS, label)
    _release_version(fields, "projectionSchemaVersion", label)
    _identifier(fields, "projectionId", label)
    _reference(fields, "sourceRef", label)
    _non_negative_int(fields, "sourceRevision", label)
    _digest(fields, "sourceDefinitionDigest", label)
    _literal_true(fields, "generated", label)
    _literal_true(fields, "readOnly", label)
    _literal_false(fields, "canonical", label)

    stable_ids = _sequence(fields, "stableElementIds", label)
    if not stable_ids:
        raise ContractSemanticError(f"{label}: stableElementIds must not be empty")
    seen: list[str] = []
    for index, item in enumerate(stable_ids):
        if not is_identifier(item):
            raise ContractSemanticError(f"{label}: stableElementIds[{index}] is not an Identifier")
        assert isinstance(item, str)
        seen.append(item)
    if len(seen) != len(set(seen)):
        raise ContractSemanticError(f"{label}: stableElementIds must not repeat a stable ID")

    text = _string(fields, "text", label)
    for stable_id in seen:
        if not _owir_element_line_present(text, stable_id):
            raise ContractSemanticError(
                f"{label}: text does not carry an 'element {stable_id}' line for stableElementIds"
            )

    _digest(fields, "projectionDigest", label)
    expected_digest = f"sha256:{sha256(text.encode('utf-8')).hexdigest()}"
    if fields["projectionDigest"] != expected_digest:
        raise ContractSemanticError(f"{label}: projectionDigest does not match its recomputation")


def validate_owir_source_projection_matches_revision(
    record: object,
    *,
    current_revision: int,
    current_definition_digest: str,
) -> None:
    """Refuse a stale `OwirSourceProjection` (REF-004 §D.5.3: a divergent projection is a defect).

    Validates the record's own shape first, then compares `sourceRevision` and
    `sourceDefinitionDigest` against the caller-supplied current values. It never mutates or
    silently refreshes the record; a mismatch on either value refuses with
    `WF_OWIR_PROJECTION_STALE`, naming that the projection must be regenerated.
    """
    validate_owir_source_projection(record)
    label = "OwirSourceProjection"
    fields = _mapping(record, label)
    if isinstance(current_revision, bool) or not isinstance(current_revision, int) or current_revision < 0:
        raise ContractSemanticError(f"{label}: current_revision is not a non-negative integer")
    if not is_content_checksum(current_definition_digest):
        raise ContractSemanticError(
            f"{label}: current_definition_digest is not a well-formed Digest"
        )
    if (
        fields["sourceRevision"] != current_revision
        or fields["sourceDefinitionDigest"] != current_definition_digest
    ):
        raise ContractSemanticError(
            f"{WF_OWIR_PROJECTION_STALE}: {label}: OWIR source is stale and must be regenerated"
        )


# --- T-0688 IP-09 / WEFT-BL-011: Nested Workflow boundary semantics -----------------------
#
# REF-004 §D.3 and DOC-004 §AD.7 define the Nested Workflow boundary as a *declared contract*:
# parent and child communicate externally only through the child's declared typed boundary
# ports, and the boundary is checked at publication and connection resolution. It is expressly
# not enforced by a mediator: there is no controller Component, proxy node or runtime
# interceptor here, and no field naming one -- `_only_fields` refuses any such member as
# unknown rather than quietly accepting it.
#
# Like the T-0688 records above, `NestedWorkflowBoundary` carries no `contractName` and is
# deliberately kept out of `WORKFLOW_RECORD_VALIDATORS`. It is additive: a Workflow that
# declares no nested boundary is untouched by anything in this section.
#
# `evaluate_nested_workflow_boundary` is the diagnose-first rollout, and it is exactly two
# stages. `R0` reports `warning` for every Workflow and refuses nothing. `R1` refuses
# publication of a `new` or `republished` Version only; a `published_active` Version stays
# pinned to the posture recorded at its publication (REF-004 §D.3.5), so an active Run keeps
# running against its pinned Version. There is deliberately no `R2` member: this behaviour has
# no third stage (COMPATIBILITY-AND-MIGRATION.md §4, §7).
#
# A diagnostic carries location only -- the parent Workflow and Version, the child stable ID,
# and the bypassing reference's own identity and endpoints. It never carries a value, a
# configuration mapping or a secret, because it is assembled from those named location members
# alone and never from the caller's record wholesale.

WF_NESTED_BOUNDARY_BYPASS: Final = "WF_NESTED_BOUNDARY_BYPASS"

NESTED_BOUNDARY_ROLLOUT_STAGES: Final[tuple[str, ...]] = ("R0", "R1")
NESTED_BOUNDARY_PUBLICATION_POSTURES: Final[tuple[str, ...]] = (
    "new",
    "republished",
    "published_active",
)

#: Every way a child can reach outside itself. Only `connection` and `reference` can cross a
#: declared typed boundary port at all; shared state, an ambient value and a side channel are
#: bypasses by construction (REF-004 §D.3.1), so a claim that one of them crossed a port is
#: refused rather than believed.
NESTED_BOUNDARY_REFERENCE_KINDS: Final[tuple[str, ...]] = (
    "connection",
    "reference",
    "shared_state",
    "ambient_value",
    "side_channel",
)
_PORT_CROSSING_REFERENCE_KINDS: Final = frozenset({"connection", "reference"})

BOUNDARY_AGGREGATION_OUTCOMES: Final[tuple[str, ...]] = (
    "satisfied",
    "unsatisfied",
    "indeterminate",
)
_BOUNDARY_AGGREGATION_DIMENSIONS: Final[tuple[str, ...]] = (
    "policyOutcome",
    "evidenceOutcome",
    "completionOutcome",
    "reviewOutcome",
)
_MAX_BOUNDARY_AGGREGATION_DEPTH: Final = 32

_NESTED_BOUNDARY_FIELDS: Final = frozenset(
    {
        "boundarySchemaVersion",
        "parentWorkflowId",
        "parentWorkflowVersion",
        "childWorkflowStableId",
        "boundaryPorts",
        "childExternalReferences",
        "aggregation",
    }
)
_CHILD_EXTERNAL_REFERENCE_FIELDS: Final = frozenset(
    {"referenceId", "referenceKind", "fromStableId", "toStableId", "viaBoundaryPortId"}
)
_BOUNDARY_AGGREGATION_FIELDS: Final = frozenset(
    {
        *_BOUNDARY_AGGREGATION_DIMENSIONS,
        "completionEvidence",
        "indeterminateReason",
        "descendantAggregations",
    }
)
_NESTED_BOUNDARY_EXCEPTION_FIELDS: Final = frozenset(
    {"exceptionId", "workflowId", "workflowVersion", "expiresAt", "decidingActor", "evidence"}
)


def _instant(value: str) -> datetime:
    """Parse an already-validated `Timestamp` into an instant for ordering."""
    return datetime.fromisoformat(value)


def _boundary_aggregation(record: object, label: str, *, depth: int) -> Mapping[str, str]:
    """Validate one boundary aggregation node and return its four composed outcomes.

    Policy, Evidence, completion and Review aggregate *at the boundary* (REF-004 §D.3.2), so a
    parent's posture accounts for every descendant: an indeterminate descendant outcome stays
    explicitly indeterminate at the parent and an unsatisfied one is never absorbed as
    satisfied. Completion remains verified rather than inferred, so a satisfied completion
    names its Evidence.
    """
    if depth > _MAX_BOUNDARY_AGGREGATION_DEPTH:
        raise ContractSemanticError(f"{label}: descendantAggregations nest beyond the bound")
    fields = _mapping(record, label)
    _only_fields(fields, _BOUNDARY_AGGREGATION_FIELDS, label)
    outcomes = {
        dimension: _member(fields, dimension, label, BOUNDARY_AGGREGATION_OUTCOMES)
        for dimension in _BOUNDARY_AGGREGATION_DIMENSIONS
    }

    has_completion_evidence = "completionEvidence" in fields
    if outcomes["completionOutcome"] == "satisfied":
        if not has_completion_evidence:
            raise ContractSemanticError(
                f"{label}: a satisfied completionOutcome is verified and names completionEvidence"
            )
        _reference(fields, "completionEvidence", label)
    elif has_completion_evidence:
        raise ContractSemanticError(
            f"{label}: completionEvidence is only valid for a satisfied completionOutcome"
        )

    any_indeterminate = any(outcome == "indeterminate" for outcome in outcomes.values())
    has_reason = "indeterminateReason" in fields
    if any_indeterminate:
        if not has_reason:
            raise ContractSemanticError(
                f"{label}: an indeterminate outcome must stay explicit and name indeterminateReason"
            )
        _reference(fields, "indeterminateReason", label)
    elif has_reason:
        raise ContractSemanticError(
            f"{label}: indeterminateReason is only valid for an indeterminate outcome"
        )

    for index, descendant in enumerate(_optional_sequence(fields, "descendantAggregations", label)):
        child = _boundary_aggregation(
            descendant, f"{label}.descendantAggregations[{index}]", depth=depth + 1
        )
        for dimension, outcome in child.items():
            if outcome == "indeterminate" and outcomes[dimension] != "indeterminate":
                raise ContractSemanticError(
                    f"{label}: an indeterminate descendant {dimension} must remain "
                    f"indeterminate at the boundary"
                )
            if outcome == "unsatisfied" and outcomes[dimension] == "satisfied":
                raise ContractSemanticError(
                    f"{label}: an unsatisfied descendant {dimension} may not aggregate as satisfied"
                )
    return outcomes


def _child_external_reference(record: object, label: str, port_ids: frozenset[str]) -> tuple[str, bool]:
    """Validate one child-to-external reference and report whether it bypasses the boundary."""
    fields = _mapping(record, label)
    _only_fields(fields, _CHILD_EXTERNAL_REFERENCE_FIELDS, label)
    reference_id = _identifier(fields, "referenceId", label)
    kind = _member(fields, "referenceKind", label, NESTED_BOUNDARY_REFERENCE_KINDS)
    _identifier(fields, "fromStableId", label)
    _identifier(fields, "toStableId", label)

    if "viaBoundaryPortId" not in fields:
        return reference_id, True
    port_id = _identifier(fields, "viaBoundaryPortId", label)
    if kind not in _PORT_CROSSING_REFERENCE_KINDS:
        raise ContractSemanticError(
            f"{label}: a {kind} reference crosses no declared typed boundary port"
        )
    return reference_id, port_id not in port_ids


def validate_nested_workflow_boundary(record: object) -> None:
    """Validate the T-0688 `NestedWorkflowBoundary` closed record (REF-004 §D.3, DOC-004 §AD.7).

    Shape only: which references bypass the declared typed boundary ports is reported by
    :func:`evaluate_nested_workflow_boundary`, which alone knows the rollout stage and the
    Version's publication posture. A bypass is never a shape error here, because at `R0` a
    bypass changes nothing at all.
    """
    label = "NestedWorkflowBoundary"
    fields = _mapping(record, label)
    _only_fields(fields, _NESTED_BOUNDARY_FIELDS, label)
    _release_version(fields, "boundarySchemaVersion", label)
    _identifier(fields, "parentWorkflowId", label)
    _release_version(fields, "parentWorkflowVersion", label)
    _identifier(fields, "childWorkflowStableId", label)

    validate_component_port_set(_present(fields, "boundaryPorts", label))
    port_ids = frozenset(
        _mapping(port, f"{label}.boundaryPorts")["portId"]  # type: ignore[misc]
        for port in _sequence(fields, "boundaryPorts", label)
    )

    reference_ids = [
        _child_external_reference(
            entry, f"{label}.childExternalReferences[{index}]", port_ids
        )[0]
        for index, entry in enumerate(_sequence(fields, "childExternalReferences", label))
    ]
    if len(reference_ids) != len(set(reference_ids)):
        raise ContractSemanticError(f"{label}: childExternalReferences must not repeat a referenceId")

    _boundary_aggregation(_present(fields, "aggregation", label), f"{label}.aggregation", depth=0)


def validate_nested_boundary_exception(record: object) -> None:
    """Validate the T-0688 `NestedBoundaryException` closed record (REF-004 §D.3.6).

    An exception names exactly one Workflow Version, an expiry instant, the deciding actor and
    its Evidence. Open-ended, class-wide and unattributed exceptions are refused structurally:
    there is no wildcard Version spelling, no absent `expiresAt` and no anonymous decision.
    Whether the expiry has already passed is a question about *when*, so it is answered by
    :func:`evaluate_nested_workflow_boundary` against the instant it is given.
    """
    label = "NestedBoundaryException"
    fields = _mapping(record, label)
    _only_fields(fields, _NESTED_BOUNDARY_EXCEPTION_FIELDS, label)
    _identifier(fields, "exceptionId", label)
    _identifier(fields, "workflowId", label)
    _release_version(fields, "workflowVersion", label)
    _timestamp(fields, "expiresAt", label)
    _reference(fields, "decidingActor", label)
    _reference(fields, "evidence", label)


def evaluate_nested_workflow_boundary(
    boundary: object,
    *,
    rollout_stage: str,
    publication_posture: str,
    at_instant: str,
    exceptions: Sequence[object] = (),
) -> Mapping[str, object]:
    """Diagnose Nested Workflow boundary bypasses and decide whether publication is refused.

    Returns a frozen `{diagnostics, publicationRefused}` description. Diagnostics are ordered by
    `referenceId` so two runs over the same boundary produce byte-identical output, carry the
    exact `WF_NESTED_BOUNDARY_BYPASS` code, and carry location only.

    Posture, exactly as sequenced by REF-004 §D.3.4-5 and the change set's §7: at `R0` every
    diagnostic is a `warning` and nothing is refused; at `R1` a `new` or `republished` Version is
    refused, while a `published_active` Version stays pinned to its published posture. Nothing
    escalates beyond `R1` -- `"R2"` is not a member of `NESTED_BOUNDARY_ROLLOUT_STAGES`.

    An exception suppresses refusal only when it names this exact Workflow Version, is
    attributed, carries Evidence and has not expired at `at_instant`. It never suppresses the
    diagnostic itself, which is still reported at `warning`.
    """
    validate_nested_workflow_boundary(boundary)
    label = "NestedWorkflowBoundary"
    fields = _mapping(boundary, label)

    stage = _member({"rollout_stage": rollout_stage}, "rollout_stage", label, NESTED_BOUNDARY_ROLLOUT_STAGES)
    posture = _member(
        {"publication_posture": publication_posture},
        "publication_posture",
        label,
        NESTED_BOUNDARY_PUBLICATION_POSTURES,
    )
    if not is_timestamp(at_instant):
        raise ContractSemanticError(f"{label}: at_instant is not a well-formed Timestamp")

    port_ids = frozenset(
        _mapping(port, f"{label}.boundaryPorts")["portId"]  # type: ignore[misc]
        for port in _sequence(fields, "boundaryPorts", label)
    )
    bypasses = [
        _mapping(entry, f"{label}.childExternalReferences[{index}]")
        for index, entry in enumerate(_sequence(fields, "childExternalReferences", label))
        if _child_external_reference(entry, f"{label}.childExternalReferences[{index}]", port_ids)[1]
    ]

    enforcing = stage == "R1" and posture in {"new", "republished"}
    if enforcing:
        now = _instant(at_instant)
        for index, exception in enumerate(exceptions):
            validate_nested_boundary_exception(exception)
            exception_fields = _mapping(exception, f"NestedBoundaryException[{index}]")
            if (
                exception_fields["workflowId"] == fields["parentWorkflowId"]
                and exception_fields["workflowVersion"] == fields["parentWorkflowVersion"]
                and _instant(exception_fields["expiresAt"]) > now  # type: ignore[arg-type]
            ):
                enforcing = False
                break

    severity = "blocker" if enforcing else "warning"
    diagnostics = tuple(
        MappingProxyType(
            {
                "code": WF_NESTED_BOUNDARY_BYPASS,
                "severity": severity,
                "parentWorkflowId": fields["parentWorkflowId"],
                "parentWorkflowVersion": fields["parentWorkflowVersion"],
                "childWorkflowStableId": fields["childWorkflowStableId"],
                "referenceId": entry["referenceId"],
                "referenceKind": entry["referenceKind"],
                "fromStableId": entry["fromStableId"],
                "toStableId": entry["toStableId"],
            }
        )
        for entry in sorted(bypasses, key=lambda entry: entry["referenceId"])  # type: ignore[arg-type,return-value]
    )
    return MappingProxyType(
        {
            "diagnostics": diagnostics,
            "publicationRefused": bool(diagnostics) and enforcing,
        }
    )


# --- T-0688 IP-09 / WEFT-BL-012: complete LoopPlan and iteration ledger semantics ----------
#
# DOC-004 §AD.5 and REF-004 §D.4 define the complete, closed `LoopPlan`. It is a *different*
# record from the T-0679 `LoopPlan` `validate_loop_plan` above already validates: that one is a
# small `contractName`-tagged runtime-readiness record (frozen-at-run-start, a concurrency
# limit, a deterministic order, an iteration-ledger requirement) and it is unchanged here, still
# dispatched from `WORKFLOW_RECORD_VALIDATORS` on its own `contractName`. The candidate shape
# below carries no `contractName`, is validated through its own separately named entry point,
# and is kept out of that registry exactly as every other T-0688 candidate record is.
#
# Contradiction is the whole point of the closed plan, so every internal contradiction §AD.5.1
# rule 6 names -- carry with parallel mode, zipped sources without a mismatch policy, parallel
# mode without a concurrency bound, a non-deterministic done condition, a gather ordering
# inconsistent with `orderGuarantee` -- refuses with the exact `WF_LOOP_PLAN_INVALID` code.

WF_LOOP_PLAN_INVALID: Final = "WF_LOOP_PLAN_INVALID"

LOOP_MODES: Final[tuple[str, ...]] = ("sequential", "parallel")
LOOP_SOURCE_KINDS: Final[tuple[str, ...]] = ("collection", "stream")
LOOP_ZIP_MISMATCH_POLICIES: Final[tuple[str, ...]] = (
    "refuse",
    "truncateToShortest",
    "padWithAbsent",
)
LOOP_ORDER_GUARANTEES: Final[tuple[str, ...]] = ("iterationIdentityOrder", "settlementOrder")
LOOP_CANCELLATION_POLICIES: Final[tuple[str, ...]] = (
    "cancelRemaining",
    "drainInFlight",
    "completeAll",
)
LOOP_PARTIAL_SUCCESS_POLICIES: Final[tuple[str, ...]] = (
    "failLoop",
    "recordAndContinue",
    "recordAndStopAfterCurrent",
)
LOOP_ITERATION_IDENTITY_RULE_KINDS: Final[tuple[str, ...]] = (
    "sourceOrdinal",
    "elementKeyPath",
    "elementDigest",
)
LOOP_DONE_CONDITION_KINDS: Final[tuple[str, ...]] = (
    "sourceExhausted",
    "predicate",
    "iterationCount",
)
LOOP_ITERATION_OUTCOME_CLASSES: Final[tuple[str, ...]] = (
    "succeeded",
    "failed",
    "cancelled",
    "skipped",
    "late",
)

_COMPLETE_LOOP_PLAN_FIELDS: Final = frozenset(
    {
        "loopStableId",
        "iterationIdentity",
        "mode",
        "source",
        "zipSources",
        "zipMismatchPolicy",
        "carry",
        "gather",
        "done",
        "maximumIterations",
        "maximumConcurrency",
        "orderGuarantee",
        "cancellationPolicy",
        "partialSuccessPolicy",
    }
)
_ITERATION_IDENTITY_RULE_FIELDS: Final = frozenset(
    {"ruleKind", "keyPath", "stableAcrossReplay"}
)
_LOOP_SOURCE_BINDING_FIELDS: Final = frozenset(
    {"sourceKind", "sourcePortRef", "completionSignal"}
)
_CARRY_DECLARATION_FIELDS: Final = frozenset({"carryId", "initialValueRef"})
_GATHER_DECLARATION_FIELDS: Final = frozenset({"gatherId", "ordering"})
_DONE_CONDITION_FIELDS: Final = frozenset(
    {"conditionKind", "deterministic", "predicateRef", "iterationCount"}
)

_LOOP_ITERATION_LEDGER_FIELDS: Final = frozenset(
    {"ledgerSchemaVersion", "loopStableId", "loopSettledAt", "entries"}
)
_LOOP_ITERATION_ENTRY_FIELDS: Final = frozenset(
    {
        "iterationIdentity",
        "outcomeClass",
        "launchedAt",
        "launchBundleRef",
        "inputsDigest",
        "carryDigest",
        "schedulingIntentsDigest",
        "outputsDigest",
        "failureRef",
        "settledAt",
        "effectSettlements",
        "cancellationDisposition",
        "evidenceRef",
        "appliedToRunState",
    }
)


def _loop_invalid(label: str, message: str) -> ContractSemanticError:
    return ContractSemanticError(f"{WF_LOOP_PLAN_INVALID}: {label}: {message}")


def _iteration_identity_rule(record: object, label: str) -> None:
    fields = _mapping(record, label)
    _only_fields(fields, _ITERATION_IDENTITY_RULE_FIELDS, label)
    kind = _member(fields, "ruleKind", label, LOOP_ITERATION_IDENTITY_RULE_KINDS)
    has_key_path = "keyPath" in fields
    if (kind == "elementKeyPath") != has_key_path:
        raise _loop_invalid(label, "keyPath is required exactly for the elementKeyPath rule")
    if has_key_path:
        _string(fields, "keyPath", label)
    if not _boolean(fields, "stableAcrossReplay", label):
        raise _loop_invalid(label, "an iteration identity must be stable across replay")


def _loop_source_binding(record: object, label: str) -> None:
    fields = _mapping(record, label)
    _only_fields(fields, _LOOP_SOURCE_BINDING_FIELDS, label)
    kind = _member(fields, "sourceKind", label, LOOP_SOURCE_KINDS)
    _reference(fields, "sourcePortRef", label)
    has_signal = "completionSignal" in fields
    if (kind == "stream") != has_signal:
        raise _loop_invalid(label, "a stream source declares its completionSignal and a collection does not")
    if has_signal:
        _reference(fields, "completionSignal", label)


def _done_condition(record: object, label: str) -> None:
    fields = _mapping(record, label)
    _only_fields(fields, _DONE_CONDITION_FIELDS, label)
    kind = _member(fields, "conditionKind", label, LOOP_DONE_CONDITION_KINDS)
    if not _boolean(fields, "deterministic", label):
        raise _loop_invalid(label, "a done condition must be deterministic")
    if ("predicate" == kind) != ("predicateRef" in fields):
        raise _loop_invalid(label, "predicateRef is required exactly for a predicate condition")
    if kind == "predicate":
        _reference(fields, "predicateRef", label)
    if ("iterationCount" == kind) != ("iterationCount" in fields):
        raise _loop_invalid(
            label, "iterationCount is required exactly for an iterationCount condition"
        )
    if kind == "iterationCount":
        _positive_int(fields, "iterationCount", label)


def validate_complete_loop_plan(record: object) -> None:
    """Validate the T-0688 complete, closed `LoopPlan` (DOC-004 §AD.5, REF-004 §D.4).

    Additive and separately named: the historical `contractName`-tagged `LoopPlan` record and
    its :func:`validate_loop_plan` are untouched and keep their exact previous meaning.

    Every member §AD.5 lists is covered, and every contradiction §AD.5.1 rule 6 names refuses
    with `WF_LOOP_PLAN_INVALID`. `maximumConcurrency` is required exactly when `mode` is
    `parallel` -- required *and* bounded, since an unbounded concurrency is not a frozen bound --
    and is refused outright for a `sequential` plan, where the concurrency is one by definition.
    """
    label = "LoopPlan"
    fields = _mapping(record, label)
    _only_fields(fields, _COMPLETE_LOOP_PLAN_FIELDS, label)
    _identifier(fields, "loopStableId", label)
    _iteration_identity_rule(_present(fields, "iterationIdentity", label), f"{label}.iterationIdentity")
    mode = _member(fields, "mode", label, LOOP_MODES)
    _loop_source_binding(_present(fields, "source", label), f"{label}.source")

    zip_sources = _optional_sequence(fields, "zipSources", label)
    for index, entry in enumerate(zip_sources):
        _loop_source_binding(entry, f"{label}.zipSources[{index}]")
    if "zipSources" in fields and not zip_sources:
        raise _loop_invalid(label, "zipSources must not be declared empty")
    if bool(zip_sources) != ("zipMismatchPolicy" in fields):
        raise _loop_invalid(
            label, "zipMismatchPolicy is required exactly when zipSources are declared"
        )
    if zip_sources:
        _member(fields, "zipMismatchPolicy", label, LOOP_ZIP_MISMATCH_POLICIES)

    if "carry" in fields:
        if mode != "sequential":
            raise _loop_invalid(label, "carry is permitted only when mode is sequential")
        carry_label = f"{label}.carry"
        carry = _mapping(fields["carry"], carry_label)
        _only_fields(carry, _CARRY_DECLARATION_FIELDS, carry_label)
        _identifier(carry, "carryId", carry_label)
        _reference(carry, "initialValueRef", carry_label)

    _done_condition(_present(fields, "done", label), f"{label}.done")
    maximum_iterations = _positive_int(fields, "maximumIterations", label)
    order_guarantee = _member(fields, "orderGuarantee", label, LOOP_ORDER_GUARANTEES)

    if "gather" in fields:
        gather_label = f"{label}.gather"
        gather = _mapping(fields["gather"], gather_label)
        _only_fields(gather, _GATHER_DECLARATION_FIELDS, gather_label)
        _identifier(gather, "gatherId", gather_label)
        if _member(gather, "ordering", gather_label, LOOP_ORDER_GUARANTEES) != order_guarantee:
            raise _loop_invalid(label, "gather ordering is inconsistent with orderGuarantee")

    has_concurrency = "maximumConcurrency" in fields
    if (mode == "parallel") != has_concurrency:
        raise _loop_invalid(
            label, "maximumConcurrency is required exactly when mode is parallel"
        )
    if has_concurrency and _positive_int(fields, "maximumConcurrency", label) > maximum_iterations:
        raise _loop_invalid(label, "maximumConcurrency exceeds maximumIterations")

    _member(fields, "cancellationPolicy", label, LOOP_CANCELLATION_POLICIES)
    _member(fields, "partialSuccessPolicy", label, LOOP_PARTIAL_SUCCESS_POLICIES)


def _loop_iteration_entry(record: object, label: str, *, loop_settled_at: str | None) -> str:
    fields = _mapping(record, label)
    _only_fields(fields, _LOOP_ITERATION_ENTRY_FIELDS, label)
    identity = _identifier(fields, "iterationIdentity", label)
    outcome = _member(fields, "outcomeClass", label, LOOP_ITERATION_OUTCOME_CLASSES)
    launched_at = _timestamp(fields, "launchedAt", label)
    # A launch is atomic: identity, inputs, carry and scheduling intents were recorded in
    # exactly one `RuntimeTransitionBundle` (§AD.5.1 rule 2), so the entry names one bundle
    # reference, never a list of them. `carryDigest` is present only for a carrying loop.
    _reference(fields, "launchBundleRef", label)
    _digest(fields, "inputsDigest", label)
    _digest(fields, "schedulingIntentsDigest", label)
    if "carryDigest" in fields:
        _digest(fields, "carryDigest", label)
    settled_at = _timestamp(fields, "settledAt", label)
    if _instant(settled_at) < _instant(launched_at):
        raise ContractSemanticError(f"{label}: settledAt precedes launchedAt")

    for index, settlement in enumerate(_sequence(fields, "effectSettlements", label)):
        _effect_settlement(settlement, f"{label}.effectSettlements[{index}]")

    has_outputs = "outputsDigest" in fields
    has_failure = "failureRef" in fields
    if outcome in {"succeeded", "late"}:
        if not has_outputs or has_failure:
            raise ContractSemanticError(f"{label}: {outcome} records outputsDigest and no failureRef")
        _digest(fields, "outputsDigest", label)
    elif outcome == "failed":
        if has_outputs or not has_failure:
            raise ContractSemanticError(f"{label}: failed records failureRef and no outputsDigest")
        _reference(fields, "failureRef", label)
    elif has_outputs or has_failure:
        raise ContractSemanticError(
            f"{label}: {outcome} records neither outputsDigest nor failureRef"
        )

    has_disposition = "cancellationDisposition" in fields
    if (outcome == "cancelled") != has_disposition:
        raise ContractSemanticError(
            f"{label}: cancellationDisposition is required exactly for a cancelled iteration"
        )
    if has_disposition:
        _member(fields, "cancellationDisposition", label, CANCELLATION_IN_FLIGHT_STATES)

    has_evidence = "evidenceRef" in fields
    has_applied = "appliedToRunState" in fields
    if outcome == "late":
        if loop_settled_at is None:
            raise ContractSemanticError(f"{label}: a late result requires a settled loop")
        if _instant(settled_at) < _instant(loop_settled_at):
            raise ContractSemanticError(f"{label}: a late result settles after the loop settled")
        if not has_evidence:
            raise ContractSemanticError(f"{label}: a late result is recorded as Evidence")
        _reference(fields, "evidenceRef", label)
        if not has_applied or _boolean(fields, "appliedToRunState", label):
            raise ContractSemanticError(
                f"{label}: a late result is marked not applied to Run state"
            )
    else:
        if has_applied and not _boolean(fields, "appliedToRunState", label):
            raise ContractSemanticError(
                f"{label}: only a late result is marked not applied to Run state"
            )
        if has_evidence:
            _reference(fields, "evidenceRef", label)
    return identity


def validate_loop_iteration_ledger(record: object) -> None:
    """Validate the T-0688 complete `LoopIterationLedger` (DOC-004 §AD.5.1 rules 4-5, REF-004 §D.4.8-9).

    The ledger is complete for *every* outcome class -- cancelled, failed and skipped as much as
    succeeded -- and each entry records its launch, inputs digest, settlement instant, effect
    settlements and, where the class calls for it, an outputs digest or a failure reference but
    never both. Iteration identities are unique, which is what makes them usable as the
    idempotency key for that iteration's activations and effect settlements.

    A `late` entry is the one class that cannot change anything: it exists only once the loop has
    settled, it is recorded as Evidence against its iteration identity, and it is marked not
    applied to Run state.
    """
    label = "LoopIterationLedger"
    fields = _mapping(record, label)
    _only_fields(fields, _LOOP_ITERATION_LEDGER_FIELDS, label)
    _release_version(fields, "ledgerSchemaVersion", label)
    _identifier(fields, "loopStableId", label)
    loop_settled_at = _timestamp(fields, "loopSettledAt", label) if "loopSettledAt" in fields else None

    identities = [
        _loop_iteration_entry(
            entry, f"{label}.entries[{index}]", loop_settled_at=loop_settled_at
        )
        for index, entry in enumerate(_sequence(fields, "entries", label))
    ]
    if len(identities) != len(set(identities)):
        raise ContractSemanticError(f"{label}: entries must not repeat an iterationIdentity")


WORKFLOW_RECORD_VALIDATORS: Final[Mapping[str, WorkflowRecordValidator]] = MappingProxyType(
    {
        "WorkflowValue": validate_workflow_value,
        "AbsentValue": validate_absent_value,
        "ComponentImplementationBinding": validate_component_implementation_binding,
        "WorkflowCheckReadinessExtension": validate_workflow_check_readiness_extension,
        "SuggestedWorkflowFix": validate_suggested_workflow_fix,
        "SimulationResult": validate_simulation_result,
        "BranchAggregatePolicy": validate_branch_aggregate_policy,
        "LoopPlan": validate_loop_plan,
        "AttemptSettlement": validate_attempt_settlement,
        "CancellationRecord": validate_cancellation_record,
        "WorkflowArtifactReceipt": validate_workflow_artifact_receipt,
        "MigrationReceipt": validate_migration_receipt,
        "WorkflowVersionDiff": validate_workflow_version_diff,
    }
)


def validate_workflow_record(record: object) -> None:
    """Validate any T-0679 workflow record that carries `contractName`."""
    fields = _mapping(record, "workflow record")
    contract_name = fields.get("contractName")
    if not isinstance(contract_name, str) or contract_name not in WORKFLOW_RECORD_VALIDATORS:
        raise ContractSemanticError("workflow record: contractName is not a known T-0679 contract")
    WORKFLOW_RECORD_VALIDATORS[contract_name](record)
