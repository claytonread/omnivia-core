"""Strict JSON content codecs for Stage 1 governed-knowledge profiles."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, cast

from omnivia_core.contracts.v1.canonical_json import canonicalize
from omnivia_core.governed_knowledge.content_limits import enforce_ov_cj1_content_limit
from omnivia_core.governed_knowledge.context import (
    CONTEXT_SELECTION_MANIFEST_VERSION,
    TASK_CONTEXT_PROFILE_VERSION,
    ContextFreshnessPolicy,
    ContextSelectionManifest,
    ManifestOmission,
    SelectedContextItem,
    SelectionAuthorityClass,
    TaskContextProfile,
)
from omnivia_core.governed_knowledge.delivery import (
    DELIVERY_RECEIPT_VERSION,
    ContextDeliveryReceipt,
    DeliveryEvent,
    DeliverySegment,
    DeliveryTransportState,
    ReceiptIssuerClass,
    SegmentProvenanceClass,
)
from omnivia_core.governed_knowledge.dependency import (
    KNOWLEDGE_DEPENDENCY_PROFILE_VERSION,
    KnowledgeConsumerDependency,
    KnowledgeDependencyClass,
)
from omnivia_core.governed_knowledge.errors import GovernedKnowledgeErrorCode, require
from omnivia_core.governed_knowledge.feedback import (
    DIAGNOSIS_PROFILE_VERSION,
    FEEDBACK_PROFILE_VERSION,
    PROPOSAL_PROFILE_VERSION,
    DiagnosisCategory,
    ExpertFeedback,
    ExpertiseClaim,
    ExpertiseSource,
    FeedbackContextCoverage,
    FeedbackDiagnosis,
    FeedbackDisposition,
    KnowledgeImprovementProposal,
    ProposalDisposition,
    ProposalTransitionKind,
)
from omnivia_core.governed_knowledge.wire import (
    decode_applicability_result,
    decode_instant,
    decode_opt_expression,
    decode_span,
    encode_applicability_result,
    encode_instant,
    encode_opt_expression,
    encode_span,
    require_enum,
    require_mapping,
    require_opt_str,
    require_str,
    require_str_list,
)
from omnivia_core.semantic_registry.consumers import (
    Consumer,
    ConsumerDependency,
    ConsumerKind,
    DependencyMode,
    ExactBinding,
)
from omnivia_core.semantic_registry.evidence import Classification


def _profile(content: object, expected: str) -> Mapping[str, Any]:
    value = require_mapping(content, "profile content")
    require(
        require_str(value.get("profile_version"), "profile_version") == expected,
        GovernedKnowledgeErrorCode.UNSUPPORTED_VALUE,
        "profile_version is unsupported",
    )
    return value


def _bool(value: object, name: str) -> bool:
    require(
        isinstance(value, bool),
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        f"{name} must be a boolean",
    )
    return cast(bool, value)


def _int(value: object, name: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    require(
        isinstance(value, int) and not isinstance(value, bool),
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        f"{name} must be an integer",
    )
    return cast(int, value)


def _required_int(value: object, name: str) -> int:
    parsed = _int(value, name)
    assert parsed is not None
    return parsed


def task_context_profile_to_content(value: TaskContextProfile) -> dict[str, Any]:
    return {
        "profile_version": value.profile_version,
        "profile_id": value.profile_id,
        "version_ref": value.version_ref,
        "purpose": value.purpose,
        "workspace_id": value.workspace_id,
        "allowed_consumer_kinds": [item.value for item in value.allowed_consumer_kinds],
        "required_scope_refs": list(value.required_scope_refs),
        "required_fact_refs": list(value.required_fact_refs),
        "knowledge_domain_refs": list(value.knowledge_domain_refs),
        "selection_stage_refs": list(value.selection_stage_refs),
        "freshness_policy": value.freshness_policy.value,
        "max_bytes": value.max_bytes,
        "max_items": value.max_items,
        "inclusion_priority_refs": list(value.inclusion_priority_refs),
        "response_shape": value.response_shape,
    }


def task_context_profile_from_content(content: object) -> TaskContextProfile:
    value = _profile(content, TASK_CONTEXT_PROFILE_VERSION)
    kinds = require_str_list(
        value.get("allowed_consumer_kinds", []), "allowed_consumer_kinds"
    )
    return TaskContextProfile(
        profile_id=require_str(value.get("profile_id"), "profile_id"),
        version_ref=require_str(value.get("version_ref"), "version_ref"),
        purpose=require_str(value.get("purpose"), "purpose"),
        workspace_id=require_str(value.get("workspace_id"), "workspace_id"),
        allowed_consumer_kinds=tuple(
            require_enum(item, "allowed_consumer_kind", ConsumerKind) for item in kinds
        ),
        required_scope_refs=tuple(
            require_str_list(
                value.get("required_scope_refs", []), "required_scope_refs"
            )
        ),
        required_fact_refs=tuple(
            require_str_list(value.get("required_fact_refs", []), "required_fact_refs")
        ),
        knowledge_domain_refs=tuple(
            require_str_list(
                value.get("knowledge_domain_refs", []), "knowledge_domain_refs"
            )
        ),
        selection_stage_refs=tuple(
            require_str_list(
                value.get("selection_stage_refs", []), "selection_stage_refs"
            )
        ),
        freshness_policy=require_enum(
            value.get("freshness_policy"), "freshness_policy", ContextFreshnessPolicy
        ),
        max_bytes=_required_int(value.get("max_bytes"), "max_bytes"),
        max_items=_int(value.get("max_items"), "max_items", optional=True),
        inclusion_priority_refs=tuple(
            require_str_list(
                value.get("inclusion_priority_refs", []), "inclusion_priority_refs"
            )
        ),
        response_shape=require_str(value.get("response_shape"), "response_shape"),
    )


def selection_manifest_to_content(value: ContextSelectionManifest) -> dict[str, Any]:
    return {
        "profile_version": value.manifest_version,
        "manifest_id": value.manifest_id,
        "profile_version_ref": value.profile_version_ref,
        "workspace_id": value.workspace_id,
        "purpose": value.purpose,
        "consumer_ref": value.consumer_ref,
        "consumer_deployment_ref": value.consumer_deployment_ref,
        "request_ref": value.request_ref,
        "semantic_model_version_ref": value.semantic_model_version_ref,
        "valid_at": encode_instant(value.valid_at),
        "recorded_as_of": encode_instant(value.recorded_as_of),
        "query_time": encode_instant(value.query_time),
        "checkpoint_refs": list(value.checkpoint_refs),
        "selected_item_refs": list(value.selected_item_refs),
        "selected_items": [
            {
                "item_version_ref": item.item_version_ref,
                "authority_class": item.authority_class.value,
                "selection_reason": item.selection_reason,
                "source_span_refs": list(item.source_span_refs),
                "applicability_result_ref": item.applicability_result_ref,
            }
            for item in value.selected_items
        ],
        "applicability_results": [
            encode_applicability_result(item) for item in value.applicability_results
        ],
        "selection_reasons": list(value.selection_reasons),
        "transformation_notes": list(value.transformation_notes),
        "coverage_note": value.coverage_note,
        "partial_coverage": value.partial_coverage,
        "retrieval_mechanisms": list(value.retrieval_mechanisms),
        "omissions": [
            {
                "reason_code": item.reason_code,
                "safe_reason": item.safe_reason,
                "item_version_ref": item.item_version_ref,
            }
            for item in value.omissions
        ],
        "effective_byte_bound": value.effective_byte_bound,
        "measured_bytes": value.measured_bytes,
        "byte_count_is_estimated": value.byte_count_is_estimated,
        "tokenizer_version": value.tokenizer_version,
        "policy_version": value.policy_version,
        "freshness_policy": value.freshness_policy.value,
        "review_overdue_item_refs": list(value.review_overdue_item_refs),
        "recheck_conditions": list(value.recheck_conditions),
        "issuer_class": value.issuer_class,
        "integrity_algorithm": value.integrity_algorithm,
        "pack_identity": value.pack_identity,
        "pack_digest": value.pack_digest,
        "integrity_digest": value.integrity_digest,
    }


def selection_manifest_from_content(content: object) -> ContextSelectionManifest:
    value = _profile(content, CONTEXT_SELECTION_MANIFEST_VERSION)
    items_raw = value.get("selected_items", [])
    omissions_raw = value.get("omissions", [])
    results_raw = value.get("applicability_results", [])
    for name, raw in (
        ("selected_items", items_raw),
        ("omissions", omissions_raw),
        ("applicability_results", results_raw),
    ):
        require(
            isinstance(raw, list),
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            f"{name} must be a list",
        )
    items = []
    for raw in items_raw:
        item = require_mapping(raw, "selected_item")
        items.append(
            SelectedContextItem(
                item_version_ref=require_str(
                    item.get("item_version_ref"), "item_version_ref"
                ),
                authority_class=require_enum(
                    item.get("authority_class"),
                    "authority_class",
                    SelectionAuthorityClass,
                ),
                selection_reason=require_str(
                    item.get("selection_reason"), "selection_reason"
                ),
                source_span_refs=tuple(
                    require_str_list(
                        item.get("source_span_refs", []), "source_span_refs"
                    )
                ),
                applicability_result_ref=require_opt_str(
                    item.get("applicability_result_ref"), "applicability_result_ref"
                ),
            )
        )
    omissions = []
    for raw in omissions_raw:
        item = require_mapping(raw, "omission")
        omissions.append(
            ManifestOmission(
                reason_code=require_str(item.get("reason_code"), "reason_code"),
                safe_reason=require_str(item.get("safe_reason"), "safe_reason"),
                item_version_ref=require_opt_str(
                    item.get("item_version_ref"), "item_version_ref"
                ),
            )
        )
    measured = _int(value.get("measured_bytes"), "measured_bytes", optional=True)
    return ContextSelectionManifest(
        manifest_id=require_str(value.get("manifest_id"), "manifest_id"),
        profile_version_ref=require_str(
            value.get("profile_version_ref"), "profile_version_ref"
        ),
        workspace_id=require_str(value.get("workspace_id"), "workspace_id"),
        purpose=require_str(value.get("purpose"), "purpose"),
        consumer_ref=require_str(value.get("consumer_ref"), "consumer_ref"),
        consumer_deployment_ref=require_str(
            value.get("consumer_deployment_ref"), "consumer_deployment_ref"
        ),
        request_ref=require_str(value.get("request_ref"), "request_ref"),
        semantic_model_version_ref=require_str(
            value.get("semantic_model_version_ref"), "semantic_model_version_ref"
        ),
        valid_at=decode_instant(value.get("valid_at")),
        recorded_as_of=decode_instant(value.get("recorded_as_of")),
        query_time=decode_instant(value.get("query_time")),
        checkpoint_refs=tuple(
            require_str_list(value.get("checkpoint_refs", []), "checkpoint_refs")
        ),
        selected_item_refs=tuple(
            require_str_list(value.get("selected_item_refs", []), "selected_item_refs")
        ),
        selected_items=tuple(items),
        applicability_results=tuple(
            decode_applicability_result(item) for item in results_raw
        ),
        selection_reasons=tuple(
            require_str_list(value.get("selection_reasons", []), "selection_reasons")
        ),
        transformation_notes=tuple(
            require_str_list(
                value.get("transformation_notes", []), "transformation_notes"
            )
        ),
        coverage_note=require_str(value.get("coverage_note", ""), "coverage_note"),
        partial_coverage=_bool(value.get("partial_coverage"), "partial_coverage"),
        retrieval_mechanisms=tuple(
            require_str_list(
                value.get("retrieval_mechanisms", []), "retrieval_mechanisms"
            )
        ),
        omissions=tuple(omissions),
        effective_byte_bound=_required_int(
            value.get("effective_byte_bound"), "effective_byte_bound"
        ),
        measured_bytes=measured,
        byte_count_is_estimated=_bool(
            value.get("byte_count_is_estimated"), "byte_count_is_estimated"
        ),
        tokenizer_version=require_opt_str(
            value.get("tokenizer_version"), "tokenizer_version"
        ),
        policy_version=require_str(value.get("policy_version"), "policy_version"),
        freshness_policy=require_enum(
            value.get("freshness_policy"), "freshness_policy", ContextFreshnessPolicy
        ),
        review_overdue_item_refs=tuple(
            require_str_list(
                value.get("review_overdue_item_refs", []), "review_overdue_item_refs"
            )
        ),
        recheck_conditions=tuple(
            require_str_list(value.get("recheck_conditions", []), "recheck_conditions")
        ),
        issuer_class=require_str(value.get("issuer_class"), "issuer_class"),
        integrity_algorithm=require_str(
            value.get("integrity_algorithm"), "integrity_algorithm"
        ),
        pack_identity=require_opt_str(value.get("pack_identity"), "pack_identity"),
        pack_digest=require_opt_str(value.get("pack_digest"), "pack_digest"),
        integrity_digest=require_opt_str(
            value.get("integrity_digest"), "integrity_digest"
        ),
    )


def compute_selection_manifest_digest(value: ContextSelectionManifest) -> str:
    """Digest a manifest without its self-referential identity fields."""
    content = selection_manifest_to_content(value)
    content.pop("manifest_id")
    content.pop("integrity_digest")
    canonical = canonicalize(content).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def verify_selection_manifest_digest(value: ContextSelectionManifest) -> bool:
    """Return whether the manifest carries its exact OV-CJ-1 integrity digest."""
    return value.integrity_digest == compute_selection_manifest_digest(value)


def delivery_receipt_to_content(value: ContextDeliveryReceipt) -> dict[str, Any]:
    return {
        "profile_version": value.receipt_version,
        "receipt_id": value.receipt_id,
        "workspace_id": value.workspace_id,
        "consumer_ref": value.consumer_ref,
        "manifest_ref": value.manifest_ref,
        "events": [
            {
                "state": item.state.value,
                "observed_at": encode_instant(item.observed_at),
                "detail": item.detail,
            }
            for item in value.events
        ],
        "issuer_class": value.issuer_class.value,
        "run_ref": value.run_ref,
        "step_ref": value.step_ref,
        "attempt_ref": value.attempt_ref,
        "pack_ref": value.pack_ref,
        "supplied_segments": [
            {
                "segment_ref": item.segment_ref,
                "representation_ref": item.representation_ref,
                "provenance_class": item.provenance_class.value,
                "original_selected_item_ref": item.original_selected_item_ref,
                "transformation_refs": list(item.transformation_refs),
            }
            for item in value.supplied_segments
        ],
        "additional_source_refs": list(value.additional_source_refs),
        "transformations_after_core": list(value.transformations_after_core),
        "instruction_version_refs": list(value.instruction_version_refs),
        "skill_workflow_version_refs": list(value.skill_workflow_version_refs),
        "tool_result_refs": list(value.tool_result_refs),
        "provider_ref": value.provider_ref,
        "model_ref": value.model_ref,
        "effective_configuration_refs": list(value.effective_configuration_refs),
        "instrumentation_complete": value.instrumentation_complete,
        "result_ref": value.result_ref,
    }


def delivery_receipt_from_content(content: object) -> ContextDeliveryReceipt:
    value = _profile(content, DELIVERY_RECEIPT_VERSION)
    events_raw = value.get("events")
    segments_raw = value.get("supplied_segments", [])
    require(
        isinstance(events_raw, list),
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        "events must be a list",
    )
    require(
        isinstance(segments_raw, list),
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        "supplied_segments must be a list",
    )
    events = []
    for raw in cast(list[Any], events_raw):
        item = require_mapping(raw, "delivery event")
        events.append(
            DeliveryEvent(
                state=require_enum(item.get("state"), "state", DeliveryTransportState),
                observed_at=decode_instant(item.get("observed_at")),
                detail=require_opt_str(item.get("detail"), "detail"),
            )
        )
    segments = []
    for raw in segments_raw:
        item = require_mapping(raw, "delivery segment")
        segments.append(
            DeliverySegment(
                segment_ref=require_str(item.get("segment_ref"), "segment_ref"),
                representation_ref=require_str(
                    item.get("representation_ref"), "representation_ref"
                ),
                provenance_class=require_enum(
                    item.get("provenance_class"),
                    "provenance_class",
                    SegmentProvenanceClass,
                ),
                original_selected_item_ref=require_opt_str(
                    item.get("original_selected_item_ref"), "original_selected_item_ref"
                ),
                transformation_refs=tuple(
                    require_str_list(
                        item.get("transformation_refs", []), "transformation_refs"
                    )
                ),
            )
        )
    return ContextDeliveryReceipt(
        receipt_id=require_str(value.get("receipt_id"), "receipt_id"),
        workspace_id=require_str(value.get("workspace_id"), "workspace_id"),
        consumer_ref=require_str(value.get("consumer_ref"), "consumer_ref"),
        manifest_ref=require_str(value.get("manifest_ref"), "manifest_ref"),
        events=tuple(events),
        issuer_class=require_enum(
            value.get("issuer_class"), "issuer_class", ReceiptIssuerClass
        ),
        run_ref=require_opt_str(value.get("run_ref"), "run_ref"),
        step_ref=require_opt_str(value.get("step_ref"), "step_ref"),
        attempt_ref=require_opt_str(value.get("attempt_ref"), "attempt_ref"),
        pack_ref=require_opt_str(value.get("pack_ref"), "pack_ref"),
        supplied_segments=tuple(segments),
        additional_source_refs=tuple(
            require_str_list(
                value.get("additional_source_refs", []), "additional_source_refs"
            )
        ),
        transformations_after_core=tuple(
            require_str_list(
                value.get("transformations_after_core", []),
                "transformations_after_core",
            )
        ),
        instruction_version_refs=tuple(
            require_str_list(
                value.get("instruction_version_refs", []), "instruction_version_refs"
            )
        ),
        skill_workflow_version_refs=tuple(
            require_str_list(
                value.get("skill_workflow_version_refs", []),
                "skill_workflow_version_refs",
            )
        ),
        tool_result_refs=tuple(
            require_str_list(value.get("tool_result_refs", []), "tool_result_refs")
        ),
        provider_ref=require_opt_str(value.get("provider_ref"), "provider_ref"),
        model_ref=require_opt_str(value.get("model_ref"), "model_ref"),
        effective_configuration_refs=tuple(
            require_str_list(
                value.get("effective_configuration_refs", []),
                "effective_configuration_refs",
            )
        ),
        instrumentation_complete=_bool(
            value.get("instrumentation_complete"), "instrumentation_complete"
        ),
        result_ref=require_opt_str(value.get("result_ref"), "result_ref"),
    )


def feedback_to_content(value: ExpertFeedback) -> dict[str, Any]:
    content = {
        "profile_version": value.profile_version,
        "feedback_id": value.feedback_id,
        "workspace_id": value.workspace_id,
        "submitted_by": value.submitted_by,
        "recorded_at": encode_instant(value.recorded_at),
        "correction_content": value.correction_content,
        "classification": value.classification.value,
        "retention_class": value.retention_class,
        "source_result_ref": value.source_result_ref,
        "affected_output_spans": [
            encode_span(item) for item in value.affected_output_spans
        ],
        "proposed_applicability_scope_refs": list(
            value.proposed_applicability_scope_refs
        ),
        "supporting_evidence_refs": list(value.supporting_evidence_refs),
        "contradicting_evidence_refs": list(value.contradicting_evidence_refs),
        "context_receipt_refs": list(value.context_receipt_refs),
        "context_coverage": value.context_coverage.value,
        "expertise_claim": None
        if value.expertise_claim is None
        else {
            "source": value.expertise_claim.source.value,
            "verified_role_ref": value.expertise_claim.verified_role_ref,
            "unverified_claim_text": value.expertise_claim.unverified_claim_text,
        },
        "revision_of_feedback_ref": value.revision_of_feedback_ref,
        "disposition": value.disposition.value,
    }
    enforce_ov_cj1_content_limit(content)
    return content


def feedback_from_content(content: object) -> ExpertFeedback:
    value = _profile(content, FEEDBACK_PROFILE_VERSION)
    spans_raw = value.get("affected_output_spans", [])
    require(
        isinstance(spans_raw, list),
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        "affected_output_spans must be a list",
    )
    claim_raw = value.get("expertise_claim")
    claim = None
    if claim_raw is not None:
        item = require_mapping(claim_raw, "expertise_claim")
        claim = ExpertiseClaim(
            source=require_enum(
                item.get("source"), "expertise source", ExpertiseSource
            ),
            verified_role_ref=require_opt_str(
                item.get("verified_role_ref"), "verified_role_ref"
            ),
            unverified_claim_text=require_opt_str(
                item.get("unverified_claim_text"), "unverified_claim_text"
            ),
        )
    result = ExpertFeedback(
        feedback_id=require_str(value.get("feedback_id"), "feedback_id"),
        workspace_id=require_str(value.get("workspace_id"), "workspace_id"),
        submitted_by=require_str(value.get("submitted_by"), "submitted_by"),
        recorded_at=decode_instant(value.get("recorded_at")),
        correction_content=require_str(
            value.get("correction_content"), "correction_content"
        ),
        classification=require_enum(
            value.get("classification"), "classification", Classification
        ),
        retention_class=require_str(value.get("retention_class"), "retention_class"),
        source_result_ref=require_opt_str(
            value.get("source_result_ref"), "source_result_ref"
        ),
        affected_output_spans=tuple(decode_span(item) for item in spans_raw),
        proposed_applicability_scope_refs=tuple(
            require_str_list(
                value.get("proposed_applicability_scope_refs", []),
                "proposed_applicability_scope_refs",
            )
        ),
        supporting_evidence_refs=tuple(
            require_str_list(
                value.get("supporting_evidence_refs", []), "supporting_evidence_refs"
            )
        ),
        contradicting_evidence_refs=tuple(
            require_str_list(
                value.get("contradicting_evidence_refs", []),
                "contradicting_evidence_refs",
            )
        ),
        context_receipt_refs=tuple(
            require_str_list(
                value.get("context_receipt_refs", []), "context_receipt_refs"
            )
        ),
        context_coverage=require_enum(
            value.get("context_coverage"), "context_coverage", FeedbackContextCoverage
        ),
        expertise_claim=claim,
        revision_of_feedback_ref=require_opt_str(
            value.get("revision_of_feedback_ref"), "revision_of_feedback_ref"
        ),
        disposition=require_enum(
            value.get("disposition"), "disposition", FeedbackDisposition
        ),
    )
    enforce_ov_cj1_content_limit(feedback_to_content(result))
    return result


def diagnosis_to_content(value: FeedbackDiagnosis) -> dict[str, Any]:
    return {
        "profile_version": value.profile_version,
        "diagnosis_id": value.diagnosis_id,
        "workspace_id": value.workspace_id,
        "feedback_ref": value.feedback_ref,
        "analyst_ref": value.analyst_ref,
        "recorded_at": encode_instant(value.recorded_at),
        "categories": [item.value for item in value.categories],
        "original_context_reconstructed": value.original_context_reconstructed,
        "proposed_owner": value.proposed_owner,
        "evidence_for": list(value.evidence_for),
        "evidence_against": list(value.evidence_against),
        "coverage_note": value.coverage_note,
        "confidence": value.confidence,
    }


def diagnosis_from_content(content: object) -> FeedbackDiagnosis:
    value = _profile(content, DIAGNOSIS_PROFILE_VERSION)
    confidence = value.get("confidence")
    require(
        confidence is None
        or (isinstance(confidence, (int, float)) and not isinstance(confidence, bool)),
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        "confidence must be a number or null",
    )
    categories = require_str_list(value.get("categories"), "categories")
    return FeedbackDiagnosis(
        diagnosis_id=require_str(value.get("diagnosis_id"), "diagnosis_id"),
        workspace_id=require_str(value.get("workspace_id"), "workspace_id"),
        feedback_ref=require_str(value.get("feedback_ref"), "feedback_ref"),
        analyst_ref=require_str(value.get("analyst_ref"), "analyst_ref"),
        recorded_at=decode_instant(value.get("recorded_at")),
        categories=tuple(
            require_enum(item, "category", DiagnosisCategory) for item in categories
        ),
        original_context_reconstructed=_bool(
            value.get("original_context_reconstructed"),
            "original_context_reconstructed",
        ),
        proposed_owner=require_str(value.get("proposed_owner"), "proposed_owner"),
        evidence_for=tuple(
            require_str_list(value.get("evidence_for", []), "evidence_for")
        ),
        evidence_against=tuple(
            require_str_list(value.get("evidence_against", []), "evidence_against")
        ),
        coverage_note=require_str(value.get("coverage_note", ""), "coverage_note"),
        confidence=None if confidence is None else float(confidence),
    )


def proposal_to_content(value: KnowledgeImprovementProposal) -> dict[str, Any]:
    return {
        "profile_version": value.profile_version,
        "proposal_id": value.proposal_id,
        "workspace_id": value.workspace_id,
        "transition_kind": value.transition_kind.value,
        "base_version_refs": list(value.base_version_refs),
        "risk_policy_ref": value.risk_policy_ref,
        "required_reviewer_refs": list(value.required_reviewer_refs),
        "feedback_ref": value.feedback_ref,
        "diagnosis_ref": value.diagnosis_ref,
        "supporting_evidence_refs": list(value.supporting_evidence_refs),
        "contradicting_evidence_refs": list(value.contradicting_evidence_refs),
        "applicability_changes": encode_opt_expression(value.applicability_changes),
        "temporal_changes_note": value.temporal_changes_note,
        "expected_consumer_impact_refs": list(value.expected_consumer_impact_refs),
        "required_evaluation_case_refs": list(value.required_evaluation_case_refs),
        "disposition": value.disposition.value,
    }


def proposal_from_content(content: object) -> KnowledgeImprovementProposal:
    value = _profile(content, PROPOSAL_PROFILE_VERSION)
    return KnowledgeImprovementProposal(
        proposal_id=require_str(value.get("proposal_id"), "proposal_id"),
        workspace_id=require_str(value.get("workspace_id"), "workspace_id"),
        transition_kind=require_enum(
            value.get("transition_kind"), "transition_kind", ProposalTransitionKind
        ),
        base_version_refs=tuple(
            require_str_list(value.get("base_version_refs"), "base_version_refs")
        ),
        risk_policy_ref=require_str(value.get("risk_policy_ref"), "risk_policy_ref"),
        required_reviewer_refs=tuple(
            require_str_list(
                value.get("required_reviewer_refs"), "required_reviewer_refs"
            )
        ),
        feedback_ref=require_opt_str(value.get("feedback_ref"), "feedback_ref"),
        diagnosis_ref=require_opt_str(value.get("diagnosis_ref"), "diagnosis_ref"),
        supporting_evidence_refs=tuple(
            require_str_list(
                value.get("supporting_evidence_refs", []), "supporting_evidence_refs"
            )
        ),
        contradicting_evidence_refs=tuple(
            require_str_list(
                value.get("contradicting_evidence_refs", []),
                "contradicting_evidence_refs",
            )
        ),
        applicability_changes=decode_opt_expression(value.get("applicability_changes")),
        temporal_changes_note=require_str(
            value.get("temporal_changes_note", ""), "temporal_changes_note"
        ),
        expected_consumer_impact_refs=tuple(
            require_str_list(
                value.get("expected_consumer_impact_refs", []),
                "expected_consumer_impact_refs",
            )
        ),
        required_evaluation_case_refs=tuple(
            require_str_list(
                value.get("required_evaluation_case_refs", []),
                "required_evaluation_case_refs",
            )
        ),
        disposition=require_enum(
            value.get("disposition"), "disposition", ProposalDisposition
        ),
        admission_reference=None,
    )


def dependency_to_content(value: KnowledgeConsumerDependency) -> dict[str, Any]:
    return {
        "profile_version": value.profile_version,
        "consumer": {
            "consumer_id": value.consumer.consumer_id,
            "kind": value.consumer.kind.value,
            "owner": value.consumer.owner,
            "deployment_identity": value.consumer.deployment_identity,
            "criticality": value.consumer.criticality,
            "lifecycle_state": value.consumer.lifecycle_state,
        },
        "dependency": {
            "consumer_id": value.dependency.consumer_id,
            "model_id": value.dependency.model_id,
            "element_id": value.dependency.element_id,
            "dependency_mode": value.dependency.dependency_mode.value,
            "usage_location": value.dependency.usage_location,
            "fallback_behaviour": value.dependency.fallback_behaviour,
        },
        "dependency_class": value.dependency_class.value,
        "exact_binding": None
        if value.exact_binding is None
        else {
            "consumer_id": value.exact_binding.consumer_id,
            "model_version_id": value.exact_binding.model_version_id,
            "binding_state": value.exact_binding.binding_state,
            "action_contract_digest": value.exact_binding.action_contract_digest,
        },
        "bounded_scope_refs": list(value.bounded_scope_refs),
    }


def dependency_from_content(content: object) -> KnowledgeConsumerDependency:
    value = _profile(content, KNOWLEDGE_DEPENDENCY_PROFILE_VERSION)
    consumer_raw = require_mapping(value.get("consumer"), "consumer")
    dependency_raw = require_mapping(value.get("dependency"), "dependency")
    exact_raw = value.get("exact_binding")
    exact = None
    if exact_raw is not None:
        item = require_mapping(exact_raw, "exact_binding")
        exact = ExactBinding(
            consumer_id=require_str(item.get("consumer_id"), "consumer_id"),
            model_version_id=require_str(
                item.get("model_version_id"), "model_version_id"
            ),
            binding_state=require_str(
                item.get("binding_state", "bound"), "binding_state"
            ),
            action_contract_digest=require_opt_str(
                item.get("action_contract_digest"), "action_contract_digest"
            ),
        )
    return KnowledgeConsumerDependency(
        consumer=Consumer(
            consumer_id=require_str(consumer_raw.get("consumer_id"), "consumer_id"),
            kind=require_enum(consumer_raw.get("kind"), "consumer kind", ConsumerKind),
            owner=require_str(consumer_raw.get("owner"), "owner"),
            deployment_identity=require_str(
                consumer_raw.get("deployment_identity"), "deployment_identity"
            ),
            criticality=require_str(
                consumer_raw.get("criticality", "standard"), "criticality"
            ),
            lifecycle_state=require_str(
                consumer_raw.get("lifecycle_state", "active"), "lifecycle_state"
            ),
        ),
        dependency=ConsumerDependency(
            consumer_id=require_str(dependency_raw.get("consumer_id"), "consumer_id"),
            model_id=require_str(dependency_raw.get("model_id"), "model_id"),
            element_id=require_str(dependency_raw.get("element_id"), "element_id"),
            dependency_mode=require_enum(
                dependency_raw.get("dependency_mode"), "dependency_mode", DependencyMode
            ),
            usage_location=require_str(
                dependency_raw.get("usage_location"), "usage_location"
            ),
            fallback_behaviour=require_opt_str(
                dependency_raw.get("fallback_behaviour"), "fallback_behaviour"
            ),
        ),
        dependency_class=require_enum(
            value.get("dependency_class"), "dependency_class", KnowledgeDependencyClass
        ),
        exact_binding=exact,
        bounded_scope_refs=tuple(
            require_str_list(value.get("bounded_scope_refs", []), "bounded_scope_refs")
        ),
    )


__all__ = [
    "compute_selection_manifest_digest",
    "delivery_receipt_from_content",
    "delivery_receipt_to_content",
    "dependency_from_content",
    "dependency_to_content",
    "diagnosis_from_content",
    "diagnosis_to_content",
    "feedback_from_content",
    "feedback_to_content",
    "proposal_from_content",
    "proposal_to_content",
    "selection_manifest_from_content",
    "selection_manifest_to_content",
    "task_context_profile_from_content",
    "task_context_profile_to_content",
    "verify_selection_manifest_digest",
]
