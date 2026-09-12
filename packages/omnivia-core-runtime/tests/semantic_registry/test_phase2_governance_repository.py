"""Acceptance tests for `storage/semantic_governance.py` (Phase 2 governance repo).

Exercises `SemanticGovernanceWriter` and its readers end to end on a migrated,
owned workspace: assertion/evidence round trips across every temporal boundary
state, append-only supersession/retraction with bitemporal half-open query
behavior, candidate/contribution order and stale-base rejection, suppression/
reconsideration trigger fields and `active_suppression` termination rules,
cross-workspace and missing-parent/stale-fencing atomic failures, and
`verify_governance_digests` on clean and tampered rows.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "phase3" / "runtime"))

from omnivia_core_runtime.ownership.fencing import StaleGeneration, fenced_transaction
from omnivia_core_runtime.storage.connection import StorageError, authorised
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.storage.semantic_evidence import (
    semantic_evidence_writer,
)
from omnivia_core_runtime.storage.semantic_governance import (
    active_suppression,
    query_assertions,
    read_assertion,
    read_assertion_retractions,
    read_assertion_supersessions,
    read_candidate,
    read_suppressions,
    semantic_governance_writer,
    verify_governance_digests,
)
from test_application_audit_idempotency_migration import (  # type: ignore[import-not-found]
    Owned,
    bootstrap_and_migrate,
    insert,
    take_ownership,
)
from test_application_audit_idempotency_migration import (
    row_for as audit_row_for,
)
from test_migration import (  # type: ignore[import-not-found]
    model_row,
    pointer_row,
    version_row,
)

from omnivia_core.semantic_registry import (
    AssertionEvidence,
    AssertionRetraction,
    AssertionSupersession,
    CandidateBand,
    CandidateContribution,
    CandidateReconsideration,
    CandidateRiskBand,
    CandidateState,
    CandidateSuppression,
    Classification,
    ContributionRole,
    EndBoundaryState,
    EvidenceItem,
    EvidenceLink,
    EvidenceLocatorScheme,
    EvidenceSource,
    EvidenceSourceKind,
    EvidenceSpan,
    EvidenceSupportRole,
    KnowledgeAssertion,
    KnowledgeObjectKind,
    ObservationBundle,
    ObservationGeneration,
    ObservationStatus,
    ObservationValueKind,
    ReconsiderationReason,
    SemanticCandidate,
    SemanticObservation,
    TemporalInstant,
    TemporalPrecision,
    TemporalProvenance,
    add_concept,
)

WORKSPACE_ID = "ws-p2-gov-0001"
OTHER_WORKSPACE_ID = "ws-p2-gov-0002"

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def instant(seconds: int, provenance: TemporalProvenance = TemporalProvenance.STATED) -> TemporalInstant:
    return TemporalInstant(
        value=datetime.fromtimestamp(1_700_000_000 + seconds, tz=UTC),
        precision=TemporalPrecision.SECOND,
        provenance=provenance,
    )


def assertion(**overrides: object) -> KnowledgeAssertion:
    values: dict[str, object] = {
        "assertion_id": "asn-1",
        "workspace_id": WORKSPACE_ID,
        "subject_id": "subj-1",
        "predicate_element_id": "pred-1",
        "model_version_id": "v1",
        "object_kind": KnowledgeObjectKind.LITERAL,
        "confidence": 0.8,
        "attested_from": instant(0, TemporalProvenance.EVIDENCE_ATTESTED),
        "valid_to_state": EndBoundaryState.OPEN,
        "recorded_at": instant(1),
        "classification": Classification.INTERNAL,
        "literal_value": {"v": 1},
    }
    values.update(overrides)
    return KnowledgeAssertion(**values)  # type: ignore[arg-type]


def evidence_link(**overrides: object) -> AssertionEvidence:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "assertion_id": "asn-1",
        "evidence_id": "ev-1",
        "role": EvidenceSupportRole.SUPPORT,
        "confidence": 0.9,
        "span_id": "sp-1",
    }
    values.update(overrides)
    return AssertionEvidence(**values)  # type: ignore[arg-type]


def operation(**overrides: object) -> object:
    kwargs: dict[str, object] = {"after": {"label": "Widget"}}
    kwargs.update(overrides)
    return add_concept("op-1", "prop-widget", **kwargs)  # type: ignore[arg-type]


def candidate(**overrides: object) -> SemanticCandidate:
    values: dict[str, object] = {
        "candidate_id": "cand-1",
        "workspace_id": WORKSPACE_ID,
        "candidate_kind": "assertion",
        "target_model_id": "model-a",
        "proposed_operation": operation(),
        "support_band": CandidateBand.MEDIUM,
        "novelty_band": CandidateBand.LOW,
        "risk_band": CandidateRiskBand.STANDARD,
        "state": CandidateState.DRAFT,
        "aggregation_version": "agg-1",
        "normalization_version": "norm-1",
        "base_version_id": "v1",
        "evidence_snapshot_digest": DIGEST_A,
        "created_at": instant(4),
    }
    values.update(overrides)
    return SemanticCandidate(**values)  # type: ignore[arg-type]


def contribution(**overrides: object) -> CandidateContribution:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "candidate_id": "cand-1",
        "observation_id": "obs-1",
        "role": ContributionRole.SUPPORT,
        "weight": 100,
        "observation_digest": DIGEST_A,
    }
    values.update(overrides)
    return CandidateContribution(**values)  # type: ignore[arg-type]


def suppression(**overrides: object) -> CandidateSuppression:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "suppression_id": "sup-1",
        "equivalence_signature": DIGEST_B,
        "rejection_ref": "cand-1",
        "suppression_rule_version": "sup-rule-1",
        "created_at": instant(5),
        "evidence_snapshot_digest": DIGEST_A,
        "aggregation_version": "agg-1",
    }
    values.update(overrides)
    return CandidateSuppression(**values)  # type: ignore[arg-type]


def reconsideration(**overrides: object) -> CandidateReconsideration:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "reconsideration_id": "rec-1",
        "suppression_id": "sup-1",
        "reason": ReconsiderationReason.NEW_EVIDENCE,
        "recorded_at": instant(6),
        "previous_evidence_digest": DIGEST_A,
        "new_evidence_digest": DIGEST_B,
    }
    values.update(overrides)
    return CandidateReconsideration(**values)  # type: ignore[arg-type]


@pytest.fixture
def owned(tmp_path: Path):
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    holder = take_ownership(path, workspace_id=WORKSPACE_ID)
    yield holder
    holder.connection.close()


def writer(holder: Owned):
    return semantic_governance_writer(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    )


def register_evidence(holder: Owned, evidence_id: str = "ev-1") -> None:
    with semantic_evidence_writer(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ) as write:
        write.register_evidence(
            EvidenceItem(
                evidence_id=evidence_id,
                workspace_id=WORKSPACE_ID,
                source=EvidenceSource(
                    source_id="src-1",
                    kind=EvidenceSourceKind.DOCUMENT,
                    locator_scheme=EvidenceLocatorScheme.HTTPS,
                    locator="https://example.test/doc",
                    version="v1",
                ),
                content_ref=f"blob://{evidence_id}",
                content_digest="sha256:" + sha256(evidence_id.encode()).hexdigest(),
                integrity_digest=DIGEST_B,
                mime_type="text/plain",
                classification=Classification.INTERNAL,
                retention_class="standard",
                captured_at=instant(0),
                source_time=None,
                span=EvidenceSpan(
                    span_id="sp-1", start_offset=0, end_offset=10, page=None, section=None
                ),
            )
        )


def register_observation(holder: Owned, observation_id: str = "obs-1") -> None:
    with semantic_evidence_writer(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ) as write:
        write.append_observation(
                ObservationBundle(
                    observation=SemanticObservation(
                    observation_id=observation_id,
                    workspace_id=WORKSPACE_ID,
                    kind="fact",
                    value_kind=ObservationValueKind.TEXT,
                    original_form="widget",
                    normalized_form="widget",
                    proposed_semantic_role="attribute",
                    classification=Classification.INTERNAL,
                    generation=ObservationGeneration.MANUAL,
                    recorded_at=instant(3),
                    status=ObservationStatus.RECORDED,
                    source_time=None,
                    supersedes_observation_id=None,
                        rule_version=None,
                    ),
                    evidence_links=(
                        EvidenceLink(
                            workspace_id=WORKSPACE_ID,
                            observation_id=observation_id,
                            evidence_id="ev-1",
                            role=EvidenceSupportRole.SUPPORT,
                            span_id="sp-1",
                            confidence=0.9,
                        ),
                    ),
                )
        )


def seed_model(holder: Owned, *, activated: bool = True) -> None:
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        insert(
            holder.connection,
            "omnivia_application_audit_events",
            audit_row_for(
                "omnivia_application_audit_events",
                audit_ref="aud-p2-gov-0001",
                workspace_id=WORKSPACE_ID,
            ),
        )
        insert(holder.connection, "omnivia_semantic_models", model_row(workspace_id=WORKSPACE_ID))
        insert(
            holder.connection,
            "omnivia_semantic_current_pointers",
            pointer_row(workspace_id=WORKSPACE_ID),
        )
        if activated:
            insert(
                holder.connection,
                "omnivia_semantic_model_versions",
                version_row(workspace_id=WORKSPACE_ID, content_digest=DIGEST_A),
            )
            insert(
                holder.connection,
                "omnivia_semantic_version_activations",
                {
                    "workspace_id": WORKSPACE_ID,
                    "model_id": "model-a",
                    "activation_sequence": 0,
                    "version_id": "v1",
                    "previous_version_id": None,
                    "generation": 1,
                    "activated_at_us": 1_700_000_000_000_002,
                    "audit_ref": "aud-p2-gov-0001",
                },
            )


def count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


# --- assertion + evidence round trip: temporal boundary states -----------------


def test_stated_boundary_assertion_and_evidence_round_trip(owned: Owned) -> None:
    register_evidence(owned)
    stated = assertion(
        valid_to_state=EndBoundaryState.STATED,
        valid_to=instant(10),
        valid_from=instant(0),
    )
    support = evidence_link(role=EvidenceSupportRole.SUPPORT)
    contradict = evidence_link(evidence_id="ev-1", role=EvidenceSupportRole.CONTRADICT, confidence=0.3)
    with writer(owned) as write:
        write.append_assertion(stated, [support, contradict])

    record = read_assertion(owned.connection, WORKSPACE_ID, "asn-1")
    assert record is not None
    assert record.assertion == stated
    assert {link.role for link in record.evidence} == {
        EvidenceSupportRole.SUPPORT,
        EvidenceSupportRole.CONTRADICT,
    }


def test_unknown_boundary_assertion_round_trip(owned: Owned) -> None:
    register_evidence(owned)
    unknown = assertion(
        valid_to_state=EndBoundaryState.UNKNOWN,
        attested_to=instant(10, TemporalProvenance.EVIDENCE_ATTESTED),
    )
    with writer(owned) as write:
        write.append_assertion(unknown, [])

    record = read_assertion(owned.connection, WORKSPACE_ID, "asn-1")
    assert record is not None
    assert record.assertion == unknown


def test_open_boundary_assertion_round_trip(owned: Owned) -> None:
    open_ended = assertion()
    with writer(owned) as write:
        write.append_assertion(open_ended, [])

    record = read_assertion(owned.connection, WORKSPACE_ID, "asn-1")
    assert record is not None
    assert record.assertion == open_ended
    assert record.evidence == ()


# --- append-only supersession/retraction and bitemporal half-open query --------


def test_supersession_round_trip_without_predecessor_update(owned: Owned) -> None:
    prior = assertion()
    successor = assertion(assertion_id="asn-2", recorded_at=instant(2))
    with writer(owned) as write:
        write.append_assertion(prior, [])
        write.append_assertion(successor, [])
        write.append_supersession(
            AssertionSupersession(
                workspace_id=WORKSPACE_ID,
                supersession_id="ssn-1",
                prior_assertion_id="asn-1",
                successor_assertion_id="asn-2",
                reason_code="correction",
                decision_id="dec-1",
                recorded_at=instant(3),
            )
        )

    supersessions = read_assertion_supersessions(owned.connection, WORKSPACE_ID)
    assert len(supersessions) == 1
    assert supersessions[0].prior_assertion_id == "asn-1"
    # The prior assertion row itself is never mutated by a supersession.
    prior_record = read_assertion(owned.connection, WORKSPACE_ID, "asn-1")
    assert prior_record is not None
    assert prior_record.assertion == prior


def test_retraction_round_trip(owned: Owned) -> None:
    with writer(owned) as write:
        write.append_assertion(assertion(), [])
        write.append_retraction(
            AssertionRetraction(
                workspace_id=WORKSPACE_ID,
                retraction_id="ret-1",
                assertion_id="asn-1",
                retracted_at=instant(9),
                reason_code="correction",
                policy_version="pol-1",
                actor_principal_id="principal-1",
            )
        )

    retractions = read_assertion_retractions(owned.connection, WORKSPACE_ID)
    assert len(retractions) == 1
    assert retractions[0].assertion_id == "asn-1"


def test_query_assertions_half_open_recorded_and_valid_boundaries(owned: Owned) -> None:
    stated = assertion(
        recorded_at=instant(0),
        recorded_until=instant(20),
        valid_from=instant(0),
        valid_to_state=EndBoundaryState.STATED,
        valid_to=instant(10),
    )
    with writer(owned) as write:
        write.append_assertion(stated, [])

    # recorded_at boundary is inclusive-open: recorded_at_us<=t.
    assert len(
        query_assertions(owned.connection, WORKSPACE_ID, recorded_at=instant(0), valid_at=instant(5))
    ) == 1
    # recorded_until boundary is exclusive: recorded_until_us>t must fail at t==until.
    assert len(
        query_assertions(owned.connection, WORKSPACE_ID, recorded_at=instant(20), valid_at=instant(5))
    ) == 0
    # valid_from boundary is inclusive.
    assert len(
        query_assertions(owned.connection, WORKSPACE_ID, recorded_at=instant(1), valid_at=instant(0))
    ) == 1
    # valid_to (stated) boundary is exclusive: valid_to_us>t must fail at t==valid_to.
    assert len(
        query_assertions(owned.connection, WORKSPACE_ID, recorded_at=instant(1), valid_at=instant(10))
    ) == 0
    assert len(
        query_assertions(owned.connection, WORKSPACE_ID, recorded_at=instant(1), valid_at=instant(9))
    ) == 1


# --- candidate/contribution order and stale base -------------------------------


def test_candidate_contribution_round_trip_preserves_order_and_digests(owned: Owned) -> None:
    seed_model(owned)
    register_evidence(owned)
    register_observation(owned, "obs-1")
    register_observation(owned, "obs-2")
    support = contribution(observation_id="obs-1", role=ContributionRole.SUPPORT)
    contradict = contribution(observation_id="obs-2", role=ContributionRole.CONTRADICT, observation_digest=DIGEST_B)
    with writer(owned) as write:
        write.append_candidate(candidate(), [contradict, support])

    record = read_candidate(owned.connection, WORKSPACE_ID, "cand-1")
    assert record is not None
    assert record.candidate == candidate()
    assert [c.observation_id for c in record.contributions] == ["obs-1", "obs-2"]


def test_candidate_insert_refuses_stale_base_and_rolls_back_contributions(
    owned: Owned,
) -> None:
    seed_model(owned)
    register_evidence(owned)
    register_observation(owned)
    # Advance the pointer past "v1" so the candidate's base_version_id is stale.
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        insert(
            owned.connection,
            "omnivia_semantic_model_versions",
            {
                "workspace_id": WORKSPACE_ID,
                "model_id": "model-a",
                "version_id": "v2",
                "label": "1.0.1",
                "sequence": 1,
                "content_digest": DIGEST_B,
                "content_json": '{"a":2}',
                "created_at_us": 1_700_000_000_000_010,
            },
        )
        insert(
            owned.connection,
            "omnivia_semantic_version_activations",
            {
                "workspace_id": WORKSPACE_ID,
                "model_id": "model-a",
                "activation_sequence": 1,
                "version_id": "v2",
                "previous_version_id": "v1",
                "generation": 2,
                "activated_at_us": 1_700_000_000_000_011,
                "audit_ref": "aud-p2-gov-0001",
            },
        )

    with pytest.raises(StorageError, match="stale"), writer(owned) as write:
        write.append_candidate(candidate(), [contribution()])

    assert count(owned.connection, "omnivia_semantic_candidates") == 0
    assert count(owned.connection, "omnivia_semantic_candidate_contributions") == 0


# --- suppression/reconsideration and active_suppression termination -----------


def test_suppression_and_reconsideration_persist_exact_trigger_fields(
    owned: Owned,
) -> None:
    seed_model(owned)
    register_evidence(owned)
    register_observation(owned)
    with writer(owned) as write:
        write.append_candidate(candidate(), [])
        write.append_suppression(suppression())
        write.append_reconsideration(reconsideration())

    stored = read_suppressions(owned.connection, WORKSPACE_ID, DIGEST_B)
    assert len(stored) == 1
    assert stored[0] == suppression()


def test_active_suppression_ends_only_for_expiry_evidence_or_rule_change(
    owned: Owned,
) -> None:
    seed_model(owned)
    register_evidence(owned)
    register_observation(owned)
    with writer(owned) as write:
        write.append_candidate(candidate(), [])
        write.append_suppression(suppression(expires_at=instant(100)))

    # Still active: same snapshot, same rule, before expiry.
    assert active_suppression(
        owned.connection,
        WORKSPACE_ID,
        DIGEST_B,
        at=instant(50),
        evidence_snapshot_digest=DIGEST_A,
        aggregation_version="agg-1",
    ) is not None

    # Expired.
    assert active_suppression(
        owned.connection,
        WORKSPACE_ID,
        DIGEST_B,
        at=instant(100),
        evidence_snapshot_digest=DIGEST_A,
        aggregation_version="agg-1",
    ) is None

    # New evidence.
    assert active_suppression(
        owned.connection,
        WORKSPACE_ID,
        DIGEST_B,
        at=instant(50),
        evidence_snapshot_digest="sha256:" + "9" * 64,
        aggregation_version="agg-1",
    ) is None

    # Rule version changed.
    assert active_suppression(
        owned.connection,
        WORKSPACE_ID,
        DIGEST_B,
        at=instant(50),
        evidence_snapshot_digest=DIGEST_A,
        aggregation_version="agg-2",
    ) is None


# --- cross-workspace, missing parents, and stale fencing fail atomically -------


def test_cross_workspace_assertion_is_refused(owned: Owned) -> None:
    with pytest.raises(StorageError, match="workspace"), writer(owned) as write:
        write.append_assertion(assertion(workspace_id=OTHER_WORKSPACE_ID), [])
    assert count(owned.connection, "omnivia_semantic_assertions") == 0


def test_missing_evidence_parent_fails_atomically(owned: Owned) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"), writer(
        owned
    ) as write:
        write.append_assertion(assertion(), [evidence_link()])
    assert count(owned.connection, "omnivia_semantic_assertions") == 0
    assert count(owned.connection, "omnivia_semantic_assertion_evidence") == 0


def test_missing_observation_parent_fails_atomically(owned: Owned) -> None:
    seed_model(owned)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"), writer(
        owned
    ) as write:
        write.append_candidate(candidate(), [contribution()])
    assert count(owned.connection, "omnivia_semantic_candidates") == 0
    assert count(owned.connection, "omnivia_semantic_candidate_contributions") == 0


def test_missing_model_parent_fails_atomically(owned: Owned) -> None:
    with pytest.raises(StorageError, match="stale"), writer(owned) as write:
        write.append_candidate(candidate(), [])
    assert count(owned.connection, "omnivia_semantic_candidates") == 0


def test_stale_fencing_generation_fails_and_writes_nothing(owned: Owned) -> None:
    with pytest.raises(StaleGeneration), semantic_governance_writer(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation + 1,
    ) as write:
        write.append_assertion(assertion(), [])
    assert count(owned.connection, "omnivia_semantic_assertions") == 0


# --- verify_governance_digests: clean and tampered ------------------------------


def test_verify_governance_digests_passes_on_clean_data(owned: Owned) -> None:
    seed_model(owned)
    register_evidence(owned)
    register_observation(owned)
    with writer(owned) as write:
        write.append_assertion(assertion(), [evidence_link()])
        write.append_candidate(candidate(), [contribution()])
        write.append_suppression(suppression())

    verify_governance_digests(owned.connection, WORKSPACE_ID)


def test_verify_governance_digests_detects_assertion_tampering(owned: Owned) -> None:
    with writer(owned) as write:
        write.append_assertion(assertion(), [])

    with authorised(owned.connection, ddl=True):
        owned.connection.execute("DROP TRIGGER omnivia_guard_semantic_assertions_update")
    with authorised(owned.connection, mutations=True):
        owned.connection.execute(
            "UPDATE omnivia_semantic_assertions SET confidence_ppm = 100000 "
            "WHERE workspace_id = ? AND assertion_id = ?",
            (WORKSPACE_ID, "asn-1"),
        )
    owned.connection.commit()

    with pytest.raises(StorageError, match="assertion digest verification failed"):
        verify_governance_digests(owned.connection, WORKSPACE_ID)


def test_verify_governance_digests_detects_candidate_tampering(owned: Owned) -> None:
    seed_model(owned)
    with writer(owned) as write:
        write.append_candidate(candidate(), [])

    with authorised(owned.connection, ddl=True):
        owned.connection.execute("DROP TRIGGER omnivia_guard_semantic_candidates_update")
    with authorised(owned.connection, mutations=True):
        owned.connection.execute(
            "UPDATE omnivia_semantic_candidates SET candidate_kind = 'tampered' "
            "WHERE workspace_id = ? AND candidate_id = ?",
            (WORKSPACE_ID, "cand-1"),
        )
    owned.connection.commit()

    with pytest.raises(StorageError, match="candidate digest verification failed"):
        verify_governance_digests(owned.connection, WORKSPACE_ID)


def test_verify_governance_digests_detects_suppression_tampering(owned: Owned) -> None:
    seed_model(owned)
    with writer(owned) as write:
        write.append_candidate(candidate(), [])
        write.append_suppression(suppression())

    with authorised(owned.connection, ddl=True):
        owned.connection.execute(
            "DROP TRIGGER omnivia_guard_semantic_candidate_suppressions_update"
        )
    with authorised(owned.connection, mutations=True):
        owned.connection.execute(
            "UPDATE omnivia_semantic_candidate_suppressions SET rejection_ref = 'tampered' "
            "WHERE workspace_id = ? AND suppression_id = ?",
            (WORKSPACE_ID, "sup-1"),
        )
    owned.connection.commit()

    with pytest.raises(StorageError, match="suppression digest verification failed"):
        verify_governance_digests(owned.connection, WORKSPACE_ID)
