"""Tests for KI-02/KI-03 `TaskContextProfile` and `ContextSelectionManifest`.

Pure representation/building-block coverage (spec 5.1, 7.1, 8.2): no
credential/model field exists on the profile, and the manifest carries the
same `ApplicabilityResult` values the evaluator produced, unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnivia_core.governed_knowledge.applicability import (
    ApplicabilityExpression,
    ApplicabilityOutcome,
    ComparisonNode,
    ComparisonOperator,
    FactEntry,
    FactSnapshot,
    evaluate_applicability,
    fact_ref,
    literal,
)
from omnivia_core.governed_knowledge.context import (
    ContextFreshnessPolicy,
    ContextSelectionManifest,
    TaskContextProfile,
)
from omnivia_core.governed_knowledge.errors import GovernedKnowledgeValidationError
from omnivia_core.governed_knowledge.profile_content import (
    selection_manifest_from_content,
    selection_manifest_to_content,
    task_context_profile_from_content,
    task_context_profile_to_content,
)
from omnivia_core.semantic_registry.consumers import ConsumerKind
from omnivia_core.semantic_registry.temporal import (
    TemporalInstant,
    TemporalPrecision,
    TemporalProvenance,
)


def _instant() -> TemporalInstant:
    return TemporalInstant(
        value=datetime(2026, 1, 1, tzinfo=UTC),
        precision=TemporalPrecision.DAY,
        provenance=TemporalProvenance.STATED,
    )


def test_task_context_profile_has_no_credential_or_model_field() -> None:
    fields = set(TaskContextProfile.__dataclass_fields__)
    for forbidden in ("model", "provider", "api_key", "credential"):
        assert not any(forbidden in name for name in fields)


def test_task_context_profile_valid_construction() -> None:
    profile = TaskContextProfile(
        profile_id="profile-1",
        version_ref="profile-1-v1",
        purpose="client_renewal_brief",
        workspace_id="ws-1",
        allowed_consumer_kinds=(ConsumerKind.APP,),
        max_bytes=8192,
        response_shape="answer_with_citations",
    )
    assert profile.max_bytes == 8192
    assert (
        task_context_profile_from_content(task_context_profile_to_content(profile))
        == profile
    )


def test_task_context_profile_rejects_non_positive_max_bytes() -> None:
    with pytest.raises(GovernedKnowledgeValidationError):
        TaskContextProfile(
            profile_id="profile-1",
            version_ref="profile-1-v1",
            purpose="client_renewal_brief",
            workspace_id="ws-1",
            max_bytes=0,
            response_shape="answer_with_citations",
        )


def test_manifest_carries_evaluator_result_unchanged() -> None:
    node = ComparisonNode(
        operator=ComparisonOperator.EQUALS,
        left=fact_ref("region"),
        right=literal("emea"),
    )
    result = evaluate_applicability(
        required=ApplicabilityExpression(root=node),
        facts=FactSnapshot(
            fact_snapshot_ref="snap-1",
            facts={"region": FactEntry(values=(literal("emea"),))},
        ),
        position_version_ref="pos-1-v1",
    )
    assert result.outcome is ApplicabilityOutcome.APPLICABLE

    manifest = ContextSelectionManifest(
        manifest_id="manifest-1",
        profile_version_ref="profile-1-v1",
        workspace_id="ws-1",
        purpose="client_renewal_brief",
        consumer_ref="consumer-1",
        request_ref="req-1",
        semantic_model_version_ref="model-v1",
        valid_at=_instant(),
        recorded_as_of=_instant(),
        query_time=_instant(),
        applicability_results=(result,),
        effective_byte_bound=8192,
        policy_version="policy-v1",
    )
    assert manifest.applicability_results[0] is result
    assert (
        selection_manifest_from_content(selection_manifest_to_content(manifest))
        == manifest
    )


def test_profile_codec_rejects_unknown_version() -> None:
    content = task_context_profile_to_content(
        TaskContextProfile(
            profile_id="profile-1",
            version_ref="profile-1-v1",
            purpose="client_renewal_brief",
            workspace_id="ws-1",
            max_bytes=8192,
            response_shape="answer_with_citations",
        )
    )
    content["profile_version"] = "future-profile"
    with pytest.raises(GovernedKnowledgeValidationError):
        task_context_profile_from_content(content)


def test_manifest_requires_temporal_instants() -> None:
    with pytest.raises(GovernedKnowledgeValidationError):
        ContextSelectionManifest(
            manifest_id="manifest-1",
            profile_version_ref="profile-1-v1",
            workspace_id="ws-1",
            purpose="client_renewal_brief",
            consumer_ref="consumer-1",
            request_ref="req-1",
            semantic_model_version_ref="model-v1",
            valid_at="not-an-instant",  # type: ignore[arg-type]
            recorded_as_of=_instant(),
            query_time=_instant(),
            effective_byte_bound=8192,
            policy_version="policy-v1",
        )


def test_freshness_policy_must_be_declared_explicitly() -> None:
    profile = TaskContextProfile(
        profile_id="profile-1",
        version_ref="profile-1-v1",
        purpose="client_renewal_brief",
        workspace_id="ws-1",
        max_bytes=8192,
        response_shape="answer_with_citations",
        freshness_policy=ContextFreshnessPolicy.PERMISSIVE_WITH_WARNING,
    )
    assert profile.freshness_policy is ContextFreshnessPolicy.PERMISSIVE_WITH_WARNING
