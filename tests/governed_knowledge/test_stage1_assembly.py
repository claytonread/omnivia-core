"""Stage 1 synthetic renewal fixture and inline selection-manifest acceptance."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnivia_core.contracts.v1 import codec
from omnivia_core.contracts.v1.generated import ContextPackBuildResult
from omnivia_core.contracts.v1.semantics_knowledge import (
    verify_context_pack_artifact_document,
)
from omnivia_core.governed_knowledge import (
    ApplicabilityExpression,
    ApplicabilityOutcome,
    BooleanNode,
    BooleanOperator,
    ComparisonNode,
    ComparisonOperator,
    ContextFreshnessPolicy,
    FactEntry,
    FactProvenanceClass,
    FactSnapshot,
    GovernedKnowledgeValidationError,
    KnowledgeConsumerDependency,
    KnowledgeDependencyClass,
    OrganisationalPosition,
    PositionLifecycleState,
    Stage1ReferenceClient,
    TaskContextProfile,
    assemble_stage1_context,
    enum,
    fact_ref,
    verify_selection_manifest_digest,
)
from omnivia_core.semantic_registry.consumers import (
    Consumer,
    ConsumerDependency,
    ConsumerKind,
    DependencyMode,
)
from omnivia_core.semantic_registry.evidence import Classification
from omnivia_core.semantic_registry.temporal import (
    TemporalInstant,
    TemporalPrecision,
    TemporalProvenance,
)

ROOT = Path(__file__).resolve().parents[2]


def _vector(vector_id: str) -> object:
    corpus = json.loads(
        (ROOT / "packages/omnivia-core-client/tests/fixtures/ovc1-v1.json").read_text()
    )
    return next(
        item["payload"] for item in corpus["vectors"] if item["id"] == vector_id
    )


def _instant(year: int = 2026) -> TemporalInstant:
    return TemporalInstant(
        value=datetime(year, 9, 13, tzinfo=UTC),
        precision=TemporalPrecision.SECOND,
        provenance=TemporalProvenance.STATED,
    )


def _pack() -> ContextPackBuildResult:
    corpus = json.loads(
        (
            ROOT
            / "contracts/application/v1/fixtures/application-wire-adapter-conformance-v1.json"
        ).read_text()
    )
    case = next(
        item
        for item in corpus["cases"]
        if item["id"] == "context_pack.build/primary-success"
    )
    document = json.dumps(case["response"]["result"], separators=(",", ":"))
    return verify_context_pack_artifact_document(document)


def _profile(*, max_bytes: int = 1_000_000) -> TaskContextProfile:
    return TaskContextProfile(
        profile_id="renewal-brief",
        version_ref="renewal-brief-v1",
        purpose="assist.chat",
        workspace_id="ws-1",
        allowed_consumer_kinds=(ConsumerKind.APP,),
        required_scope_refs=("memory:read",),
        required_fact_refs=("billing_cycle", "payment_timing"),
        knowledge_domain_refs=("commercial",),
        selection_stage_refs=("admitted_positions", "context_pack"),
        freshness_policy=ContextFreshnessPolicy.STRICT,
        max_bytes=max_bytes,
        response_shape="renewal_brief",
    )


def _dependency() -> KnowledgeConsumerDependency:
    consumer = Consumer(
        consumer_id="renewal-reference-client",
        kind=ConsumerKind.APP,
        owner="core",
        deployment_identity="renewal-reference-client-v1",
    )
    return KnowledgeConsumerDependency(
        consumer=consumer,
        dependency=ConsumerDependency(
            consumer_id=consumer.consumer_id,
            model_id="model-commercial",
            element_id="renewal-discount-position",
            dependency_mode=DependencyMode.REQUIRED,
            usage_location="renewal_brief",
        ),
        dependency_class=KnowledgeDependencyClass.EXPLICIT_LOGICAL_REFERENCE,
    )


def _position(version: str, cycle: str) -> OrganisationalPosition:
    required = ApplicabilityExpression(
        root=BooleanNode(
            operator=BooleanOperator.ALL,
            children=(
                ComparisonNode(
                    operator=ComparisonOperator.EQUALS,
                    left=fact_ref("billing_cycle"),
                    right=enum(cycle),
                ),
                ComparisonNode(
                    operator=ComparisonOperator.EQUALS,
                    left=fact_ref("payment_timing"),
                    right=enum("prepaid"),
                ),
            ),
        )
    )
    return OrganisationalPosition(
        position_id=f"renewal-{cycle}",
        version_ref=version,
        title=f"{cycle.title()} renewal discount",
        statement=f"{cycle.title()} prepaid renewals qualify for the approved discount.",
        domain_refs=("commercial",),
        position_kind="commercial_eligibility",
        semantic_model_version_ref="model-commercial-v1",
        workspace_id="ws-1",
        required_conditions=required,
        declared_required_facts=("billing_cycle", "payment_timing"),
        evaluator_version=required.version,
        owning_domain="commercial",
        approved_by_reference=f"approval-{version}",
        supporting_evidence_refs=(f"evidence-{version}",),
        recorded_at=_instant(),
        classification=Classification.INTERNAL,
        retention_class="standard-3y",
        lifecycle_state=PositionLifecycleState.ADMITTED,
    )


def _facts(cycle: str = "annual") -> FactSnapshot:
    return FactSnapshot(
        fact_snapshot_ref="renewal-facts-v1",
        facts={
            "billing_cycle": FactEntry(
                values=(enum(cycle),),
                provenance_class=FactProvenanceClass.USER_ASSUMPTION,
            ),
            "payment_timing": FactEntry(
                values=(enum("prepaid"),),
                provenance_class=FactProvenanceClass.VERIFIED_ORGANISATIONAL_RECORD,
            ),
        },
    )


def _assemble(**overrides: object):  # type: ignore[no-untyped-def]
    fields: dict[str, object] = {
        "profile": _profile(),
        "dependency": _dependency(),
        "context_pack": _pack(),
        "positions": (
            _position("renewal-annual-v1", "annual"),
            _position("renewal-monthly-v1", "monthly"),
        ),
        "facts": _facts(),
        "request_ref": "req-context-pack.build-success",
        "semantic_model_version_ref": "model-commercial-v1",
        "valid_at": _instant(),
        "recorded_as_of": _instant(),
        "query_time": _instant(),
    }
    fields.update(overrides)
    return assemble_stage1_context(**fields)  # type: ignore[arg-type]


def test_annual_prepaid_is_selected_and_monthly_is_excluded() -> None:
    bundle = _assemble()
    repeated = _assemble()
    assert tuple(item.version_ref for item in bundle.applicable_positions) == (
        "renewal-annual-v1",
    )
    outcomes = {
        item.position_version_ref: item.outcome
        for item in bundle.manifest.applicability_results
    }
    assert outcomes == {
        "renewal-annual-v1": ApplicabilityOutcome.APPLICABLE,
        "renewal-monthly-v1": ApplicabilityOutcome.NOT_APPLICABLE,
    }
    assert bundle.manifest.pack_identity == bundle.context_pack.pack_id
    assert verify_selection_manifest_digest(bundle.manifest)
    assert repeated.manifest == bundle.manifest


def test_pending_position_never_enters_strict_selection() -> None:
    pending = _position("renewal-pending-v1", "annual")
    pending = replace(pending, lifecycle_state=PositionLifecycleState.PROPOSED)
    bundle = _assemble(positions=(pending,))
    assert bundle.applicable_positions == ()
    assert bundle.manifest.omissions[0].reason_code == "pending_or_noncurrent"


def test_conflicting_applicable_positions_are_contested_not_selected() -> None:
    first = _position("renewal-a-v1", "annual")
    second = _position("renewal-b-v1", "annual")
    first = replace(first, contradicts_position_refs=(second.version_ref,))
    bundle = _assemble(positions=(first, second))
    assert bundle.applicable_positions == ()
    assert {
        item.outcome for item in bundle.manifest.applicability_results
    } == {ApplicabilityOutcome.CONTESTED}
    assert {
        item.item_version_ref
        for item in bundle.manifest.omissions
        if item.reason_code == "contested"
    } == {first.version_ref, second.version_ref}


def test_missing_profile_fact_is_refused_without_guessing() -> None:
    facts = FactSnapshot(
        fact_snapshot_ref="incomplete-facts",
        facts={"billing_cycle": FactEntry(values=(enum("annual"),))},
    )
    with pytest.raises(GovernedKnowledgeValidationError):
        _assemble(facts=facts)


def test_profile_byte_limit_refuses_instead_of_truncating() -> None:
    with pytest.raises(GovernedKnowledgeValidationError):
        _assemble(profile=_profile(max_bytes=1))


class _Caller:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[tuple[object, object, object]] = []

    def call(
        self, request: object, *, deadline: object, cancellation: object = None
    ) -> object:
        self.calls.append((request, deadline, cancellation))
        return self.response


def test_reference_client_uses_the_shared_call_and_returns_inline_manifest() -> None:
    request = codec.decode_request(_vector("application.request"))
    request = replace(
        request,
        operation="context_pack.build",
        metadata=replace(
            request.metadata,
            request_id="req-context-pack.build-success",
            correlation_id="req-context-pack.build-success",
            workspace_id="ws-1",
            purpose="assist.chat",
        ),
        input={"query": "hello", "mode": "deterministic_view", "token_budget": 1000},
    )
    response = codec.decode_success_response(_vector("application.success"))
    response = replace(
        response,
        metadata=replace(
            response.metadata,
            request_id=request.metadata.request_id,
            correlation_id=request.metadata.correlation_id,
        ),
        result=_pack().to_wire(),
    )
    caller = _Caller(response)
    deadline = object()
    result = Stage1ReferenceClient(caller=caller).build_context(  # type: ignore[arg-type]
        request,
        profile=_profile(),
        dependency=_dependency(),
        positions=(_position("renewal-annual-v1", "annual"),),
        facts=_facts(),
        semantic_model_version_ref="model-commercial-v1",
        valid_at=_instant(),
        recorded_as_of=_instant(),
        query_time=_instant(),
        deadline=deadline,
    )
    assert result.succeeded
    assert result.bundle is not None
    assert verify_selection_manifest_digest(result.bundle.manifest)
    assert caller.calls == [(request, deadline, None)]
