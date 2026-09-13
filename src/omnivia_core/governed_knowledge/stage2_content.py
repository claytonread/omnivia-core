"""Strict versioned content decoders for Stage 2 portable profiles."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from omnivia_core.governed_knowledge.assisted import (
    DIAGNOSIS_ATTEMPT_PROFILE_VERSION,
    WORKER_BINDING_PROFILE_VERSION,
    AssistedWorkerBinding,
    DiagnosisAttempt,
    DiagnosisAttemptStatus,
    ModelIdentityKind,
    diagnosis_attempt_to_content,
    worker_binding_to_content,
)
from omnivia_core.governed_knowledge.errors import GovernedKnowledgeErrorCode, require
from omnivia_core.governed_knowledge.evaluation import (
    CANDIDATE_OVERLAY_PROFILE_VERSION,
    EVALUATION_ATTEMPT_PROFILE_VERSION,
    EVALUATION_CASE_PROFILE_VERSION,
    EVALUATION_REPORT_PROFILE_VERSION,
    EVALUATION_SUITE_PROFILE_VERSION,
    CandidateOverlay,
    DeterministicCheck,
    DeterministicCheckStatus,
    EvaluationAttempt,
    EvaluationCaseLifecycle,
    EvaluationCaseSummary,
    EvaluationReportStatus,
    EvaluationResultStatus,
    KnowledgeEvaluationCase,
    KnowledgeEvaluationReport,
    KnowledgeEvaluationSuite,
    candidate_overlay_to_content,
    evaluation_attempt_to_content,
    evaluation_case_to_content,
    evaluation_report_to_content,
    evaluation_suite_to_content,
)
from omnivia_core.governed_knowledge.wire import decode_instant
from omnivia_core.semantic_registry.evidence import Classification


def _mapping(content: object, what: str) -> Mapping[str, Any]:
    require(isinstance(content, Mapping), GovernedKnowledgeErrorCode.INVALID_FIELD, f"{what} must be a mapping")
    return cast(Mapping[str, Any], content)


def _exact(value: Mapping[str, Any], expected: set[str], version: str, what: str) -> None:
    require(set(value) == expected, GovernedKnowledgeErrorCode.INVALID_FIELD, f"{what} fields are not exact")
    require(value.get("profile_version") == version, GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE, f"{what} profile_version is unsupported")


def _str(value: object, name: str) -> str:
    require(isinstance(value, str), GovernedKnowledgeErrorCode.INVALID_FIELD, f"{name} must be a string")
    return cast(str, value)


def _opt_str(value: object, name: str) -> str | None:
    require(value is None or isinstance(value, str), GovernedKnowledgeErrorCode.INVALID_FIELD, f"{name} must be a string or null")
    return cast(str | None, value)


def _int(value: object, name: str, *, optional: bool = False) -> int | None:
    require(
        (optional and value is None) or (isinstance(value, int) and not isinstance(value, bool)),
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        f"{name} must be an integer" + (" or null" if optional else ""),
    )
    return cast(int | None, value)


def _bool(value: object, name: str) -> bool:
    require(isinstance(value, bool), GovernedKnowledgeErrorCode.INVALID_FIELD, f"{name} must be a boolean")
    return cast(bool, value)


def _strings(value: object, name: str) -> tuple[str, ...]:
    require(isinstance(value, list) and all(isinstance(item, str) for item in value), GovernedKnowledgeErrorCode.INVALID_FIELD, f"{name} must be a string list")
    return tuple(cast(list[str], value))


def _enum(value: object, enum_type: type[Any], name: str) -> Any:
    raw = _str(value, name)
    require(raw in {item.value for item in enum_type}, GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE, f"{name} is unsupported")
    return enum_type(raw)


_WORKER_KEYS = {
    "profile_version", "binding_id", "workspace_id", "source_id", "executor_id",
    "executor_version", "executor_build_hash", "executor_content_hash",
    "runtime_profile_ref", "policy_ref", "required_capabilities", "minimum_isolation",
    "resolved_isolation", "run_ref", "step_ref", "attempt_ref", "provider_ref",
    "model_ref", "model_identity_kind", "observed_at_ref",
}


def worker_binding_from_content(content: object) -> AssistedWorkerBinding:
    value = _mapping(content, "worker binding")
    _exact(value, _WORKER_KEYS, WORKER_BINDING_PROFILE_VERSION, "worker binding")
    minimum = _int(value["minimum_isolation"], "minimum_isolation")
    resolved = _int(value["resolved_isolation"], "resolved_isolation")
    assert minimum is not None and resolved is not None
    return AssistedWorkerBinding(
        binding_id=_str(value["binding_id"], "binding_id"), workspace_id=_str(value["workspace_id"], "workspace_id"),
        source_id=_str(value["source_id"], "source_id"), executor_id=_str(value["executor_id"], "executor_id"),
        executor_version=_str(value["executor_version"], "executor_version"),
        executor_build_hash=_str(value["executor_build_hash"], "executor_build_hash"),
        executor_content_hash=_str(value["executor_content_hash"], "executor_content_hash"),
        runtime_profile_ref=_str(value["runtime_profile_ref"], "runtime_profile_ref"), policy_ref=_str(value["policy_ref"], "policy_ref"),
        required_capabilities=_strings(value["required_capabilities"], "required_capabilities"),
        minimum_isolation=minimum, resolved_isolation=resolved,
        run_ref=_str(value["run_ref"], "run_ref"), step_ref=_str(value["step_ref"], "step_ref"),
        attempt_ref=_str(value["attempt_ref"], "attempt_ref"), provider_ref=_opt_str(value["provider_ref"], "provider_ref"),
        model_ref=_opt_str(value["model_ref"], "model_ref"), model_identity_kind=_enum(value["model_identity_kind"], ModelIdentityKind, "model_identity_kind"),
        observed_at_ref=_str(value["observed_at_ref"], "observed_at_ref"),
    )


_DIAGNOSIS_ATTEMPT_KEYS = {
    "profile_version", "attempt_id", "request_id", "workspace_id", "feedback_ref",
    "worker_binding_ref", "status", "issue_refs", "diagnosis_refs", "proposal_ref",
    "safe_error_code", "input_tokens", "output_tokens", "cost_microunits",
}


def diagnosis_attempt_from_content(content: object) -> DiagnosisAttempt:
    value = _mapping(content, "diagnosis attempt")
    _exact(value, _DIAGNOSIS_ATTEMPT_KEYS, DIAGNOSIS_ATTEMPT_PROFILE_VERSION, "diagnosis attempt")
    return DiagnosisAttempt(
        attempt_id=_str(value["attempt_id"], "attempt_id"), request_id=_str(value["request_id"], "request_id"),
        workspace_id=_str(value["workspace_id"], "workspace_id"), feedback_ref=_str(value["feedback_ref"], "feedback_ref"),
        worker_binding_ref=_str(value["worker_binding_ref"], "worker_binding_ref"),
        status=_enum(value["status"], DiagnosisAttemptStatus, "status"), issue_refs=_strings(value["issue_refs"], "issue_refs"),
        diagnosis_refs=_strings(value["diagnosis_refs"], "diagnosis_refs"), proposal_ref=_opt_str(value["proposal_ref"], "proposal_ref"),
        safe_error_code=_opt_str(value["safe_error_code"], "safe_error_code"),
        input_tokens=_int(value["input_tokens"], "input_tokens", optional=True), output_tokens=_int(value["output_tokens"], "output_tokens", optional=True),
        cost_microunits=_int(value["cost_microunits"], "cost_microunits", optional=True),
    )


_OVERLAY_KEYS = {
    "profile_version", "overlay_id", "workspace_id", "purpose", "baseline_digest",
    "candidate_digest", "candidate_refs", "authorised_context_refs", "created_at",
    "expires_at", "classification", "retention_class", "live_selection_enabled",
    "permission_grant_refs",
}


def candidate_overlay_from_content(content: object) -> CandidateOverlay:
    value = _mapping(content, "candidate overlay")
    _exact(value, _OVERLAY_KEYS, CANDIDATE_OVERLAY_PROFILE_VERSION, "candidate overlay")
    return CandidateOverlay(
        overlay_id=_str(value["overlay_id"], "overlay_id"), workspace_id=_str(value["workspace_id"], "workspace_id"),
        purpose=_str(value["purpose"], "purpose"), baseline_digest=_str(value["baseline_digest"], "baseline_digest"),
        candidate_digest=_str(value["candidate_digest"], "candidate_digest"), candidate_refs=_strings(value["candidate_refs"], "candidate_refs"),
        authorised_context_refs=_strings(value["authorised_context_refs"], "authorised_context_refs"),
        created_at=decode_instant(value["created_at"]), expires_at=decode_instant(value["expires_at"]),
        classification=_enum(value["classification"], Classification, "classification"), retention_class=_str(value["retention_class"], "retention_class"),
        live_selection_enabled=_bool(value["live_selection_enabled"], "live_selection_enabled"), permission_grant_refs=_strings(value["permission_grant_refs"], "permission_grant_refs"),
    )


_CASE_KEYS = {
    "profile_version", "case_id", "workspace_id", "owner_domain_ref", "lifecycle",
    "owner_review_ref", "scenario_input_ref", "consumer_visible_input_refs",
    "expected_outcome_ref", "rubric_ref", "consumer_ref", "configuration_ref",
    "required", "critical", "synthetic", "originating_feedback_ref",
    "classification", "retention_class",
}


def evaluation_case_from_content(content: object) -> KnowledgeEvaluationCase:
    value = _mapping(content, "evaluation case")
    _exact(value, _CASE_KEYS, EVALUATION_CASE_PROFILE_VERSION, "evaluation case")
    return KnowledgeEvaluationCase(
        case_id=_str(value["case_id"], "case_id"), workspace_id=_str(value["workspace_id"], "workspace_id"),
        owner_domain_ref=_str(value["owner_domain_ref"], "owner_domain_ref"), lifecycle=_enum(value["lifecycle"], EvaluationCaseLifecycle, "lifecycle"),
        owner_review_ref=_opt_str(value["owner_review_ref"], "owner_review_ref"), scenario_input_ref=_str(value["scenario_input_ref"], "scenario_input_ref"),
        consumer_visible_input_refs=_strings(value["consumer_visible_input_refs"], "consumer_visible_input_refs"),
        expected_outcome_ref=_str(value["expected_outcome_ref"], "expected_outcome_ref"), rubric_ref=_str(value["rubric_ref"], "rubric_ref"),
        consumer_ref=_str(value["consumer_ref"], "consumer_ref"), configuration_ref=_str(value["configuration_ref"], "configuration_ref"),
        required=_bool(value["required"], "required"), critical=_bool(value["critical"], "critical"), synthetic=_bool(value["synthetic"], "synthetic"),
        originating_feedback_ref=_opt_str(value["originating_feedback_ref"], "originating_feedback_ref"),
        classification=_enum(value["classification"], Classification, "classification"), retention_class=_str(value["retention_class"], "retention_class"),
    )


_SUITE_KEYS = {
    "profile_version", "suite_id", "workspace_id", "version_ref", "owner_ref",
    "owner_review_ref", "case_refs", "critical_case_refs", "critical_repeat_count",
    "classification", "retention_class",
}


def evaluation_suite_from_content(content: object) -> KnowledgeEvaluationSuite:
    value = _mapping(content, "evaluation suite")
    _exact(value, _SUITE_KEYS, EVALUATION_SUITE_PROFILE_VERSION, "evaluation suite")
    repeat = _int(value["critical_repeat_count"], "critical_repeat_count")
    assert repeat is not None
    return KnowledgeEvaluationSuite(
        suite_id=_str(value["suite_id"], "suite_id"), workspace_id=_str(value["workspace_id"], "workspace_id"),
        version_ref=_str(value["version_ref"], "version_ref"), owner_ref=_str(value["owner_ref"], "owner_ref"), owner_review_ref=_str(value["owner_review_ref"], "owner_review_ref"),
        case_refs=_strings(value["case_refs"], "case_refs"), critical_case_refs=_strings(value["critical_case_refs"], "critical_case_refs"),
        critical_repeat_count=repeat, classification=_enum(value["classification"], Classification, "classification"), retention_class=_str(value["retention_class"], "retention_class"),
    )


_ATTEMPT_KEYS = {
    "profile_version", "attempt_id", "workspace_id", "case_ref", "ordinal",
    "worker_binding_ref", "overlay_ref", "status", "deterministic_checks",
    "output_ref", "judgement_ref", "safe_error_code", "input_tokens",
    "output_tokens", "cost_microunits",
}


def evaluation_attempt_from_content(content: object) -> EvaluationAttempt:
    value = _mapping(content, "evaluation attempt")
    _exact(value, _ATTEMPT_KEYS, EVALUATION_ATTEMPT_PROFILE_VERSION, "evaluation attempt")
    checks_raw = value["deterministic_checks"]
    require(isinstance(checks_raw, list), GovernedKnowledgeErrorCode.INVALID_FIELD, "deterministic_checks must be a list")
    checks = []
    for raw in checks_raw:
        item = _mapping(raw, "deterministic check")
        require(set(item) == {"check_id", "status", "evidence_ref"}, GovernedKnowledgeErrorCode.INVALID_FIELD, "deterministic check fields are not exact")
        checks.append(DeterministicCheck(_str(item["check_id"], "check_id"), _enum(item["status"], DeterministicCheckStatus, "status"), _str(item["evidence_ref"], "evidence_ref")))
    ordinal = _int(value["ordinal"], "ordinal")
    assert ordinal is not None
    return EvaluationAttempt(
        attempt_id=_str(value["attempt_id"], "attempt_id"), workspace_id=_str(value["workspace_id"], "workspace_id"),
        case_ref=_str(value["case_ref"], "case_ref"), ordinal=ordinal, worker_binding_ref=_str(value["worker_binding_ref"], "worker_binding_ref"),
        overlay_ref=_str(value["overlay_ref"], "overlay_ref"), status=_enum(value["status"], EvaluationResultStatus, "status"),
        deterministic_checks=tuple(checks), output_ref=_opt_str(value["output_ref"], "output_ref"), judgement_ref=_opt_str(value["judgement_ref"], "judgement_ref"),
        safe_error_code=_opt_str(value["safe_error_code"], "safe_error_code"), input_tokens=_int(value["input_tokens"], "input_tokens", optional=True),
        output_tokens=_int(value["output_tokens"], "output_tokens", optional=True), cost_microunits=_int(value["cost_microunits"], "cost_microunits", optional=True),
    )


_REPORT_KEYS = {
    "profile_version", "report_id", "workspace_id", "suite_ref", "suite_version_ref",
    "overlay_ref", "baseline_digest", "candidate_digest", "worker_binding_refs",
    "case_summaries", "deterministic_finding_refs", "total_input_tokens",
    "total_output_tokens", "total_cost_microunits", "coverage_complete", "status",
    "limitation_refs", "classification", "retention_class", "integrity_digest",
}


def evaluation_report_from_content(content: object) -> KnowledgeEvaluationReport:
    value = _mapping(content, "evaluation report")
    _exact(value, _REPORT_KEYS, EVALUATION_REPORT_PROFILE_VERSION, "evaluation report")
    summaries_raw = value["case_summaries"]
    require(isinstance(summaries_raw, list), GovernedKnowledgeErrorCode.INVALID_FIELD, "case_summaries must be a list")
    summaries = []
    for raw in summaries_raw:
        item = _mapping(raw, "case summary")
        require(set(item) == {"case_ref", "attempt_refs", "outcomes"}, GovernedKnowledgeErrorCode.INVALID_FIELD, "case summary fields are not exact")
        summaries.append(EvaluationCaseSummary(_str(item["case_ref"], "case_ref"), _strings(item["attempt_refs"], "attempt_refs"), tuple(_enum(outcome, EvaluationResultStatus, "outcome") for outcome in _strings(item["outcomes"], "outcomes"))))
    totals = [_int(value[name], name) for name in ("total_input_tokens", "total_output_tokens", "total_cost_microunits")]
    assert all(item is not None for item in totals)
    return KnowledgeEvaluationReport(
        report_id=_str(value["report_id"], "report_id"), workspace_id=_str(value["workspace_id"], "workspace_id"), suite_ref=_str(value["suite_ref"], "suite_ref"),
        suite_version_ref=_str(value["suite_version_ref"], "suite_version_ref"), overlay_ref=_str(value["overlay_ref"], "overlay_ref"),
        baseline_digest=_str(value["baseline_digest"], "baseline_digest"), candidate_digest=_str(value["candidate_digest"], "candidate_digest"),
        worker_binding_refs=_strings(value["worker_binding_refs"], "worker_binding_refs"), case_summaries=tuple(summaries),
        deterministic_finding_refs=_strings(value["deterministic_finding_refs"], "deterministic_finding_refs"),
        total_input_tokens=cast(int, totals[0]), total_output_tokens=cast(int, totals[1]), total_cost_microunits=cast(int, totals[2]),
        coverage_complete=_bool(value["coverage_complete"], "coverage_complete"), status=_enum(value["status"], EvaluationReportStatus, "status"),
        limitation_refs=_strings(value["limitation_refs"], "limitation_refs"), classification=_enum(value["classification"], Classification, "classification"),
        retention_class=_str(value["retention_class"], "retention_class"), integrity_digest=_opt_str(value["integrity_digest"], "integrity_digest"),
    )


__all__ = [
    "candidate_overlay_from_content", "candidate_overlay_to_content",
    "diagnosis_attempt_from_content", "diagnosis_attempt_to_content",
    "evaluation_attempt_from_content", "evaluation_attempt_to_content",
    "evaluation_case_from_content", "evaluation_case_to_content",
    "evaluation_report_from_content", "evaluation_report_to_content",
    "evaluation_suite_from_content", "evaluation_suite_to_content",
    "worker_binding_from_content", "worker_binding_to_content",
]
