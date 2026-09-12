"""Acceptance tests for the permission-checked Phase 2 application service."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "phase3" / "runtime"))

from omnivia_core_runtime.service.semantic_phase2 import (
    ASSERTION_HISTORY_READ,
    CANDIDATE_AGGREGATE,
    CANDIDATE_CONVERT,
    CANDIDATE_READ,
    CANDIDATE_RECONSIDER,
    CANDIDATE_REJECT,
    EVIDENCE_CONTENT_READ,
    EVIDENCE_METADATA_READ,
    EVIDENCE_REGISTER,
    OBSERVATION_CREATE_MANUAL,
    OBSERVATION_CREATE_RULE,
    SEMANTIC_EVIDENCE_RESTRICTED,
    SEMANTIC_PERMISSION_DENIED,
    SEMANTIC_WORKSPACE_MISMATCH,
    TEMPORAL_QUERY,
    SemanticAuthority,
    SemanticPhase2Service,
    SemanticServiceError,
)
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.storage.semantic_evidence import read_evidence_item
from omnivia_core_runtime.storage.semantic_governance import semantic_governance_writer
from omnivia_core_runtime.storage.semantic_registry import read_review
from test_application_audit_idempotency_migration import (  # type: ignore[import-not-found]
    Owned,
    bootstrap_and_migrate,
    take_ownership,
)
from test_phase2_governance_repository import (  # type: ignore[import-not-found]
    WORKSPACE_ID,
    assertion,
    evidence_link,
    instant,
    register_evidence,
    register_observation,
    seed_model,
)

from omnivia_core.semantic_registry import (
    Classification,
    EvidenceItem,
    EvidenceLink,
    EvidenceLocatorScheme,
    EvidenceSource,
    EvidenceSourceKind,
    EvidenceSupportRole,
    ObservationBundle,
    ObservationGeneration,
    ObservationValueKind,
    ReconsiderationReason,
    SemanticObservation,
    TemporalProvenance,
    add_concept,
)


@pytest.fixture
def owned(tmp_path: Path):
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    holder = take_ownership(path, workspace_id=WORKSPACE_ID)
    yield holder
    holder.connection.close()


def authority(*capabilities: str) -> SemanticAuthority:
    return SemanticAuthority(
        principal_id="principal-1",
        workspace_id=WORKSPACE_ID,
        capabilities=frozenset(capabilities),
    )


def service(
    holder: Owned,
    *,
    capabilities: tuple[str, ...] = (),
    authorizer=None,
    resolver=None,
) -> tuple[SemanticPhase2Service, SemanticAuthority]:
    context = authority(*capabilities)
    return (
        SemanticPhase2Service(
            holder.connection,
            holder.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=holder.generation,
            authorizer=authorizer or (lambda current, capability: capability in current.capabilities),
            content_resolver=resolver,
        ),
        context,
    )


def test_metadata_and_sensitive_content_permissions_are_independent(owned: Owned) -> None:
    register_evidence(owned)
    resolved: list[str] = []

    def resolver(reference: str) -> bytes:
        resolved.append(reference)
        return b"protected"

    metadata_service, metadata_context = service(
        owned, capabilities=(EVIDENCE_METADATA_READ,), resolver=resolver
    )
    metadata = metadata_service.evidence_metadata(metadata_context, "ev-1")
    assert metadata.evidence_id == "ev-1"
    assert metadata.permission_filtered
    assert not hasattr(metadata, "content_ref")
    assert not hasattr(metadata, "content")
    with pytest.raises(SemanticServiceError) as denied:
        metadata_service.sensitive_evidence(metadata_context, "ev-1")
    assert denied.value.code == SEMANTIC_EVIDENCE_RESTRICTED
    assert resolved == []

    content_service, content_context = service(
        owned, capabilities=(EVIDENCE_CONTENT_READ,), resolver=resolver
    )
    with pytest.raises(SemanticServiceError) as metadata_denied:
        content_service.evidence_metadata(content_context, "ev-1")
    assert metadata_denied.value.code == SEMANTIC_PERMISSION_DENIED
    assert content_service.sensitive_evidence(content_context, "ev-1").content == b"protected"
    assert resolved == ["blob://ev-1"]


def test_write_authority_is_rechecked_inside_transaction_and_rolls_back(owned: Owned) -> None:
    calls = 0

    def revoke_after_precheck(_context: SemanticAuthority, capability: str) -> bool:
        nonlocal calls
        assert capability == EVIDENCE_REGISTER
        calls += 1
        return calls == 1

    guarded, context = service(
        owned, capabilities=(EVIDENCE_REGISTER,), authorizer=revoke_after_precheck
    )
    item = EvidenceItem(
        evidence_id="ev-revoked",
        workspace_id=WORKSPACE_ID,
        source=EvidenceSource(
            source_id="src-revoked",
            kind=EvidenceSourceKind.DOCUMENT,
            locator_scheme=EvidenceLocatorScheme.HTTPS,
            locator="https://example.test/revoked",
            version="v1",
        ),
        content_ref="blob://revoked",
        content_digest="sha256:" + "c" * 64,
        integrity_digest="sha256:" + "d" * 64,
        mime_type="text/plain",
        classification=Classification.INTERNAL,
        retention_class="standard",
        captured_at=instant(0),
    )
    with pytest.raises(SemanticServiceError) as denied:
        guarded.register_evidence(context, item, actor_id="principal-1")
    assert denied.value.code == SEMANTIC_PERMISSION_DENIED
    assert calls == 2
    assert read_evidence_item(owned.connection, WORKSPACE_ID, "ev-revoked") is None


def test_transport_workspace_and_actor_claims_cannot_be_forged(owned: Owned) -> None:
    register_evidence(owned)
    checked, context = service(owned, capabilities=(EVIDENCE_REGISTER,))
    stored = read_evidence_item(owned.connection, WORKSPACE_ID, "ev-1")
    assert stored is not None
    foreign = replace(stored, evidence_id="ev-foreign", workspace_id="another-workspace")
    with pytest.raises(SemanticServiceError) as mismatch:
        checked.register_evidence(context, foreign, actor_id="principal-1")
    assert mismatch.value.code == SEMANTIC_WORKSPACE_MISMATCH
    assert "another-workspace" not in str(mismatch.value)
    with pytest.raises(SemanticServiceError) as actor_mismatch:
        checked.register_evidence(context, stored, actor_id="forged-principal")
    assert actor_mismatch.value.code == SEMANTIC_WORKSPACE_MISMATCH
    assert "forged-principal" not in str(actor_mismatch.value)


def test_manual_and_rule_observation_capabilities_are_separate(owned: Owned) -> None:
    register_evidence(owned)
    manual_service, manual_context = service(
        owned, capabilities=(OBSERVATION_CREATE_MANUAL,)
    )
    original = SemanticObservation(
        observation_id="obs-manual",
        workspace_id=WORKSPACE_ID,
        kind="fact",
        value_kind=ObservationValueKind.TEXT,
        original_form="widget",
        normalized_form="widget",
        proposed_semantic_role="concept",
        classification=Classification.INTERNAL,
        generation=ObservationGeneration.MANUAL,
        recorded_at=instant(1),
    )
    manual = ObservationBundle(
        original,
        (
            EvidenceLink(
                workspace_id=WORKSPACE_ID,
                observation_id="obs-manual",
                evidence_id="ev-1",
                role=EvidenceSupportRole.SUPPORT,
                span_id="sp-1",
            ),
        ),
    )
    manual_service.create_observation(manual_context, manual, actor_id="principal-1")
    rule = replace(
        manual,
        observation=replace(
            original,
            observation_id="obs-rule",
            generation=ObservationGeneration.DETERMINISTIC_RULE,
            rule_version="rule-v1",
        ),
        evidence_links=(
            replace(manual.evidence_links[0], observation_id="obs-rule"),
        ),
    )
    with pytest.raises(SemanticServiceError) as denied:
        manual_service.create_observation(manual_context, rule, actor_id="principal-1")
    assert denied.value.code == SEMANTIC_PERMISSION_DENIED

    rule_service, rule_context = service(owned, capabilities=(OBSERVATION_CREATE_RULE,))
    rule_service.create_observation(rule_context, rule, actor_id="principal-1")


def test_candidate_aggregation_inspection_and_conversion_stay_human_governed(
    owned: Owned,
) -> None:
    seed_model(owned)
    register_evidence(owned)
    register_observation(owned)
    checked, context = service(
        owned,
        capabilities=(CANDIDATE_AGGREGATE, CANDIDATE_READ, CANDIDATE_CONVERT),
    )
    aggregated = checked.aggregate_candidate(
        context,
        target_model_id="model-a",
        candidate_kind="ontology change",
        proposed_operation=add_concept("op-service", "concept-service", {"label": "Service"}),
        observation_ids=("obs-1",),
        created_at=instant(4),
        actor_id="principal-1",
        candidate_id="cand-service",
    )
    assert aggregated.candidate.base_version_id == "v1"
    view = checked.inspect_candidate(context, "cand-service")
    assert view.record.candidate == aggregated.candidate
    assert view.model_version_id == "v1"
    proposal = checked.convert_candidate(context, "cand-service", actor_id="principal-1")
    assert proposal.base_version_id == "v1"
    assert proposal.decision is None
    assert read_review(
        owned.connection,
        workspace_id=WORKSPACE_ID,
        change_set_id=proposal.change_set_id,
    ) is None


def test_candidate_rejection_and_reconsideration_require_separate_capabilities(
    owned: Owned,
) -> None:
    seed_model(owned)
    register_evidence(owned)
    register_observation(owned)
    checked, context = service(
        owned,
        capabilities=(CANDIDATE_AGGREGATE, CANDIDATE_REJECT, CANDIDATE_RECONSIDER),
    )
    checked.aggregate_candidate(
        context,
        target_model_id="model-a",
        candidate_kind="ontology change",
        proposed_operation=add_concept("op-reject", "concept-reject", {"label": "Reject"}),
        observation_ids=("obs-1",),
        created_at=instant(4),
        actor_id="principal-1",
        candidate_id="cand-reject",
    )
    suppression = checked.reject_candidate(
        context,
        "cand-reject",
        actor_id="principal-1",
        created_at=instant(5),
        suppression_id="sup-reject",
    )
    receipt = checked.reconsider_candidate(
        context,
        suppression,
        actor_id="principal-1",
        reason=ReconsiderationReason.NEW_EVIDENCE,
        recorded_at=instant(6),
        evidence_snapshot_digest="sha256:" + "f" * 64,
        reconsideration_id="rec-reject",
    )
    assert receipt.reason is ReconsiderationReason.NEW_EVIDENCE

    reject_only, reject_context = service(owned, capabilities=(CANDIDATE_REJECT,))
    with pytest.raises(SemanticServiceError) as denied:
        reject_only.reconsider_candidate(
            reject_context,
            suppression,
            actor_id="principal-1",
            reason=ReconsiderationReason.HUMAN_OVERRIDE,
            recorded_at=instant(7),
        )
    assert denied.value.code == SEMANTIC_PERMISSION_DENIED


def test_assertion_history_and_temporal_query_echo_axes(owned: Owned) -> None:
    register_evidence(owned)
    with semantic_governance_writer(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ) as writer:
        writer.append_assertion(assertion(), (evidence_link(),))
    checked, context = service(
        owned, capabilities=(ASSERTION_HISTORY_READ, TEMPORAL_QUERY)
    )
    history = checked.assertion_history(context)
    assert [record.assertion.assertion_id for record in history.records] == ["asn-1"]
    recorded_axis = instant(2)
    valid_axis = instant(2, TemporalProvenance.EVIDENCE_ATTESTED)
    result = checked.query_knowledge_at(
        context, recorded_at=recorded_axis, valid_at=valid_axis
    )
    assert result.resolved_workspace_id == WORKSPACE_ID
    assert result.resolved_recorded_at == recorded_axis
    assert result.resolved_valid_at == valid_axis
    assert [record.assertion.assertion_id for record in result.records] == ["asn-1"]
    assert result.effective_intervals[0].effective_from == instant(
        0, TemporalProvenance.EVIDENCE_ATTESTED
    )
