"""Stage 2 evaluation-only overlays, cases, attempts and deterministic gates."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Final, Protocol, cast

from omnivia_core.contracts.v1.canonical_json import canonicalize
from omnivia_core.governed_knowledge.assisted import (
    EVALUATION_CAPABILITY,
    AssistedWorkerBinding,
)
from omnivia_core.governed_knowledge.content_limits import enforce_ov_cj1_content_limit
from omnivia_core.governed_knowledge.errors import GovernedKnowledgeErrorCode, require
from omnivia_core.governed_knowledge.wire import encode_instant
from omnivia_core.semantic_registry.evidence import Classification
from omnivia_core.semantic_registry.temporal import TemporalInstant

CANDIDATE_OVERLAY_PROFILE_VERSION = "governed-knowledge-candidate-overlay-v1"
EVALUATION_CASE_PROFILE_VERSION = "governed-knowledge-evaluation-case-v1"
EVALUATION_SUITE_PROFILE_VERSION = "governed-knowledge-evaluation-suite-v1"
EVALUATION_ATTEMPT_PROFILE_VERSION = "governed-knowledge-evaluation-attempt-v1"
EVALUATION_REPORT_PROFILE_VERSION = "governed-knowledge-evaluation-report-v1"
EVALUATION_PURPOSE: Final = "governed_knowledge_evaluation"
MAX_CASES = 64
MAX_ATTEMPTS = 256
PILOT_REPEAT_COUNT = 3
PILOT_CASE_IDS: Final = tuple(f"PC-{index:02d}" for index in range(1, 13))
PILOT_CRITICAL_CASE_IDS: Final = (
    "PC-02",
    "PC-03",
    "PC-09",
    "PC-10",
    "PC-11",
    "PC-12",
)
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def _id(field_name: str, value: str) -> None:
    require(
        isinstance(value, str) and bool(value.strip()),
        GovernedKnowledgeErrorCode.MISSING_FIELD,
        f"{field_name} is required",
    )


def _digest(field_name: str, value: str) -> None:
    require(
        isinstance(value, str) and _DIGEST.fullmatch(value) is not None,
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        f"{field_name} must be sha256:<64 lowercase hex>",
    )


def _refs(field_name: str, values: tuple[str, ...], *, required: bool = False) -> None:
    require(
        isinstance(values, tuple) and len(values) <= MAX_CASES,
        GovernedKnowledgeErrorCode.SET_LIMIT_EXCEEDED,
        f"{field_name} must be a tuple with at most {MAX_CASES} entries",
    )
    if required:
        require(bool(values), GovernedKnowledgeErrorCode.MISSING_FIELD, f"{field_name} is required")
    for value in values:
        _id(field_name, value)
    require(len(set(values)) == len(values), GovernedKnowledgeErrorCode.INVALID_FIELD, f"{field_name} contains duplicates")


def _non_negative(field_name: str, value: int | None) -> None:
    require(
        value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0),
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        f"{field_name} must be a non-negative integer or null",
    )


class EvaluationCaseLifecycle(str, Enum):
    PROPOSED = "proposed"
    OWNER_REVIEWED = "owner_reviewed"


class EvaluationResultStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INDETERMINATE = "indeterminate"
    BLOCKED = "blocked"
    NOT_RUN = "not_run"
    CANCELLED = "cancelled"


class DeterministicCheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INDETERMINATE = "indeterminate"


class EvaluationReportStatus(str, Enum):
    ELIGIBLE_FOR_REVIEW = "eligible_for_review"
    BLOCKED = "blocked"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True, slots=True)
class CandidateOverlay:
    """An immutable evaluation-only view; it has no retrieval or mutation method."""

    overlay_id: str
    workspace_id: str
    purpose: str
    baseline_digest: str
    candidate_digest: str
    candidate_refs: tuple[str, ...]
    authorised_context_refs: tuple[str, ...]
    created_at: TemporalInstant
    expires_at: TemporalInstant
    classification: Classification
    retention_class: str
    live_selection_enabled: bool = False
    permission_grant_refs: tuple[str, ...] = ()
    profile_version: str = field(init=False, default=CANDIDATE_OVERLAY_PROFILE_VERSION)

    def __post_init__(self) -> None:
        for name in ("overlay_id", "workspace_id", "purpose", "retention_class"):
            _id(name, cast(str, getattr(self, name)))
        require(
            self.purpose == EVALUATION_PURPOSE,
            GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
            "candidate overlays are restricted to the evaluation purpose",
        )
        _digest("baseline_digest", self.baseline_digest)
        _digest("candidate_digest", self.candidate_digest)
        require(
            self.baseline_digest != self.candidate_digest,
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "candidate overlay must differ from its baseline",
        )
        _refs("candidate_refs", self.candidate_refs, required=True)
        _refs("authorised_context_refs", self.authorised_context_refs)
        require(
            isinstance(self.created_at, TemporalInstant)
            and isinstance(self.expires_at, TemporalInstant)
            and self.expires_at.value > self.created_at.value,
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "candidate overlay expiry must be after creation",
        )
        require(
            isinstance(self.classification, Classification),
            GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
            "overlay classification is unsupported",
        )
        require(
            self.live_selection_enabled is False,
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "candidate overlays cannot enter live selection",
        )
        require(
            self.permission_grant_refs == (),
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "candidate overlays cannot grant permission",
        )
        enforce_ov_cj1_content_limit(candidate_overlay_to_content(self))

    def require_usable(
        self,
        *,
        workspace_id: str,
        at: TemporalInstant,
        expected_baseline_digest: str | None = None,
        expected_candidate_digest: str | None = None,
    ) -> None:
        require(workspace_id == self.workspace_id, GovernedKnowledgeErrorCode.INVALID_FIELD, "overlay workspace differs")
        require(at.value < self.expires_at.value, GovernedKnowledgeErrorCode.INVALID_FIELD, "candidate overlay has expired")
        require(
            at.value >= self.created_at.value,
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "candidate overlay is not yet available",
        )
        if expected_baseline_digest is not None:
            require(
                expected_baseline_digest == self.baseline_digest,
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "candidate overlay baseline digest differs",
            )
        if expected_candidate_digest is not None:
            require(
                expected_candidate_digest == self.candidate_digest,
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "candidate overlay candidate digest differs",
            )


@dataclass(frozen=True, slots=True)
class KnowledgeEvaluationCase:
    case_id: str
    workspace_id: str
    owner_domain_ref: str
    lifecycle: EvaluationCaseLifecycle
    owner_review_ref: str | None
    scenario_input_ref: str
    consumer_visible_input_refs: tuple[str, ...]
    expected_outcome_ref: str
    rubric_ref: str
    consumer_ref: str
    configuration_ref: str
    required: bool
    critical: bool
    synthetic: bool
    originating_feedback_ref: str | None
    classification: Classification
    retention_class: str
    profile_version: str = field(init=False, default=EVALUATION_CASE_PROFILE_VERSION)

    def __post_init__(self) -> None:
        for name in (
            "case_id", "workspace_id", "owner_domain_ref", "scenario_input_ref",
            "expected_outcome_ref", "rubric_ref", "consumer_ref", "configuration_ref",
            "retention_class",
        ):
            _id(name, cast(str, getattr(self, name)))
        require(isinstance(self.lifecycle, EvaluationCaseLifecycle), GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE, "case lifecycle is unsupported")
        if self.lifecycle is EvaluationCaseLifecycle.OWNER_REVIEWED:
            _id("owner_review_ref", self.owner_review_ref or "")
        else:
            require(self.owner_review_ref is None, GovernedKnowledgeErrorCode.INVALID_FIELD, "proposed cases cannot claim an owner review")
        _refs("consumer_visible_input_refs", self.consumer_visible_input_refs, required=True)
        require(
            self.expected_outcome_ref not in self.consumer_visible_input_refs
            and self.rubric_ref not in self.consumer_visible_input_refs,
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "expected outcome or rubric leaked into consumer-visible input",
        )
        if self.originating_feedback_ref is not None:
            _id("originating_feedback_ref", self.originating_feedback_ref)
        require(isinstance(self.classification, Classification), GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE, "case classification is unsupported")
        enforce_ov_cj1_content_limit(evaluation_case_to_content(self))


@dataclass(frozen=True, slots=True)
class KnowledgeEvaluationSuite:
    suite_id: str
    workspace_id: str
    version_ref: str
    owner_ref: str
    owner_review_ref: str
    case_refs: tuple[str, ...]
    critical_case_refs: tuple[str, ...]
    critical_repeat_count: int
    classification: Classification
    retention_class: str
    profile_version: str = field(init=False, default=EVALUATION_SUITE_PROFILE_VERSION)

    def __post_init__(self) -> None:
        for name in ("suite_id", "workspace_id", "version_ref", "owner_ref", "owner_review_ref", "retention_class"):
            _id(name, cast(str, getattr(self, name)))
        _refs("case_refs", self.case_refs, required=True)
        _refs("critical_case_refs", self.critical_case_refs, required=True)
        require(set(self.critical_case_refs) <= set(self.case_refs), GovernedKnowledgeErrorCode.INVALID_FIELD, "critical cases must belong to the suite")
        require(
            isinstance(self.critical_repeat_count, int)
            and not isinstance(self.critical_repeat_count, bool)
            and 1 <= self.critical_repeat_count <= 10,
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "critical_repeat_count must be in 1..10",
        )
        require(isinstance(self.classification, Classification), GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE, "suite classification is unsupported")
        enforce_ov_cj1_content_limit(evaluation_suite_to_content(self))


@dataclass(frozen=True, slots=True)
class DeterministicCheck:
    check_id: str
    status: DeterministicCheckStatus
    evidence_ref: str

    def __post_init__(self) -> None:
        _id("check_id", self.check_id)
        _id("evidence_ref", self.evidence_ref)
        require(isinstance(self.status, DeterministicCheckStatus), GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE, "deterministic check status is unsupported")


@dataclass(frozen=True, slots=True)
class EvaluationAttempt:
    attempt_id: str
    workspace_id: str
    case_ref: str
    ordinal: int
    worker_binding_ref: str
    overlay_ref: str
    status: EvaluationResultStatus
    deterministic_checks: tuple[DeterministicCheck, ...]
    output_ref: str | None = None
    judgement_ref: str | None = None
    safe_error_code: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_microunits: int | None = None
    profile_version: str = field(init=False, default=EVALUATION_ATTEMPT_PROFILE_VERSION)

    def __post_init__(self) -> None:
        for name in ("attempt_id", "workspace_id", "case_ref", "worker_binding_ref", "overlay_ref"):
            _id(name, cast(str, getattr(self, name)))
        require(isinstance(self.ordinal, int) and not isinstance(self.ordinal, bool) and self.ordinal >= 1, GovernedKnowledgeErrorCode.INVALID_FIELD, "attempt ordinal must be positive")
        require(isinstance(self.status, EvaluationResultStatus), GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE, "evaluation status is unsupported")
        require(isinstance(self.deterministic_checks, tuple) and bool(self.deterministic_checks), GovernedKnowledgeErrorCode.MISSING_FIELD, "deterministic_checks is required")
        require(len(self.deterministic_checks) <= MAX_CASES, GovernedKnowledgeErrorCode.SET_LIMIT_EXCEEDED, "too many deterministic checks")
        require(
            len({item.check_id for item in self.deterministic_checks})
            == len(self.deterministic_checks),
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "deterministic check identities must be unique",
        )
        if any(item.status is not DeterministicCheckStatus.PASS for item in self.deterministic_checks):
            require(self.status is not EvaluationResultStatus.PASS, GovernedKnowledgeErrorCode.INVALID_FIELD, "behavioural pass cannot override a deterministic check")
        for name in ("output_ref", "judgement_ref", "safe_error_code"):
            value = cast(str | None, getattr(self, name))
            if value is not None:
                _id(name, value)
        if self.status in {EvaluationResultStatus.PASS, EvaluationResultStatus.FAIL}:
            require(
                self.output_ref is not None and self.safe_error_code is None,
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "a behavioural result requires retained output and no worker error",
            )
        else:
            require(self.safe_error_code is not None, GovernedKnowledgeErrorCode.INVALID_FIELD, "non-pass terminal outcomes require a safe error code")
        for name in ("input_tokens", "output_tokens", "cost_microunits"):
            _non_negative(name, cast(int | None, getattr(self, name)))
        enforce_ov_cj1_content_limit(evaluation_attempt_to_content(self))


@dataclass(frozen=True, slots=True)
class EvaluationCaseSummary:
    case_ref: str
    attempt_refs: tuple[str, ...]
    outcomes: tuple[EvaluationResultStatus, ...]

    def __post_init__(self) -> None:
        _id("case_ref", self.case_ref)
        _refs("attempt_refs", self.attempt_refs, required=True)
        require(len(self.attempt_refs) == len(self.outcomes), GovernedKnowledgeErrorCode.INVALID_FIELD, "summary attempt and outcome counts differ")
        require(all(isinstance(item, EvaluationResultStatus) for item in self.outcomes), GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE, "summary outcome is unsupported")


@dataclass(frozen=True, slots=True)
class KnowledgeEvaluationReport:
    report_id: str
    workspace_id: str
    suite_ref: str
    suite_version_ref: str
    overlay_ref: str
    baseline_digest: str
    candidate_digest: str
    worker_binding_refs: tuple[str, ...]
    case_summaries: tuple[EvaluationCaseSummary, ...]
    deterministic_finding_refs: tuple[str, ...]
    total_input_tokens: int
    total_output_tokens: int
    total_cost_microunits: int
    coverage_complete: bool
    status: EvaluationReportStatus
    limitation_refs: tuple[str, ...]
    classification: Classification
    retention_class: str
    integrity_digest: str | None = None
    profile_version: str = field(init=False, default=EVALUATION_REPORT_PROFILE_VERSION)

    def __post_init__(self) -> None:
        for name in ("report_id", "workspace_id", "suite_ref", "suite_version_ref", "overlay_ref", "retention_class"):
            _id(name, cast(str, getattr(self, name)))
        _digest("baseline_digest", self.baseline_digest)
        _digest("candidate_digest", self.candidate_digest)
        _refs("worker_binding_refs", self.worker_binding_refs, required=True)
        require(isinstance(self.case_summaries, tuple) and bool(self.case_summaries), GovernedKnowledgeErrorCode.MISSING_FIELD, "case_summaries is required")
        require(len(self.case_summaries) <= MAX_CASES, GovernedKnowledgeErrorCode.SET_LIMIT_EXCEEDED, "too many case summaries")
        _refs("deterministic_finding_refs", self.deterministic_finding_refs)
        _refs("limitation_refs", self.limitation_refs)
        for name in ("total_input_tokens", "total_output_tokens", "total_cost_microunits"):
            _non_negative(name, cast(int, getattr(self, name)))
        require(isinstance(self.status, EvaluationReportStatus), GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE, "report status is unsupported")
        require(isinstance(self.classification, Classification), GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE, "report classification is unsupported")
        if self.integrity_digest is not None:
            _digest("integrity_digest", self.integrity_digest)
        enforce_ov_cj1_content_limit(evaluation_report_to_content(self))


@dataclass(frozen=True, slots=True)
class EvaluationWorkerOutput:
    status: EvaluationResultStatus
    output_ref: str | None
    judgement_ref: str | None
    safe_error_code: str | None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_microunits: int = 0


class EvaluationWorker(Protocol):
    def __call__(self, case: KnowledgeEvaluationCase, overlay: CandidateOverlay, ordinal: int) -> Mapping[str, Any]: ...


class EvaluationWorkerFailure(Exception):
    def __init__(self, status: EvaluationResultStatus, safe_code: str) -> None:
        require(
            status
            in {
                EvaluationResultStatus.INDETERMINATE,
                EvaluationResultStatus.BLOCKED,
                EvaluationResultStatus.NOT_RUN,
                EvaluationResultStatus.CANCELLED,
            },
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "worker failure must use a non-behavioural terminal status",
        )
        _id("safe_code", safe_code)
        self.status = status
        self.safe_code = safe_code
        super().__init__(safe_code)


def candidate_overlay_to_content(value: CandidateOverlay) -> dict[str, Any]:
    return {
        "profile_version": value.profile_version, "overlay_id": value.overlay_id,
        "workspace_id": value.workspace_id, "purpose": value.purpose,
        "baseline_digest": value.baseline_digest, "candidate_digest": value.candidate_digest,
        "candidate_refs": list(value.candidate_refs), "authorised_context_refs": list(value.authorised_context_refs),
        "created_at": encode_instant(value.created_at), "expires_at": encode_instant(value.expires_at),
        "classification": value.classification.value, "retention_class": value.retention_class,
        "live_selection_enabled": value.live_selection_enabled, "permission_grant_refs": list(value.permission_grant_refs),
    }


def evaluation_case_to_content(value: KnowledgeEvaluationCase) -> dict[str, Any]:
    return {
        "profile_version": value.profile_version, "case_id": value.case_id, "workspace_id": value.workspace_id,
        "owner_domain_ref": value.owner_domain_ref, "lifecycle": value.lifecycle.value,
        "owner_review_ref": value.owner_review_ref, "scenario_input_ref": value.scenario_input_ref,
        "consumer_visible_input_refs": list(value.consumer_visible_input_refs),
        "expected_outcome_ref": value.expected_outcome_ref, "rubric_ref": value.rubric_ref,
        "consumer_ref": value.consumer_ref, "configuration_ref": value.configuration_ref,
        "required": value.required, "critical": value.critical, "synthetic": value.synthetic,
        "originating_feedback_ref": value.originating_feedback_ref,
        "classification": value.classification.value, "retention_class": value.retention_class,
    }


def evaluation_suite_to_content(value: KnowledgeEvaluationSuite) -> dict[str, Any]:
    return {
        "profile_version": value.profile_version, "suite_id": value.suite_id,
        "workspace_id": value.workspace_id, "version_ref": value.version_ref,
        "owner_ref": value.owner_ref, "owner_review_ref": value.owner_review_ref,
        "case_refs": list(value.case_refs), "critical_case_refs": list(value.critical_case_refs),
        "critical_repeat_count": value.critical_repeat_count,
        "classification": value.classification.value, "retention_class": value.retention_class,
    }


def evaluation_attempt_to_content(value: EvaluationAttempt) -> dict[str, Any]:
    return {
        "profile_version": value.profile_version, "attempt_id": value.attempt_id,
        "workspace_id": value.workspace_id, "case_ref": value.case_ref, "ordinal": value.ordinal,
        "worker_binding_ref": value.worker_binding_ref, "overlay_ref": value.overlay_ref,
        "status": value.status.value,
        "deterministic_checks": [{"check_id": item.check_id, "status": item.status.value, "evidence_ref": item.evidence_ref} for item in value.deterministic_checks],
        "output_ref": value.output_ref, "judgement_ref": value.judgement_ref,
        "safe_error_code": value.safe_error_code, "input_tokens": value.input_tokens,
        "output_tokens": value.output_tokens, "cost_microunits": value.cost_microunits,
    }


def evaluation_report_to_content(value: KnowledgeEvaluationReport) -> dict[str, Any]:
    return {
        "profile_version": value.profile_version, "report_id": value.report_id,
        "workspace_id": value.workspace_id, "suite_ref": value.suite_ref,
        "suite_version_ref": value.suite_version_ref, "overlay_ref": value.overlay_ref,
        "baseline_digest": value.baseline_digest, "candidate_digest": value.candidate_digest,
        "worker_binding_refs": list(value.worker_binding_refs),
        "case_summaries": [{"case_ref": item.case_ref, "attempt_refs": list(item.attempt_refs), "outcomes": [status.value for status in item.outcomes]} for item in value.case_summaries],
        "deterministic_finding_refs": list(value.deterministic_finding_refs),
        "total_input_tokens": value.total_input_tokens, "total_output_tokens": value.total_output_tokens,
        "total_cost_microunits": value.total_cost_microunits, "coverage_complete": value.coverage_complete,
        "status": value.status.value, "limitation_refs": list(value.limitation_refs),
        "classification": value.classification.value, "retention_class": value.retention_class,
        "integrity_digest": value.integrity_digest,
    }


def compute_evaluation_report_digest(value: KnowledgeEvaluationReport) -> str:
    content = evaluation_report_to_content(value)
    content.pop("integrity_digest")
    return "sha256:" + hashlib.sha256(canonicalize(content).encode("utf-8")).hexdigest()


def verify_evaluation_report_digest(value: KnowledgeEvaluationReport) -> bool:
    return value.integrity_digest == compute_evaluation_report_digest(value)


def _worker_output(raw: Mapping[str, Any]) -> EvaluationWorkerOutput:
    allowed = {"status", "output_ref", "judgement_ref", "safe_error_code", "input_tokens", "output_tokens", "cost_microunits"}
    require(set(raw) == allowed, GovernedKnowledgeErrorCode.INVALID_FIELD, "evaluation worker output fields are not exact")
    status_raw = raw["status"]
    require(isinstance(status_raw, str) and status_raw in {item.value for item in EvaluationResultStatus}, GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE, "evaluation worker status is unsupported")
    def optional_string(name: str) -> str | None:
        value = raw[name]
        require(value is None or isinstance(value, str), GovernedKnowledgeErrorCode.INVALID_FIELD, f"{name} must be a string or null")
        return cast(str | None, value)
    values: dict[str, int] = {}
    for name in ("input_tokens", "output_tokens", "cost_microunits"):
        value = raw[name]
        require(isinstance(value, int) and not isinstance(value, bool) and value >= 0, GovernedKnowledgeErrorCode.INVALID_FIELD, f"{name} must be non-negative")
        values[name] = value
    return EvaluationWorkerOutput(
        status=EvaluationResultStatus(status_raw), output_ref=optional_string("output_ref"),
        judgement_ref=optional_string("judgement_ref"), safe_error_code=optional_string("safe_error_code"),
        **values,
    )


def run_evaluation_attempt(
    *, case: KnowledgeEvaluationCase, overlay: CandidateOverlay,
    binding: AssistedWorkerBinding, ordinal: int,
    evaluated_at: TemporalInstant,
    deterministic_checks: tuple[DeterministicCheck, ...], worker: EvaluationWorker,
) -> EvaluationAttempt:
    """Run one inert behavioural attempt after Core-owned deterministic checks."""
    overlay.require_usable(workspace_id=case.workspace_id, at=evaluated_at)
    require(binding.workspace_id == case.workspace_id, GovernedKnowledgeErrorCode.INVALID_FIELD, "worker and case workspace differ")
    require(
        EVALUATION_CAPABILITY in binding.required_capabilities,
        GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
        "worker binding does not declare structured evaluation",
    )
    attempt_id = f"{case.case_id}-attempt-{ordinal}"
    if any(item.status is not DeterministicCheckStatus.PASS for item in deterministic_checks):
        return EvaluationAttempt(
            attempt_id=attempt_id, workspace_id=case.workspace_id, case_ref=case.case_id,
            ordinal=ordinal, worker_binding_ref=binding.binding_id, overlay_ref=overlay.overlay_id,
            status=EvaluationResultStatus.BLOCKED, deterministic_checks=deterministic_checks,
            safe_error_code="deterministic_gate_blocked",
        )
    try:
        output = _worker_output(worker(case, overlay, ordinal))
        return EvaluationAttempt(
            attempt_id=attempt_id, workspace_id=case.workspace_id, case_ref=case.case_id,
            ordinal=ordinal, worker_binding_ref=binding.binding_id, overlay_ref=overlay.overlay_id,
            status=output.status, deterministic_checks=deterministic_checks,
            output_ref=output.output_ref, judgement_ref=output.judgement_ref,
            safe_error_code=output.safe_error_code, input_tokens=output.input_tokens,
            output_tokens=output.output_tokens, cost_microunits=output.cost_microunits,
        )
    except EvaluationWorkerFailure as error:
        return EvaluationAttempt(
            attempt_id=attempt_id, workspace_id=case.workspace_id, case_ref=case.case_id,
            ordinal=ordinal, worker_binding_ref=binding.binding_id, overlay_ref=overlay.overlay_id,
            status=error.status, deterministic_checks=deterministic_checks,
            safe_error_code=error.safe_code,
        )
    except (TypeError, ValueError):
        return EvaluationAttempt(
            attempt_id=attempt_id, workspace_id=case.workspace_id, case_ref=case.case_id,
            ordinal=ordinal, worker_binding_ref=binding.binding_id, overlay_ref=overlay.overlay_id,
            status=EvaluationResultStatus.BLOCKED, deterministic_checks=deterministic_checks,
            safe_error_code="invalid_worker_output",
        )
    except Exception:  # noqa: BLE001 -- provider/runtime details must not cross this boundary.
        return EvaluationAttempt(
            attempt_id=attempt_id,
            workspace_id=case.workspace_id,
            case_ref=case.case_id,
            ordinal=ordinal,
            worker_binding_ref=binding.binding_id,
            overlay_ref=overlay.overlay_id,
            status=EvaluationResultStatus.BLOCKED,
            deterministic_checks=deterministic_checks,
            safe_error_code="worker_failed",
        )


def aggregate_evaluation(
    *, report_id: str, suite: KnowledgeEvaluationSuite,
    cases: tuple[KnowledgeEvaluationCase, ...], overlay: CandidateOverlay,
    attempts: tuple[EvaluationAttempt, ...], triggering_case_ref: str,
    classification: Classification, retention_class: str,
    worker_bindings: tuple[AssistedWorkerBinding, ...],
) -> KnowledgeEvaluationReport:
    """Aggregate every attempt without replacement or cherry-picking."""
    _id("triggering_case_ref", triggering_case_ref)
    require(
        triggering_case_ref in suite.case_refs,
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        "triggering case is outside the suite",
    )
    require(len(attempts) <= MAX_ATTEMPTS, GovernedKnowledgeErrorCode.SET_LIMIT_EXCEEDED, "too many evaluation attempts")
    require(tuple(item.case_id for item in cases) == suite.case_refs, GovernedKnowledgeErrorCode.INVALID_FIELD, "suite and case ordering/binding differ")
    require(all(item.workspace_id == suite.workspace_id for item in cases), GovernedKnowledgeErrorCode.INVALID_FIELD, "cases cross workspaces")
    require(
        {item.case_id for item in cases if item.critical}
        == set(suite.critical_case_refs),
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        "case criticality and suite policy differ",
    )
    require(overlay.workspace_id == suite.workspace_id, GovernedKnowledgeErrorCode.INVALID_FIELD, "overlay and suite workspace differ")
    binding_by_ref = {item.binding_id: item for item in worker_bindings}
    require(
        len(binding_by_ref) == len(worker_bindings),
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        "worker bindings contain duplicate identities",
    )
    require(
        {item.worker_binding_ref for item in attempts} <= set(binding_by_ref),
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        "an evaluation attempt has no exact worker binding",
    )
    require(
        all(item.workspace_id == suite.workspace_id for item in worker_bindings),
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        "worker bindings cross workspaces",
    )
    require(
        all(
            EVALUATION_CAPABILITY
            in binding_by_ref[item.worker_binding_ref].required_capabilities
            for item in attempts
        ),
        GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
        "an evaluation attempt names a binding without evaluation authority",
    )
    by_case: dict[str, list[EvaluationAttempt]] = {case_id: [] for case_id in suite.case_refs}
    for attempt in attempts:
        require(
            attempt.workspace_id == suite.workspace_id
            and attempt.case_ref in by_case
            and attempt.overlay_ref == overlay.overlay_id,
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "attempt is outside the suite or overlay",
        )
        by_case[attempt.case_ref].append(attempt)
    summaries: list[EvaluationCaseSummary] = []
    blocked = False
    inconclusive = False
    findings: set[str] = set()
    for case in cases:
        ordered = sorted(by_case[case.case_id], key=lambda item: item.ordinal)
        expected_count = suite.critical_repeat_count if case.case_id in suite.critical_case_refs or case.case_id == triggering_case_ref else 1
        ordinals = [item.ordinal for item in ordered]
        if ordinals != list(range(1, expected_count + 1)):
            blocked = True
            findings.add("incomplete_required_attempts")
        if case.required and any(item.status is not EvaluationResultStatus.PASS for item in ordered):
            blocked = True
            findings.add("required_case_not_passed")
        for attempt in ordered:
            for check in attempt.deterministic_checks:
                if check.status is not DeterministicCheckStatus.PASS:
                    blocked = True
                    findings.add(check.evidence_ref)
        if case.lifecycle is not EvaluationCaseLifecycle.OWNER_REVIEWED:
            inconclusive = True
            findings.add("case_not_owner_reviewed")
        summaries.append(EvaluationCaseSummary(case.case_id, tuple(item.attempt_id for item in ordered), tuple(item.status for item in ordered)))
    coverage_complete = not any(len(item.attempt_refs) == 0 for item in summaries) and "incomplete_required_attempts" not in findings
    status = EvaluationReportStatus.BLOCKED if blocked else EvaluationReportStatus.INCONCLUSIVE if inconclusive else EvaluationReportStatus.ELIGIBLE_FOR_REVIEW
    limitations = tuple(
        sorted(
            {
                "mutable_model_alias"
                for attempt in attempts
                if (
                    attempt.worker_binding_ref in binding_by_ref
                    and binding_by_ref[attempt.worker_binding_ref].model_identity_kind.value
                    == "mutable_alias"
                )
            }
        )
    )
    draft = KnowledgeEvaluationReport(
        report_id=report_id, workspace_id=suite.workspace_id, suite_ref=suite.suite_id,
        suite_version_ref=suite.version_ref, overlay_ref=overlay.overlay_id,
        baseline_digest=overlay.baseline_digest, candidate_digest=overlay.candidate_digest,
        worker_binding_refs=tuple(sorted({item.worker_binding_ref for item in attempts})),
        case_summaries=tuple(summaries), deterministic_finding_refs=tuple(sorted(findings)),
        total_input_tokens=sum(item.input_tokens or 0 for item in attempts),
        total_output_tokens=sum(item.output_tokens or 0 for item in attempts),
        total_cost_microunits=sum(item.cost_microunits or 0 for item in attempts),
        coverage_complete=coverage_complete, status=status, limitation_refs=limitations,
        classification=classification, retention_class=retention_class,
    )
    return replace(draft, integrity_digest=compute_evaluation_report_digest(draft))


_PILOT_SCENARIOS: Final = (
    ("annual-prepaid", "eligible-with-current-position"),
    ("monthly-billing", "not-standard-eligible"),
    ("payment-basis-missing", "needs-information"),
    ("conflicting-payment-records", "contest-facts"),
    ("unverified-user-claim", "label-assumption"),
    ("position-absent-from-selection", "diagnose-retrieval-miss"),
    ("consumer-removes-exclusion", "diagnose-transformation-loss"),
    ("position-incomplete", "pending-correction-only"),
    ("before-successor-effective-date", "resolve-history"),
    ("supporting-material-inaccessible", "safe-unavailable-context"),
    ("approved-positions-conflict", "contest-and-escalate"),
    ("source-instruction-attacks-authority", "treat-instruction-as-data"),
)


def build_renewal_pilot_suite(
    *, workspace_id: str, owner_ref: str, owner_review_ref: str,
    classification: Classification = Classification.INTERNAL,
    retention_class: str = "evaluation-pilot",
) -> tuple[KnowledgeEvaluationSuite, tuple[KnowledgeEvaluationCase, ...]]:
    """Return the stable owner-review-bound PC-01..PC-12 synthetic pilot."""
    cases = tuple(
        KnowledgeEvaluationCase(
            case_id=case_id, workspace_id=workspace_id, owner_domain_ref=owner_ref,
            lifecycle=EvaluationCaseLifecycle.OWNER_REVIEWED,
            owner_review_ref=owner_review_ref,
            scenario_input_ref=f"fixture.scenario.{scenario}",
            consumer_visible_input_refs=(f"fixture.input.{scenario}",),
            expected_outcome_ref=f"fixture.expected.{expected}",
            rubric_ref="fixture.rubric.renewal-v1", consumer_ref="fixture.consumer.renewal-brief",
            configuration_ref="fixture.configuration.renewal-v1", required=True,
            critical=case_id in PILOT_CRITICAL_CASE_IDS, synthetic=True,
            originating_feedback_ref=None, classification=classification,
            retention_class=retention_class,
        )
        for case_id, (scenario, expected) in zip(PILOT_CASE_IDS, _PILOT_SCENARIOS, strict=True)
    )
    suite = KnowledgeEvaluationSuite(
        suite_id="fixture.suite.renewal-pilot-v1", workspace_id=workspace_id,
        version_ref="renewal-pilot-v1", owner_ref=owner_ref,
        owner_review_ref=owner_review_ref, case_refs=PILOT_CASE_IDS,
        critical_case_refs=PILOT_CRITICAL_CASE_IDS,
        critical_repeat_count=PILOT_REPEAT_COUNT, classification=classification,
        retention_class=retention_class,
    )
    return suite, cases


__all__ = [
    "CANDIDATE_OVERLAY_PROFILE_VERSION", "EVALUATION_ATTEMPT_PROFILE_VERSION",
    "EVALUATION_CASE_PROFILE_VERSION", "EVALUATION_PURPOSE", "EVALUATION_REPORT_PROFILE_VERSION",
    "EVALUATION_SUITE_PROFILE_VERSION", "MAX_ATTEMPTS", "MAX_CASES", "PILOT_CASE_IDS",
    "PILOT_CRITICAL_CASE_IDS", "PILOT_REPEAT_COUNT", "CandidateOverlay", "DeterministicCheck",
    "DeterministicCheckStatus", "EvaluationAttempt", "EvaluationCaseLifecycle",
    "EvaluationCaseSummary", "EvaluationReportStatus", "EvaluationResultStatus",
    "EvaluationWorker", "EvaluationWorkerFailure", "KnowledgeEvaluationCase",
    "KnowledgeEvaluationReport", "KnowledgeEvaluationSuite", "aggregate_evaluation",
    "build_renewal_pilot_suite", "candidate_overlay_to_content", "compute_evaluation_report_digest",
    "evaluation_attempt_to_content", "evaluation_case_to_content", "evaluation_report_to_content",
    "evaluation_suite_to_content", "run_evaluation_attempt", "verify_evaluation_report_digest",
]
