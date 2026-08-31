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
from types import MappingProxyType
from typing import Final

from omnivia_core.contracts.v1.compatibility import ContractSemanticError
from omnivia_core.contracts.v1.generated import (
    is_content_checksum,
    is_identifier,
    is_open_code,
    is_timestamp,
)

__all__ = [
    "ATTEMPT_STATES",
    "BRANCH_AGGREGATE_MODES",
    "COMPONENT_IMPLEMENTATION_BINDING_STATES",
    "EFFECT_DISPOSITIONS",
    "LOOP_LATE_RESULT_POLICIES",
    "VISUAL_REVIEW_STATES",
    "WORKFLOW_DIFF_CLASSES",
    "WORKFLOW_RECORD_VALIDATORS",
    "WORKFLOW_VALUE_CARDINALITIES",
    "WORKFLOW_VALUE_PRESENCES",
    "WorkflowRecordValidator",
    "validate_absent_value",
    "validate_attempt_settlement",
    "validate_branch_aggregate_policy",
    "validate_cancellation_record",
    "validate_component_implementation_binding",
    "validate_loop_plan",
    "validate_migration_receipt",
    "validate_simulation_result",
    "validate_start_workflow_run_readiness",
    "validate_suggested_workflow_fix",
    "validate_workflow_artifact_receipt",
    "validate_workflow_check_readiness_extension",
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


def _reference(fields: Mapping[str, object], key: str, label: str) -> Mapping[str, object]:
    reference = _mapping(_present(fields, key, label), f"{label}.{key}")
    if not reference:
        raise ContractSemanticError(f"{label}: {key} must not be empty")
    return reference


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
