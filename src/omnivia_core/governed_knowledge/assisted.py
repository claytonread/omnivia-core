"""Stage 2 bounded assisted diagnosis over an injected, host-authorised worker.

The module is deliberately portable and inert.  It owns structured values and
validation only: no provider SDK, credential, tool, database, network or
filesystem handle is reachable from :func:`run_assisted_diagnosis`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, cast

from omnivia_core.governed_knowledge.content_limits import enforce_ov_cj1_content_limit
from omnivia_core.governed_knowledge.errors import (
    GovernedKnowledgeErrorCode,
    GovernedKnowledgeValidationError,
    require,
)
from omnivia_core.governed_knowledge.feedback import (
    ExpertFeedback,
    FeedbackDiagnosis,
    KnowledgeImprovementProposal,
)
from omnivia_core.governed_knowledge.profile_content import (
    diagnosis_from_content,
    diagnosis_to_content,
    proposal_from_content,
    proposal_to_content,
)
from omnivia_core.governed_knowledge.wire import decode_span, encode_span
from omnivia_core.semantic_registry.evidence import EvidenceSpan

FEEDBACK_ISSUE_PROFILE_VERSION = "governed-knowledge-feedback-issue-v1"
WORKER_BINDING_PROFILE_VERSION = "governed-knowledge-worker-binding-v1"
DIAGNOSIS_ATTEMPT_PROFILE_VERSION = "governed-knowledge-diagnosis-attempt-v1"
MAX_ISSUES = 16
MAX_REFERENCES = 64
MAX_DEADLINE_MS = 300_000
DIAGNOSIS_CAPABILITY = "governed_knowledge.diagnose"
EVALUATION_CAPABILITY = "governed_knowledge.evaluate"
STAGE2_WORKER_CAPABILITIES = frozenset(
    {DIAGNOSIS_CAPABILITY, EVALUATION_CAPABILITY}
)

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_FORBIDDEN_WORKER_KEYS = frozenset(
    {
        "action",
        "actions",
        "admission",
        "admission_reference",
        "admitted",
        "approval",
        "approved",
        "capability_grant",
        "credentials",
        "grant",
        "permission",
        "permissions",
        "tool",
        "tool_call",
        "tool_calls",
        "tools",
    }
)


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
        isinstance(values, tuple),
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        f"{field_name} must be a tuple",
    )
    require(
        len(values) <= MAX_REFERENCES,
        GovernedKnowledgeErrorCode.SET_LIMIT_EXCEEDED,
        f"{field_name} exceeds {MAX_REFERENCES} entries",
    )
    if required:
        require(
            bool(values),
            GovernedKnowledgeErrorCode.MISSING_FIELD,
            f"{field_name} is required",
        )
    for value in values:
        _id(field_name, value)
    require(
        len(set(values)) == len(values),
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        f"{field_name} contains duplicates",
    )


class ModelIdentityKind(str, Enum):
    EXACT = "exact"
    MUTABLE_ALIAS = "mutable_alias"
    NOT_APPLICABLE = "not_applicable"


class DiagnosisAttemptStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    INVALID_OUTPUT = "invalid_output"


@dataclass(frozen=True, slots=True)
class FeedbackIssue:
    """One bounded issue extracted from immutable feedback evidence."""

    issue_id: str
    workspace_id: str
    feedback_ref: str
    affected_claim: str
    proposed_correction: str
    feedback_spans: tuple[EvidenceSpan, ...]
    supporting_evidence_refs: tuple[str, ...] = ()
    task_context_refs: tuple[str, ...] = ()
    source_lineage_refs: tuple[str, ...] = ()
    profile_version: str = field(init=False, default=FEEDBACK_ISSUE_PROFILE_VERSION)

    def __post_init__(self) -> None:
        for name in (
            "issue_id",
            "workspace_id",
            "feedback_ref",
            "affected_claim",
            "proposed_correction",
        ):
            _id(name, cast(str, getattr(self, name)))
        require(
            isinstance(self.feedback_spans, tuple) and bool(self.feedback_spans),
            GovernedKnowledgeErrorCode.MISSING_FIELD,
            "feedback_spans must be a non-empty tuple",
        )
        require(
            len(self.feedback_spans) <= MAX_ISSUES,
            GovernedKnowledgeErrorCode.SET_LIMIT_EXCEEDED,
            f"feedback_spans exceeds {MAX_ISSUES} entries",
        )
        for span in self.feedback_spans:
            require(
                isinstance(span, EvidenceSpan),
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "feedback_spans entries must be EvidenceSpan values",
            )
        for name in (
            "supporting_evidence_refs",
            "task_context_refs",
            "source_lineage_refs",
        ):
            _refs(name, cast(tuple[str, ...], getattr(self, name)))
        enforce_ov_cj1_content_limit(feedback_issue_to_content(self))


@dataclass(frozen=True, slots=True)
class AssistedWorkerBinding:
    """Exact host-issued evidence for one already-authorised worker route.

    This value is not a grant.  The host must resolve and reauthorise it before
    invocation; Core merely preserves what exact build and policy were used.
    """

    binding_id: str
    workspace_id: str
    source_id: str
    executor_id: str
    executor_version: str
    executor_build_hash: str
    executor_content_hash: str
    runtime_profile_ref: str
    policy_ref: str
    required_capabilities: tuple[str, ...]
    minimum_isolation: int
    resolved_isolation: int
    run_ref: str
    step_ref: str
    attempt_ref: str
    provider_ref: str | None
    model_ref: str | None
    model_identity_kind: ModelIdentityKind
    observed_at_ref: str
    profile_version: str = field(init=False, default=WORKER_BINDING_PROFILE_VERSION)

    def __post_init__(self) -> None:
        for name in (
            "binding_id",
            "workspace_id",
            "source_id",
            "executor_id",
            "executor_version",
            "runtime_profile_ref",
            "policy_ref",
            "run_ref",
            "step_ref",
            "attempt_ref",
            "observed_at_ref",
        ):
            _id(name, cast(str, getattr(self, name)))
        _digest("executor_build_hash", self.executor_build_hash)
        _digest("executor_content_hash", self.executor_content_hash)
        _refs("required_capabilities", self.required_capabilities, required=True)
        require(
            set(self.required_capabilities) <= STAGE2_WORKER_CAPABILITIES,
            GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
            "the Stage 2 worker may declare only diagnosis or evaluation",
        )
        require(
            self.required_capabilities == tuple(sorted(self.required_capabilities)),
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "required_capabilities must use deterministic order",
        )
        for name, value in (
            ("minimum_isolation", self.minimum_isolation),
            ("resolved_isolation", self.resolved_isolation),
        ):
            require(
                isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 3,
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                f"{name} must be an isolation ordinal in 0..3",
            )
        require(
            self.resolved_isolation >= self.minimum_isolation,
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "resolved isolation is below the policy minimum",
        )
        require(
            isinstance(self.model_identity_kind, ModelIdentityKind),
            GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
            "model_identity_kind is unsupported",
        )
        if self.model_identity_kind is ModelIdentityKind.NOT_APPLICABLE:
            require(
                self.provider_ref is None and self.model_ref is None,
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "non-model workers must not claim provider or model identity",
            )
        else:
            _id("provider_ref", self.provider_ref or "")
            _id("model_ref", self.model_ref or "")
        enforce_ov_cj1_content_limit(worker_binding_to_content(self))


@dataclass(frozen=True, slots=True)
class AssistedDiagnosisRequest:
    request_id: str
    workspace_id: str
    feedback_ref: str
    worker_binding: AssistedWorkerBinding
    authorised_context_refs: tuple[str, ...]
    deadline_ms: int
    cancellation_requested: bool = False

    def __post_init__(self) -> None:
        for name in ("request_id", "workspace_id", "feedback_ref"):
            _id(name, cast(str, getattr(self, name)))
        require(
            self.worker_binding.workspace_id == self.workspace_id,
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "worker binding and request workspace differ",
        )
        _refs("authorised_context_refs", self.authorised_context_refs)
        require(
            isinstance(self.deadline_ms, int)
            and not isinstance(self.deadline_ms, bool)
            and 0 < self.deadline_ms <= MAX_DEADLINE_MS,
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            f"deadline_ms must be in 1..{MAX_DEADLINE_MS}",
        )


@dataclass(frozen=True, slots=True)
class AssistedWorkerOutput:
    issues: tuple[FeedbackIssue, ...]
    diagnoses: tuple[FeedbackDiagnosis, ...]
    proposal: KnowledgeImprovementProposal | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_microunits: int = 0

    def __post_init__(self) -> None:
        require(
            isinstance(self.issues, tuple) and 0 < len(self.issues) <= MAX_ISSUES,
            GovernedKnowledgeErrorCode.SET_LIMIT_EXCEEDED,
            f"issues must contain 1..{MAX_ISSUES} entries",
        )
        require(
            isinstance(self.diagnoses, tuple) and bool(self.diagnoses),
            GovernedKnowledgeErrorCode.MISSING_FIELD,
            "diagnoses must be a non-empty tuple",
        )
        require(
            len(self.diagnoses) <= MAX_ISSUES,
            GovernedKnowledgeErrorCode.SET_LIMIT_EXCEEDED,
            f"diagnoses exceeds {MAX_ISSUES} entries",
        )
        workspaces = {item.workspace_id for item in self.issues} | {
            item.workspace_id for item in self.diagnoses
        }
        require(
            len(workspaces) == 1,
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "worker output crosses workspaces",
        )
        feedback_refs = {item.feedback_ref for item in self.issues} | {
            item.feedback_ref for item in self.diagnoses
        }
        require(
            len(feedback_refs) == 1,
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "worker output refers to different feedback records",
        )
        if self.proposal is not None:
            require(
                self.proposal.workspace_id in workspaces,
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "proposal and diagnosis workspace differ",
            )
            require(
                self.proposal.feedback_ref in feedback_refs,
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "proposal and diagnosis feedback differ",
            )
            require(
                self.proposal.is_pending,
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "assisted proposals must remain pending",
            )
        for name in ("input_tokens", "output_tokens", "cost_microunits"):
            value = cast(int, getattr(self, name))
            require(
                isinstance(value, int) and not isinstance(value, bool) and value >= 0,
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                f"{name} must be a non-negative integer",
            )
        enforce_ov_cj1_content_limit(assisted_worker_output_to_mapping(self))


@dataclass(frozen=True, slots=True)
class DiagnosisAttempt:
    attempt_id: str
    request_id: str
    workspace_id: str
    feedback_ref: str
    worker_binding_ref: str
    status: DiagnosisAttemptStatus
    issue_refs: tuple[str, ...] = ()
    diagnosis_refs: tuple[str, ...] = ()
    proposal_ref: str | None = None
    safe_error_code: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_microunits: int | None = None
    profile_version: str = field(init=False, default=DIAGNOSIS_ATTEMPT_PROFILE_VERSION)

    def __post_init__(self) -> None:
        for name in (
            "attempt_id",
            "request_id",
            "workspace_id",
            "feedback_ref",
            "worker_binding_ref",
        ):
            _id(name, cast(str, getattr(self, name)))
        require(
            isinstance(self.status, DiagnosisAttemptStatus),
            GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
            "diagnosis attempt status is unsupported",
        )
        _refs("issue_refs", self.issue_refs)
        _refs("diagnosis_refs", self.diagnosis_refs)
        success = self.status is DiagnosisAttemptStatus.SUCCEEDED
        require(
            success == bool(self.diagnosis_refs),
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "only a successful attempt may carry diagnosis refs",
        )
        require(
            success or (not self.issue_refs and self.proposal_ref is None),
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "failed attempts must not carry issues or a proposal",
        )
        if self.proposal_ref is not None:
            _id("proposal_ref", self.proposal_ref)
        require(
            success == (self.safe_error_code is None),
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "failed attempts require one safe error code",
        )
        for name in ("input_tokens", "output_tokens", "cost_microunits"):
            value = cast(int | None, getattr(self, name))
            require(
                value is None
                or (isinstance(value, int) and not isinstance(value, bool) and value >= 0),
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                f"{name} must be a non-negative integer or null",
            )
        enforce_ov_cj1_content_limit(diagnosis_attempt_to_content(self))


@dataclass(frozen=True, slots=True)
class AssistedDiagnosisOutcome:
    attempt: DiagnosisAttempt
    output: AssistedWorkerOutput | None


class AssistedDiagnosisWorker(Protocol):
    def __call__(self, request: AssistedDiagnosisRequest) -> Mapping[str, Any]: ...


class AssistedWorkerFailure(Exception):
    """Sanitised worker failure carrying only a stable safe reason code."""

    def __init__(self, safe_code: str = "worker_failed") -> None:
        self.safe_code = safe_code
        super().__init__(safe_code)


class AssistedWorkerTimedOut(AssistedWorkerFailure):
    def __init__(self) -> None:
        super().__init__("worker_timed_out")


class AssistedWorkerCancelled(AssistedWorkerFailure):
    def __init__(self) -> None:
        super().__init__("worker_cancelled")


def _walk_forbidden(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalised_key = (
                key.lower().replace("-", "_") if isinstance(key, str) else ""
            )
            require(
                isinstance(key, str) and normalised_key not in _FORBIDDEN_WORKER_KEYS,
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "worker output contains an authority or effect-bearing field",
            )
            _walk_forbidden(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _walk_forbidden(nested)


def feedback_issue_to_content(value: FeedbackIssue) -> dict[str, Any]:
    return {
        "profile_version": value.profile_version,
        "issue_id": value.issue_id,
        "workspace_id": value.workspace_id,
        "feedback_ref": value.feedback_ref,
        "affected_claim": value.affected_claim,
        "proposed_correction": value.proposed_correction,
        "feedback_spans": [encode_span(item) for item in value.feedback_spans],
        "supporting_evidence_refs": list(value.supporting_evidence_refs),
        "task_context_refs": list(value.task_context_refs),
        "source_lineage_refs": list(value.source_lineage_refs),
    }


def feedback_issue_from_content(content: object) -> FeedbackIssue:
    require(isinstance(content, Mapping), GovernedKnowledgeErrorCode.INVALID_FIELD, "issue must be a mapping")
    value = cast(Mapping[str, Any], content)
    expected = {
        "profile_version", "issue_id", "workspace_id", "feedback_ref",
        "affected_claim", "proposed_correction", "feedback_spans",
        "supporting_evidence_refs", "task_context_refs", "source_lineage_refs",
    }
    require(set(value) == expected, GovernedKnowledgeErrorCode.INVALID_FIELD, "issue fields are not exact")
    require(value["profile_version"] == FEEDBACK_ISSUE_PROFILE_VERSION, GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE, "issue profile_version is unsupported")
    spans = value["feedback_spans"]
    require(isinstance(spans, list), GovernedKnowledgeErrorCode.INVALID_FIELD, "feedback_spans must be a list")
    def strings(name: str) -> tuple[str, ...]:
        raw = value[name]
        require(isinstance(raw, list) and all(isinstance(item, str) for item in raw), GovernedKnowledgeErrorCode.INVALID_FIELD, f"{name} must be a string list")
        return tuple(cast(list[str], raw))
    return FeedbackIssue(
        issue_id=cast(str, value["issue_id"]),
        workspace_id=cast(str, value["workspace_id"]),
        feedback_ref=cast(str, value["feedback_ref"]),
        affected_claim=cast(str, value["affected_claim"]),
        proposed_correction=cast(str, value["proposed_correction"]),
        feedback_spans=tuple(decode_span(item) for item in spans),
        supporting_evidence_refs=strings("supporting_evidence_refs"),
        task_context_refs=strings("task_context_refs"),
        source_lineage_refs=strings("source_lineage_refs"),
    )


def worker_binding_to_content(value: AssistedWorkerBinding) -> dict[str, Any]:
    return {
        "profile_version": value.profile_version,
        "binding_id": value.binding_id,
        "workspace_id": value.workspace_id,
        "source_id": value.source_id,
        "executor_id": value.executor_id,
        "executor_version": value.executor_version,
        "executor_build_hash": value.executor_build_hash,
        "executor_content_hash": value.executor_content_hash,
        "runtime_profile_ref": value.runtime_profile_ref,
        "policy_ref": value.policy_ref,
        "required_capabilities": list(value.required_capabilities),
        "minimum_isolation": value.minimum_isolation,
        "resolved_isolation": value.resolved_isolation,
        "run_ref": value.run_ref,
        "step_ref": value.step_ref,
        "attempt_ref": value.attempt_ref,
        "provider_ref": value.provider_ref,
        "model_ref": value.model_ref,
        "model_identity_kind": value.model_identity_kind.value,
        "observed_at_ref": value.observed_at_ref,
    }


def diagnosis_attempt_to_content(value: DiagnosisAttempt) -> dict[str, Any]:
    return {
        "profile_version": value.profile_version,
        "attempt_id": value.attempt_id,
        "request_id": value.request_id,
        "workspace_id": value.workspace_id,
        "feedback_ref": value.feedback_ref,
        "worker_binding_ref": value.worker_binding_ref,
        "status": value.status.value,
        "issue_refs": list(value.issue_refs),
        "diagnosis_refs": list(value.diagnosis_refs),
        "proposal_ref": value.proposal_ref,
        "safe_error_code": value.safe_error_code,
        "input_tokens": value.input_tokens,
        "output_tokens": value.output_tokens,
        "cost_microunits": value.cost_microunits,
    }


def assisted_worker_output_from_mapping(value: Mapping[str, Any]) -> AssistedWorkerOutput:
    _walk_forbidden(value)
    require(
        set(value)
        == {
            "issues",
            "diagnoses",
            "proposal",
            "input_tokens",
            "output_tokens",
            "cost_microunits",
        },
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        "worker output fields are not exact",
    )
    issues_raw = value["issues"]
    diagnoses_raw = value["diagnoses"]
    require(isinstance(issues_raw, list), GovernedKnowledgeErrorCode.INVALID_FIELD, "issues must be a list")
    require(isinstance(diagnoses_raw, list), GovernedKnowledgeErrorCode.INVALID_FIELD, "diagnoses must be a list")
    diagnosis_fields = {
        "profile_version", "diagnosis_id", "workspace_id", "feedback_ref",
        "analyst_ref", "recorded_at", "categories",
        "original_context_reconstructed", "proposed_owner", "evidence_for",
        "evidence_against", "coverage_note", "confidence",
    }
    proposal_fields = {
        "profile_version", "proposal_id", "workspace_id", "transition_kind",
        "base_version_refs", "risk_policy_ref", "required_reviewer_refs",
        "feedback_ref", "diagnosis_ref", "supporting_evidence_refs",
        "contradicting_evidence_refs", "applicability_changes",
        "temporal_changes_note", "expected_consumer_impact_refs",
        "required_evaluation_case_refs", "disposition",
    }
    for raw in diagnoses_raw:
        require(
            isinstance(raw, Mapping) and set(raw) == diagnosis_fields,
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "diagnosis fields are not exact",
        )
    proposal_raw = value["proposal"]
    if proposal_raw is not None:
        require(
            isinstance(proposal_raw, Mapping) and set(proposal_raw) == proposal_fields,
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "proposal fields are not exact",
        )
    usage: dict[str, int] = {}
    for name in ("input_tokens", "output_tokens", "cost_microunits"):
        raw = value[name]
        require(
            isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0,
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            f"{name} must be a non-negative integer",
        )
        usage[name] = raw
    return AssistedWorkerOutput(
        issues=tuple(feedback_issue_from_content(item) for item in issues_raw),
        diagnoses=tuple(diagnosis_from_content(item) for item in diagnoses_raw),
        proposal=None if proposal_raw is None else proposal_from_content(proposal_raw),
        **usage,
    )


def assisted_worker_output_to_mapping(value: AssistedWorkerOutput) -> dict[str, Any]:
    return {
        "issues": [feedback_issue_to_content(item) for item in value.issues],
        "diagnoses": [diagnosis_to_content(item) for item in value.diagnoses],
        "proposal": None if value.proposal is None else proposal_to_content(value.proposal),
        "input_tokens": value.input_tokens,
        "output_tokens": value.output_tokens,
        "cost_microunits": value.cost_microunits,
    }


def _failed_attempt(
    request: AssistedDiagnosisRequest,
    status: DiagnosisAttemptStatus,
    safe_code: str,
) -> AssistedDiagnosisOutcome:
    return AssistedDiagnosisOutcome(
        attempt=DiagnosisAttempt(
            attempt_id=request.worker_binding.attempt_ref,
            request_id=request.request_id,
            workspace_id=request.workspace_id,
            feedback_ref=request.feedback_ref,
            worker_binding_ref=request.worker_binding.binding_id,
            status=status,
            safe_error_code=safe_code,
        ),
        output=None,
    )


def run_assisted_diagnosis(
    request: AssistedDiagnosisRequest,
    feedback: ExpertFeedback,
    worker: AssistedDiagnosisWorker,
) -> AssistedDiagnosisOutcome:
    """Run one injected worker and convert every failure to a safe inert result."""
    require(
        feedback.workspace_id == request.workspace_id
        and feedback.feedback_id == request.feedback_ref,
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        "feedback and diagnosis request binding differ",
    )
    require(
        DIAGNOSIS_CAPABILITY in request.worker_binding.required_capabilities,
        GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
        "worker binding does not declare structured diagnosis",
    )
    if request.cancellation_requested:
        return _failed_attempt(request, DiagnosisAttemptStatus.CANCELLED, "worker_cancelled")
    try:
        raw = worker(request)
        require(isinstance(raw, Mapping), GovernedKnowledgeErrorCode.INVALID_FIELD, "worker output must be a mapping")
        output = assisted_worker_output_from_mapping(raw)
        require(
            all(item.workspace_id == request.workspace_id and item.feedback_ref == request.feedback_ref for item in output.issues),
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "worker issues do not match the request",
        )
        require(
            all(item.workspace_id == request.workspace_id and item.feedback_ref == request.feedback_ref for item in output.diagnoses),
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "worker diagnoses do not match the request",
        )
    except AssistedWorkerCancelled:
        return _failed_attempt(request, DiagnosisAttemptStatus.CANCELLED, "worker_cancelled")
    except AssistedWorkerTimedOut:
        return _failed_attempt(request, DiagnosisAttemptStatus.TIMED_OUT, "worker_timed_out")
    except AssistedWorkerFailure as error:
        return _failed_attempt(request, DiagnosisAttemptStatus.FAILED, error.safe_code)
    except (GovernedKnowledgeValidationError, TypeError, ValueError):
        return _failed_attempt(request, DiagnosisAttemptStatus.INVALID_OUTPUT, "invalid_worker_output")
    except Exception:  # noqa: BLE001 -- provider/runtime details must not cross this boundary.
        return _failed_attempt(request, DiagnosisAttemptStatus.FAILED, "worker_failed")
    attempt = DiagnosisAttempt(
        attempt_id=request.worker_binding.attempt_ref,
        request_id=request.request_id,
        workspace_id=request.workspace_id,
        feedback_ref=request.feedback_ref,
        worker_binding_ref=request.worker_binding.binding_id,
        status=DiagnosisAttemptStatus.SUCCEEDED,
        issue_refs=tuple(item.issue_id for item in output.issues),
        diagnosis_refs=tuple(item.diagnosis_id for item in output.diagnoses),
        proposal_ref=None if output.proposal is None else output.proposal.proposal_id,
        input_tokens=output.input_tokens,
        output_tokens=output.output_tokens,
        cost_microunits=output.cost_microunits,
    )
    return AssistedDiagnosisOutcome(attempt=attempt, output=output)


__all__ = [
    "DIAGNOSIS_ATTEMPT_PROFILE_VERSION",
    "DIAGNOSIS_CAPABILITY",
    "EVALUATION_CAPABILITY",
    "FEEDBACK_ISSUE_PROFILE_VERSION",
    "MAX_DEADLINE_MS",
    "MAX_ISSUES",
    "STAGE2_WORKER_CAPABILITIES",
    "WORKER_BINDING_PROFILE_VERSION",
    "AssistedDiagnosisOutcome",
    "AssistedDiagnosisRequest",
    "AssistedDiagnosisWorker",
    "AssistedWorkerBinding",
    "AssistedWorkerCancelled",
    "AssistedWorkerFailure",
    "AssistedWorkerOutput",
    "AssistedWorkerTimedOut",
    "DiagnosisAttempt",
    "DiagnosisAttemptStatus",
    "FeedbackIssue",
    "ModelIdentityKind",
    "assisted_worker_output_from_mapping",
    "assisted_worker_output_to_mapping",
    "diagnosis_attempt_to_content",
    "feedback_issue_from_content",
    "feedback_issue_to_content",
    "run_assisted_diagnosis",
    "worker_binding_to_content",
]
