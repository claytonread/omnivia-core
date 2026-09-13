"""Stage 1 deterministic governed-context assembly over an accepted Context Pack.

This module is deliberately pure.  The existing ``context_pack.build`` operation owns
the authorised read and snapshot; this layer applies a versioned task profile to the
already-authorised material, evaluates admitted organisational positions, and emits the
inline selection manifest consumed by the reference client.  It performs no I/O and
cannot turn a pending profile into accepted knowledge.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from omnivia_core.contracts.v1.canonical_json import canonicalize
from omnivia_core.contracts.v1.generated import ContextPackBuildResult
from omnivia_core.governed_knowledge.applicability import (
    ApplicabilityOutcome,
    FactSnapshot,
    contested_positions,
    evaluate_applicability,
)
from omnivia_core.governed_knowledge.context import (
    ContextFreshnessPolicy,
    ContextSelectionManifest,
    ManifestOmission,
    SelectedContextItem,
    SelectionAuthorityClass,
    TaskContextProfile,
)
from omnivia_core.governed_knowledge.dependency import KnowledgeConsumerDependency
from omnivia_core.governed_knowledge.errors import (
    GovernedKnowledgeErrorCode,
    require,
)
from omnivia_core.governed_knowledge.position import (
    OrganisationalPosition,
    PositionLifecycleState,
)
from omnivia_core.governed_knowledge.profile_content import (
    compute_selection_manifest_digest,
)
from omnivia_core.semantic_registry.temporal import TemporalInstant

SELECTION_POLICY_VERSION = "governed-knowledge-stage1-selection-v1"
RETRIEVAL_MECHANISM = "context_pack.build/deterministic_view"


@dataclass(frozen=True, slots=True)
class Stage1ContextBundle:
    """One base Context Pack plus the admitted positions selected for the task."""

    context_pack: ContextPackBuildResult
    applicable_positions: tuple[OrganisationalPosition, ...]
    manifest: ContextSelectionManifest


def _pack_bytes(pack: ContextPackBuildResult) -> int:
    return len(canonicalize(pack.to_wire()).encode("utf-8"))


def _position_is_current_at(
    position: OrganisationalPosition, valid_at: TemporalInstant
) -> bool:
    interval = position.valid_interval
    if interval is None:
        return True
    if valid_at.value < interval.effective_from.value:
        return False
    return interval.effective_to is None or valid_at.value < interval.effective_to.value


def _check_binding(
    *,
    profile: TaskContextProfile,
    dependency: KnowledgeConsumerDependency,
    pack: ContextPackBuildResult,
    purpose: str,
) -> None:
    authorization = pack.reproducibility.authorization_context
    require(
        authorization.workspace_id == profile.workspace_id,
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        "the task profile and Context Pack must name the same workspace",
    )
    require(
        authorization.purpose == purpose == profile.purpose,
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        "the task profile, request, and Context Pack purposes must match",
    )
    require(
        not profile.allowed_consumer_kinds
        or dependency.consumer.kind in profile.allowed_consumer_kinds,
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        "the registered consumer kind is not allowed by the task profile",
    )
    require(
        set(profile.required_scope_refs).issubset(authorization.scopes),
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        "the Context Pack authority does not satisfy the task profile scopes",
    )
    require(
        dependency.consumer.lifecycle_state == "active",
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        "the registered consumer must be active",
    )
    require(
        pack.fresh_authorization_required,
        GovernedKnowledgeErrorCode.INVALID_FIELD,
        "the Context Pack must retain its fresh-authorisation requirement",
    )


def assemble_stage1_context(
    *,
    profile: TaskContextProfile,
    dependency: KnowledgeConsumerDependency,
    context_pack: ContextPackBuildResult,
    positions: tuple[OrganisationalPosition, ...],
    facts: FactSnapshot,
    request_ref: str,
    semantic_model_version_ref: str,
    valid_at: TemporalInstant,
    recorded_as_of: TemporalInstant,
    query_time: TemporalInstant,
) -> Stage1ContextBundle:
    """Apply one Stage 1 task profile and return its pack with an inline manifest.

    ``positions`` must be the caller's already-authorised, exact governed-record views.
    Only admitted, temporally current, model-bound positions can enter the returned
    selection.  Missing facts remain explicit ``needs_information`` omissions.
    """
    _check_binding(
        profile=profile,
        dependency=dependency,
        pack=context_pack,
        purpose=profile.purpose,
    )
    require(
        all(ref in facts.facts for ref in profile.required_fact_refs),
        GovernedKnowledgeErrorCode.MISSING_FIELD,
        "the task fact snapshot omits a fact required by the task profile",
    )

    measured_bytes = _pack_bytes(context_pack)
    require(
        measured_bytes <= profile.max_bytes,
        GovernedKnowledgeErrorCode.PAYLOAD_TOO_LARGE,
        "the authorised Context Pack exceeds the task profile byte bound",
    )

    selected_positions: list[OrganisationalPosition] = []
    selected_items: list[SelectedContextItem] = []
    applicability_results = []
    omissions: list[ManifestOmission] = []
    overdue: list[str] = []

    for position in sorted(positions, key=lambda item: item.version_ref):
        require(
            position.workspace_id == profile.workspace_id,
            GovernedKnowledgeErrorCode.INVALID_FIELD,
            "a candidate position belongs to another workspace",
        )
        if position.lifecycle_state is not PositionLifecycleState.ADMITTED:
            omissions.append(
                ManifestOmission(
                    reason_code="pending_or_noncurrent",
                    safe_reason="Non-admitted knowledge was excluded.",
                    item_version_ref=position.version_ref,
                )
            )
            continue
        if position.semantic_model_version_ref != semantic_model_version_ref:
            omissions.append(
                ManifestOmission(
                    reason_code="semantic_binding_mismatch",
                    safe_reason="Knowledge bound to another semantic version was excluded.",
                    item_version_ref=position.version_ref,
                )
            )
            continue
        if profile.knowledge_domain_refs and not set(position.domain_refs).intersection(
            profile.knowledge_domain_refs
        ):
            omissions.append(
                ManifestOmission(
                    reason_code="outside_profile_domain",
                    safe_reason="Knowledge outside the task profile domain was excluded.",
                    item_version_ref=position.version_ref,
                )
            )
            continue
        if (
            position.recorded_at is not None
            and position.recorded_at.value > recorded_as_of.value
        ):
            omissions.append(
                ManifestOmission(
                    reason_code="recorded_after_query_axis",
                    safe_reason="Knowledge recorded after the requested axis was excluded.",
                    item_version_ref=position.version_ref,
                )
            )
            continue
        if not _position_is_current_at(position, valid_at):
            omissions.append(
                ManifestOmission(
                    reason_code="outside_valid_interval",
                    safe_reason="Knowledge outside the requested valid time was excluded.",
                    item_version_ref=position.version_ref,
                )
            )
            continue
        if (
            position.review_due_at is not None
            and query_time.value >= position.review_due_at.value
        ):
            overdue.append(position.version_ref)
            if profile.freshness_policy is ContextFreshnessPolicy.STRICT:
                omissions.append(
                    ManifestOmission(
                        reason_code="review_overdue",
                        safe_reason="Review-overdue knowledge was excluded by profile policy.",
                        item_version_ref=position.version_ref,
                    )
                )
                continue
        require(
            position.required_conditions is not None,
            GovernedKnowledgeErrorCode.MISSING_FIELD,
            "Stage 1 positions require an explicit applicability expression",
        )
        assert position.required_conditions is not None
        result = evaluate_applicability(
            required=position.required_conditions,
            exceptions=tuple(item.condition for item in position.exceptions),
            facts=facts,
            position_version_ref=position.version_ref,
        )
        applicability_results.append(result)
        if result.outcome is ApplicabilityOutcome.APPLICABLE:
            selected_positions.append(position)
            selected_items.append(
                SelectedContextItem(
                    item_version_ref=position.version_ref,
                    authority_class=SelectionAuthorityClass.ADMITTED_KNOWLEDGE,
                    selection_reason="Applicable admitted organisational position.",
                    source_span_refs=position.supporting_evidence_refs,
                    applicability_result_ref=(
                        f"{facts.fact_snapshot_ref}:{position.version_ref}"
                    ),
                )
            )
        else:
            omissions.append(
                ManifestOmission(
                    reason_code=result.outcome.value,
                    safe_reason=" ".join(result.reasons),
                    item_version_ref=position.version_ref,
                )
            )

    contested = contested_positions(
        (item.version_ref for item in selected_positions),
        (
            (item.version_ref, contradicted)
            for item in selected_positions
            for contradicted in item.contradicts_position_refs
        ),
    )
    if contested:
        selected_positions = [
            item for item in selected_positions if item.version_ref not in contested
        ]
        selected_items = [
            item for item in selected_items if item.item_version_ref not in contested
        ]
        applicability_results = [
            replace(
                item,
                outcome=ApplicabilityOutcome.CONTESTED,
                reasons=("Applicable positions conflict without approved precedence.",),
            )
            if item.position_version_ref in contested
            else item
            for item in applicability_results
        ]
        omissions.extend(
            ManifestOmission(
                reason_code="contested",
                safe_reason="Conflicting applicable knowledge requires authorised review.",
                item_version_ref=version_ref,
            )
            for version_ref in sorted(contested)
        )

    pack_refs = tuple(
        f"{item.record_id}@{item.version}"
        for item in context_pack.reproducibility.record_versions
    )
    total_items = (
        len(context_pack.reproducibility.evidence_versions)
        + len(pack_refs)
        + len(selected_positions)
    )
    if profile.max_items is not None:
        require(
            total_items <= profile.max_items,
            GovernedKnowledgeErrorCode.PAYLOAD_TOO_LARGE,
            "the selected context exceeds the task profile item bound",
        )
    checkpoint_refs = tuple(
        f"{name}:{value}"
        for name, value in sorted(
            context_pack.reproducibility.freshness.projection_watermarks.items()
        )
    )
    provisional = ContextSelectionManifest(
        manifest_id="pending-digest",
        profile_version_ref=profile.version_ref,
        workspace_id=profile.workspace_id,
        purpose=profile.purpose,
        consumer_ref=dependency.consumer.consumer_id,
        consumer_deployment_ref=dependency.consumer.deployment_identity,
        request_ref=request_ref,
        semantic_model_version_ref=semantic_model_version_ref,
        valid_at=valid_at,
        recorded_as_of=recorded_as_of,
        query_time=query_time,
        checkpoint_refs=checkpoint_refs,
        selected_item_refs=pack_refs
        + tuple(item.version_ref for item in selected_positions),
        selected_items=tuple(selected_items),
        applicability_results=tuple(applicability_results),
        selection_reasons=(
            "Deterministic Context Pack plus applicable admitted positions.",
        ),
        transformation_notes=(),
        coverage_note="Authorised Context Pack frontier and supplied exact position views.",
        partial_coverage=bool(context_pack.omissions or omissions),
        retrieval_mechanisms=(RETRIEVAL_MECHANISM,),
        omissions=tuple(omissions),
        effective_byte_bound=profile.max_bytes,
        measured_bytes=measured_bytes,
        byte_count_is_estimated=False,
        tokenizer_version=context_pack.reproducibility.tokenizer_version,
        policy_version=SELECTION_POLICY_VERSION,
        freshness_policy=profile.freshness_policy,
        review_overdue_item_refs=tuple(overdue),
        recheck_conditions=("Reauthorise every cited item before dereference.",),
        pack_identity=context_pack.pack_id,
        pack_digest=context_pack.reproducibility.artifact_checksum,
    )
    digest = compute_selection_manifest_digest(provisional)
    manifest = replace(
        provisional,
        manifest_id=f"manifest-{digest.removeprefix('sha256:')}",
        integrity_digest=digest,
    )
    return Stage1ContextBundle(
        context_pack=context_pack,
        applicable_positions=tuple(selected_positions),
        manifest=manifest,
    )


__all__ = [
    "RETRIEVAL_MECHANISM",
    "SELECTION_POLICY_VERSION",
    "Stage1ContextBundle",
    "assemble_stage1_context",
]
