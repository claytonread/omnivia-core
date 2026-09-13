"""KI-02/KI-03: `TaskContextProfile` and `ContextSelectionManifest` (spec 7.1, 8.2).

Both are pure representations/building blocks in this lane (spec 5.1): neither
type runs retrieval, assembles a Context Pack, or performs any I/O. A
`TaskContextProfile` is configuration over an existing Context Pack build
mechanism -- it carries no credential or permission grant, and provider/model
selection belongs to the consumer's own execution configuration, never to this
profile (spec 7.1); that is a structural guarantee (no such field exists here),
not a runtime check. A `ContextSelectionManifest` is Core's own record of what
it selected under one request/checkpoint set (spec 8.1) -- it cannot establish
what an external host ultimately sent to a model, which is the separate,
consumer-issued `ContextDeliveryReceipt` (`governed_knowledge.delivery`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from omnivia_core.governed_knowledge.applicability import ApplicabilityResult
from omnivia_core.governed_knowledge.errors import (
    GovernedKnowledgeErrorCode,
    require,
)
from omnivia_core.semantic_registry.consumers import ConsumerKind
from omnivia_core.semantic_registry.temporal import TemporalInstant

TASK_CONTEXT_PROFILE_VERSION = "governed-knowledge-task-context-v1"
CONTEXT_SELECTION_MANIFEST_VERSION = "governed-knowledge-selection-manifest-v1"


def _require_id(field_name: str, value: str) -> None:
    require(
        isinstance(value, str) and value.strip() != "",
        GovernedKnowledgeErrorCode.MISSING_FIELD,
        f"{field_name} is required",
    )


def _require_str_tuple(field_name: str, value: tuple[str, ...]) -> None:
    require(
        isinstance(value, tuple),
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        f"{field_name} must be a tuple",
    )
    for index, item in enumerate(value):
        require(
            isinstance(item, str) and item.strip() != "",
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            f"{field_name}[{index}] must be a non-empty string",
        )


class ContextFreshnessPolicy(str, Enum):
    """Whether a profile blocks or warns on review-overdue material (spec 6.5, 7.6).

    A profile declares one of these explicitly; nothing here infers it from a
    background scan.
    """

    STRICT = "strict"
    PERMISSIVE_WITH_WARNING = "permissive_with_warning"


class SelectionAuthorityClass(str, Enum):
    ADMITTED_KNOWLEDGE = "admitted_knowledge"
    SUPPORTING_EVIDENCE = "supporting_evidence"
    PENDING_ASSERTION = "pending_assertion"
    HISTORICAL_KNOWLEDGE = "historical_knowledge"
    AUTHENTICATED_INSTRUCTION = "authenticated_instruction"
    GENERATED_INTERPRETATION = "generated_interpretation"


@dataclass(frozen=True, slots=True)
class SelectedContextItem:
    item_version_ref: str
    authority_class: SelectionAuthorityClass
    selection_reason: str
    source_span_refs: tuple[str, ...] = ()
    applicability_result_ref: str | None = None

    def __post_init__(self) -> None:
        _require_id("item_version_ref", self.item_version_ref)
        require(
            isinstance(self.authority_class, SelectionAuthorityClass),
            GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
            "authority_class must be a SelectionAuthorityClass",
        )
        _require_id("selection_reason", self.selection_reason)
        _require_str_tuple("source_span_refs", self.source_span_refs)
        if self.applicability_result_ref is not None:
            _require_id("applicability_result_ref", self.applicability_result_ref)


@dataclass(frozen=True, slots=True)
class ManifestOmission:
    reason_code: str
    safe_reason: str
    item_version_ref: str | None = None

    def __post_init__(self) -> None:
        _require_id("reason_code", self.reason_code)
        _require_id("safe_reason", self.safe_reason)
        if self.item_version_ref is not None:
            _require_id("item_version_ref", self.item_version_ref)


@dataclass(frozen=True, slots=True)
class TaskContextProfile:
    """Configuration over an existing Context Pack purpose (spec 7.1)."""

    profile_id: str
    version_ref: str
    purpose: str
    workspace_id: str
    allowed_consumer_kinds: tuple[ConsumerKind, ...] = ()
    required_scope_refs: tuple[str, ...] = ()
    required_fact_refs: tuple[str, ...] = ()
    knowledge_domain_refs: tuple[str, ...] = ()
    selection_stage_refs: tuple[str, ...] = ()
    freshness_policy: ContextFreshnessPolicy = ContextFreshnessPolicy.STRICT
    max_bytes: int = 0
    max_items: int | None = None
    inclusion_priority_refs: tuple[str, ...] = ()
    response_shape: str = ""
    profile_version: str = field(init=False, default=TASK_CONTEXT_PROFILE_VERSION)

    def __post_init__(self) -> None:
        _require_id("profile_id", self.profile_id)
        _require_id("version_ref", self.version_ref)
        _require_id("purpose", self.purpose)
        _require_id("workspace_id", self.workspace_id)
        _require_id("response_shape", self.response_shape)
        for index, kind in enumerate(self.allowed_consumer_kinds):
            require(
                isinstance(kind, ConsumerKind),
                GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
                f"allowed_consumer_kinds[{index}] must be a ConsumerKind",
            )
        for name in (
            "required_scope_refs",
            "required_fact_refs",
            "knowledge_domain_refs",
            "selection_stage_refs",
            "inclusion_priority_refs",
        ):
            _require_str_tuple(name, getattr(self, name))
        require(
            isinstance(self.freshness_policy, ContextFreshnessPolicy),
            GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
            "freshness_policy must be a ContextFreshnessPolicy",
        )
        require(
            isinstance(self.max_bytes, int)
            and not isinstance(self.max_bytes, bool)
            and self.max_bytes > 0,
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "max_bytes must be a positive integer",
        )
        if self.max_items is not None:
            require(
                isinstance(self.max_items, int)
                and not isinstance(self.max_items, bool)
                and self.max_items > 0,
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "max_items must be a positive integer",
            )


@dataclass(frozen=True, slots=True)
class ContextSelectionManifest:
    """Core's own record of what it selected under one request (spec 8.2).

    Binds a permission-checked digest/identity per group of spec 8.2's table.
    `applicability_results` reuses `governed_knowledge.applicability`'s own
    `ApplicabilityResult` unchanged, so a manifest's applicable-position
    evidence carries exactly the same authorized-fact-visibility guarantees the
    evaluator itself provides -- never a second, looser projection of it.
    """

    manifest_id: str
    profile_version_ref: str
    workspace_id: str
    purpose: str
    consumer_ref: str
    request_ref: str
    semantic_model_version_ref: str
    valid_at: TemporalInstant
    recorded_as_of: TemporalInstant
    query_time: TemporalInstant
    consumer_deployment_ref: str = "unspecified-deployment"
    checkpoint_refs: tuple[str, ...] = ()
    selected_item_refs: tuple[str, ...] = ()
    selected_items: tuple[SelectedContextItem, ...] = ()
    applicability_results: tuple[ApplicabilityResult, ...] = ()
    selection_reasons: tuple[str, ...] = ()
    transformation_notes: tuple[str, ...] = ()
    coverage_note: str = ""
    partial_coverage: bool = False
    retrieval_mechanisms: tuple[str, ...] = ()
    omissions: tuple[ManifestOmission, ...] = ()
    effective_byte_bound: int = 0
    measured_bytes: int | None = None
    byte_count_is_estimated: bool = False
    tokenizer_version: str | None = None
    policy_version: str = ""
    freshness_policy: ContextFreshnessPolicy = ContextFreshnessPolicy.STRICT
    review_overdue_item_refs: tuple[str, ...] = ()
    recheck_conditions: tuple[str, ...] = ()
    issuer_class: str = "core_issued"
    integrity_algorithm: str = "sha-256/ov-cj-1"
    pack_identity: str | None = None
    pack_digest: str | None = None
    integrity_digest: str | None = None
    manifest_version: str = field(
        init=False, default=CONTEXT_SELECTION_MANIFEST_VERSION
    )

    def __post_init__(self) -> None:
        _require_id("manifest_id", self.manifest_id)
        _require_id("profile_version_ref", self.profile_version_ref)
        _require_id("workspace_id", self.workspace_id)
        _require_id("purpose", self.purpose)
        _require_id("consumer_ref", self.consumer_ref)
        _require_id("consumer_deployment_ref", self.consumer_deployment_ref)
        _require_id("request_ref", self.request_ref)
        _require_id("semantic_model_version_ref", self.semantic_model_version_ref)
        _require_id("policy_version", self.policy_version)
        for name, value in (
            ("valid_at", self.valid_at),
            ("recorded_as_of", self.recorded_as_of),
            ("query_time", self.query_time),
        ):
            require(
                isinstance(value, TemporalInstant),
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                f"{name} must be a TemporalInstant",
            )
        for name in (
            "checkpoint_refs",
            "selected_item_refs",
            "selection_reasons",
            "transformation_notes",
            "review_overdue_item_refs",
            "retrieval_mechanisms",
            "recheck_conditions",
        ):
            _require_str_tuple(name, getattr(self, name))
        for index, item in enumerate(self.selected_items):
            require(
                isinstance(item, SelectedContextItem),
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                f"selected_items[{index}] must be a SelectedContextItem",
            )
        for index, omission in enumerate(self.omissions):
            require(
                isinstance(omission, ManifestOmission),
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                f"omissions[{index}] must be a ManifestOmission",
            )
        for index, result in enumerate(self.applicability_results):
            require(
                isinstance(result, ApplicabilityResult),
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                f"applicability_results[{index}] must be an ApplicabilityResult",
            )
        require(
            isinstance(self.freshness_policy, ContextFreshnessPolicy),
            GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
            "freshness_policy must be a ContextFreshnessPolicy",
        )
        require(
            isinstance(self.effective_byte_bound, int)
            and not isinstance(self.effective_byte_bound, bool)
            and self.effective_byte_bound > 0,
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "effective_byte_bound must be a positive integer",
        )
        if self.measured_bytes is not None:
            require(
                isinstance(self.measured_bytes, int)
                and not isinstance(self.measured_bytes, bool)
                and self.measured_bytes >= 0,
                GovernedKnowledgeErrorCode.INVALID_FIELD,
                "measured_bytes must be a non-negative integer",
            )
        require(
            self.issuer_class == "core_issued",
            GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
            "selection manifest issuer_class must be core_issued",
        )
        _require_id("integrity_algorithm", self.integrity_algorithm)


__all__ = [
    "CONTEXT_SELECTION_MANIFEST_VERSION",
    "TASK_CONTEXT_PROFILE_VERSION",
    "ContextFreshnessPolicy",
    "ContextSelectionManifest",
    "ManifestOmission",
    "SelectedContextItem",
    "SelectionAuthorityClass",
    "TaskContextProfile",
]
