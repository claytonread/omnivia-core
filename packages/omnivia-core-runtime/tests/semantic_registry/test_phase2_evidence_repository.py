"""Acceptance tests for `storage/semantic_evidence.py` (Phase 2 evidence/observation repo).

Exercises `EvidenceObservationWriter` and its readers end to end on a migrated,
owned workspace: register/read round trips, workspace-scoped digest dedup,
atomic rollback on conflicting or partial writes, evidence-only extraction
persistence (never raw completion content), observation bundle round trips
with links/features/order, cross-workspace opacity, stale fencing, and digest
verification (clean and tampered).
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "phase3" / "runtime"))

from omnivia_core_runtime.ownership.fencing import StaleGeneration
from omnivia_core_runtime.storage.connection import StorageError, authorised
from omnivia_core_runtime.storage.migrations import materialise_phase0_baseline
from omnivia_core_runtime.storage.semantic_evidence import (
    read_evidence_by_digest,
    read_evidence_item,
    read_observation_bundle,
    semantic_evidence_writer,
    verify_evidence_observation_digests,
)
from test_application_audit_idempotency_migration import (  # type: ignore[import-not-found]
    Owned,
    bootstrap_and_migrate,
    take_ownership,
)

from omnivia_core.semantic_registry import (
    Classification,
    EvidenceExtraction,
    EvidenceItem,
    EvidenceLink,
    EvidenceLocatorScheme,
    EvidenceSource,
    EvidenceSourceKind,
    EvidenceSpan,
    EvidenceSupportRole,
    ObservationBundle,
    ObservationFeature,
    ObservationGeneration,
    ObservationStatus,
    ObservationValueKind,
    SemanticObservation,
    TemporalInstant,
    TemporalPrecision,
    TemporalProvenance,
    parse_source_time,
)

WORKSPACE_ID = "ws-p2-repo-0001"
OTHER_WORKSPACE_ID = "ws-p2-repo-0002"

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
DIGEST_D = "sha256:" + "d" * 64


def instant(seconds: int, provenance: TemporalProvenance = TemporalProvenance.STATED) -> TemporalInstant:
    return TemporalInstant(
        value=datetime.fromtimestamp(1_700_000_000 + seconds, tz=UTC),
        precision=TemporalPrecision.SECOND,
        provenance=provenance,
    )


def source(**overrides: object) -> EvidenceSource:
    values: dict[str, object] = {
        "source_id": "src-1",
        "kind": EvidenceSourceKind.DOCUMENT,
        "locator_scheme": EvidenceLocatorScheme.HTTPS,
        "locator": "https://example.test/doc",
        "version": "v1",
    }
    values.update(overrides)
    return EvidenceSource(**values)  # type: ignore[arg-type]


def span(**overrides: object) -> EvidenceSpan:
    values: dict[str, object] = {
        "span_id": "sp-1",
        "start_offset": 0,
        "end_offset": 10,
        "page": None,
        "section": None,
    }
    values.update(overrides)
    return EvidenceSpan(**values)  # type: ignore[arg-type]


def evidence_item(**overrides: object) -> EvidenceItem:
    values: dict[str, object] = {
        "evidence_id": "ev-1",
        "workspace_id": WORKSPACE_ID,
        "source": source(),
        "content_ref": "blob://ev-1",
        "content_digest": DIGEST_A,
        "integrity_digest": DIGEST_B,
        "mime_type": "text/plain",
        "classification": Classification.INTERNAL,
        "retention_class": "standard",
        "captured_at": instant(1),
        "source_time": None,
        "span": span(),
    }
    values.update(overrides)
    return EvidenceItem(**values)  # type: ignore[arg-type]


def extraction(**overrides: object) -> EvidenceExtraction:
    values: dict[str, object] = {
        "extraction_id": "ext-1",
        "workspace_id": WORKSPACE_ID,
        "evidence_id": "ev-1",
        "worker_version": "w1",
        "template_version": "t1",
        "input_digest": DIGEST_A,
        "output_digest": DIGEST_B,
        "confidence": 0.75,
        "model_version": "m1",
        "raw_completion_ref": "blob://raw-1",
    }
    values.update(overrides)
    return EvidenceExtraction(**values)  # type: ignore[arg-type]


def observation(**overrides: object) -> SemanticObservation:
    values: dict[str, object] = {
        "observation_id": "obs-1",
        "workspace_id": WORKSPACE_ID,
        "kind": "fact",
        "value_kind": ObservationValueKind.TEXT,
        "original_form": "The sky is blue",
        "normalized_form": "the sky is blue",
        "proposed_semantic_role": "attribute",
        "classification": Classification.INTERNAL,
        "generation": ObservationGeneration.MANUAL,
        "recorded_at": instant(2),
        "status": ObservationStatus.RECORDED,
        "source_time": None,
        "supersedes_observation_id": None,
        "rule_version": None,
    }
    values.update(overrides)
    return SemanticObservation(**values)  # type: ignore[arg-type]


def evidence_link(**overrides: object) -> EvidenceLink:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "observation_id": "obs-1",
        "evidence_id": "ev-1",
        "role": EvidenceSupportRole.SUPPORT,
        "span_id": "sp-1",
        "confidence": 0.9,
    }
    values.update(overrides)
    return EvidenceLink(**values)  # type: ignore[arg-type]


def feature(**overrides: object) -> ObservationFeature:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "observation_id": "obs-1",
        "feature_name": "length",
        "value": 15,
        "policy_version": "p1",
        "calculation_version": "c1",
    }
    values.update(overrides)
    return ObservationFeature(**values)  # type: ignore[arg-type]


@pytest.fixture
def owned(tmp_path: Path):
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    holder = take_ownership(path, workspace_id=WORKSPACE_ID)
    yield holder
    holder.connection.close()


def writer(holder: Owned):
    return semantic_evidence_writer(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    )


def count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


# --- evidence register/read round trip -----------------------------------------


def test_register_and_read_preserves_every_evidence_field(owned: Owned) -> None:
    item = evidence_item(source_time=instant(0))
    with writer(owned) as write:
        write.register_evidence(item)

    read = read_evidence_item(owned.connection, WORKSPACE_ID, "ev-1")
    assert read == item


def test_source_temporal_metadata_round_trips_for_evidence_and_observation(
    owned: Owned,
) -> None:
    evidence_time = parse_source_time(
        "2023-11-13T09:30:00",
        TemporalPrecision.MINUTE,
        trusted_source_timezone="Australia/Sydney",
        provenance=TemporalProvenance.EVIDENCE_ATTESTED,
    )
    observation_time = parse_source_time(
        "2023-11-13T08:15:00+10:00",
        TemporalPrecision.MINUTE,
        provenance=TemporalProvenance.EVIDENCE_ATTESTED,
    )
    item = evidence_item(
        classification=Classification.RESTRICTED,
        source_time=evidence_time,
    )
    bundle = ObservationBundle(
        observation=observation(
            classification=Classification.RESTRICTED,
            source_time=observation_time,
        ),
        evidence_links=(evidence_link(),),
    )
    with writer(owned) as write:
        write.register_evidence(item)
        write.append_observation(bundle)

    stored_item = read_evidence_item(owned.connection, WORKSPACE_ID, "ev-1")
    stored_bundle = read_observation_bundle(owned.connection, WORKSPACE_ID, "obs-1")
    assert stored_item is not None
    assert stored_item.source_time == evidence_time
    assert stored_item.source_time is not None
    assert stored_item.source_time.original_source_text == "2023-11-13T09:30:00"
    assert stored_item.source_time.source_timezone == "Australia/Sydney"
    assert stored_bundle is not None
    assert stored_bundle.observation.source_time == observation_time
    assert stored_bundle.observation.source_time is not None
    assert stored_bundle.observation.source_time.source_timezone == "+10:00"
    verify_evidence_observation_digests(owned.connection, WORKSPACE_ID)


def test_sqlite_temporal_metadata_guards_lengths_and_null_combinations(
    owned: Owned,
) -> None:
    item = evidence_item(
        source_time=parse_source_time(
            "2023-11-13T09:30:00Z", TemporalPrecision.MINUTE
        )
    )
    with writer(owned) as write:
        write.register_evidence(item)
        write.append_observation(
            ObservationBundle(
                observation=observation(source_time=item.source_time),
                evidence_links=(evidence_link(),),
            )
        )
    with authorised(owned.connection, ddl=True):
        owned.connection.execute(
            "DROP TRIGGER omnivia_guard_semantic_evidence_items_update"
        )
        owned.connection.execute(
            "DROP TRIGGER omnivia_guard_semantic_observations_update"
        )
    with authorised(owned.connection, mutations=True):
        with pytest.raises(sqlite3.IntegrityError):
            owned.connection.execute(
                "UPDATE omnivia_semantic_evidence_items "
                "SET source_time_original_text = ? WHERE workspace_id = ?",
                ("x" * 2049, WORKSPACE_ID),
            )
        with pytest.raises(sqlite3.IntegrityError):
            owned.connection.execute(
                "UPDATE omnivia_semantic_evidence_items "
                "SET source_time_original_text = NULL, source_time_timezone = 'UTC' "
                "WHERE workspace_id = ?",
                (WORKSPACE_ID,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            owned.connection.execute(
                "UPDATE omnivia_semantic_observations "
                "SET source_time_timezone = ? WHERE workspace_id = ?",
                ("x" * 256, WORKSPACE_ID),
            )


def test_register_and_read_preserves_a_missing_span_and_source_time(owned: Owned) -> None:
    item = evidence_item(span=None, source_time=None)
    with writer(owned) as write:
        write.register_evidence(item)

    read = read_evidence_item(owned.connection, WORKSPACE_ID, "ev-1")
    assert read == item
    assert read is not None
    assert read.span is None
    assert read.source_time is None


# --- workspace-scoped duplicate-digest dedup ------------------------------------


def test_duplicate_content_digest_in_the_same_workspace_returns_the_original(
    owned: Owned,
) -> None:
    original = evidence_item()
    with writer(owned) as write:
        stored = write.register_evidence(original)
    assert stored == original

    duplicate = evidence_item(
        evidence_id="ev-2",
        content_ref="blob://ev-2",
        span=span(span_id="sp-2"),
    )
    with writer(owned) as write:
        returned = write.register_evidence(duplicate)

    assert returned == original
    assert count(owned.connection, "omnivia_semantic_evidence_items") == 1
    assert count(owned.connection, "omnivia_semantic_evidence_spans") == 1
    assert read_evidence_item(owned.connection, WORKSPACE_ID, "ev-2") is None


def test_identical_digest_in_a_separate_workspace_is_accepted(
    owned: Owned, tmp_path: Path
) -> None:
    with writer(owned) as write:
        write.register_evidence(evidence_item())

    other_path = tmp_path / "other.sqlite"
    materialise_phase0_baseline(other_path)
    bootstrap_and_migrate(other_path, workspace_id=OTHER_WORKSPACE_ID)
    other = take_ownership(other_path, workspace_id=OTHER_WORKSPACE_ID)
    try:
        other_item = evidence_item(workspace_id=OTHER_WORKSPACE_ID)
        with semantic_evidence_writer(
            other.connection,
            other.identity,
            workspace_id=OTHER_WORKSPACE_ID,
            fencing_generation=other.generation,
        ) as write:
            write.register_evidence(other_item)
        assert (
            read_evidence_by_digest(other.connection, OTHER_WORKSPACE_ID, DIGEST_A)
            == other_item
        )
    finally:
        other.connection.close()


# --- conflicting source identity rolls back atomically --------------------------


def test_conflicting_source_identity_rolls_back_atomically(owned: Owned) -> None:
    with writer(owned) as write:
        write.register_evidence(evidence_item())

    conflicting = evidence_item(
        evidence_id="ev-2",
        content_digest=DIGEST_D,
        span=None,
        source=source(locator="https://example.test/other-doc"),
    )
    with pytest.raises(StorageError, match="source identity conflicts"), writer(
        owned
    ) as write:
        write.register_evidence(conflicting)

    assert count(owned.connection, "omnivia_semantic_evidence_items") == 1
    assert read_evidence_item(owned.connection, WORKSPACE_ID, "ev-2") is None


# --- extraction persists refs/digests only --------------------------------------


def test_extraction_persists_only_refs_digests_versions_confidence(owned: Owned) -> None:
    with writer(owned) as write:
        write.register_evidence(evidence_item())
        write.append_extraction(extraction(), created_at_us=1_700_000_000_000_010)

    row = owned.connection.execute(
        "SELECT extraction_id, evidence_id, worker_version, model_version, "
        "template_version, input_digest, output_digest, raw_completion_ref, "
        "confidence_ppm, schema_version FROM omnivia_semantic_evidence_extractions "
        "WHERE workspace_id = ? AND extraction_id = ?",
        (WORKSPACE_ID, "ext-1"),
    ).fetchone()
    assert row == (
        "ext-1",
        "ev-1",
        "w1",
        "m1",
        "t1",
        DIGEST_A,
        DIGEST_B,
        "blob://raw-1",
        750_000,
        "1.0.0",
    )
    columns = {
        description[0]
        for description in owned.connection.execute(
            "SELECT * FROM omnivia_semantic_evidence_extractions LIMIT 0"
        ).description
    }
    assert "raw_completion_ref" in columns
    assert not any("completion" in name and name != "raw_completion_ref" for name in columns)
    assert not any(name in {"prompt", "output_text", "raw_output"} for name in columns)


# --- observation bundle round trip -----------------------------------------------


def test_observation_bundle_round_trip_preserves_links_features_and_order(
    owned: Owned,
) -> None:
    with writer(owned) as write:
        write.register_evidence(evidence_item())
        write.register_evidence(
            evidence_item(
                evidence_id="ev-2",
                content_digest=DIGEST_B,
                content_ref="blob://ev-2",
                span=span(span_id="sp-2"),
            )
        )
        bundle = ObservationBundle(
            observation=observation(source_time=instant(0, TemporalProvenance.EVIDENCE_ATTESTED)),
            evidence_links=(
                evidence_link(role=EvidenceSupportRole.SUPPORT, span_id="sp-1"),
                evidence_link(
                    evidence_id="ev-2",
                    role=EvidenceSupportRole.CONTRADICT,
                    span_id="sp-2",
                    confidence=0.4,
                ),
            ),
            features=(
                feature(feature_name="length", value=15),
                feature(feature_name="tone", value={"score": 3, "label": "neutral"}),
            ),
        )
        write.append_observation(bundle)

    read = read_observation_bundle(owned.connection, WORKSPACE_ID, "obs-1")
    assert read is not None
    assert read.observation == bundle.observation
    assert sorted(read.evidence_links, key=lambda link: link.evidence_id) == sorted(
        bundle.evidence_links, key=lambda link: link.evidence_id
    )
    assert sorted(read.features, key=lambda f: f.feature_name) == sorted(
        bundle.features, key=lambda f: f.feature_name
    )
    assert {f.feature_name for f in read.features} == {"length", "tone"}
    supporting = [link for link in read.evidence_links if link.role is EvidenceSupportRole.SUPPORT]
    contradicting = [
        link for link in read.evidence_links if link.role is EvidenceSupportRole.CONTRADICT
    ]
    assert [link.evidence_id for link in supporting] == ["ev-1"]
    assert [link.evidence_id for link in contradicting] == ["ev-2"]


# --- multi-row failures leave no partial rows ------------------------------------


def test_observation_write_failure_leaves_no_partial_rows(owned: Owned) -> None:
    with writer(owned) as write:
        write.register_evidence(evidence_item())

    bundle = ObservationBundle(
        observation=observation(),
        evidence_links=(evidence_link(role=EvidenceSupportRole.SUPPORT, span_id="sp-1"),),
        features=(feature(),),
    )
    with pytest.raises(sqlite3.DatabaseError), writer(owned) as write:
        write.append_observation(bundle)
        # Second insert of the same observation id violates the primary key,
        # forcing a rollback of the whole fenced transaction, features included.
        write.append_observation(bundle)

    assert count(owned.connection, "omnivia_semantic_observations") == 0
    assert count(owned.connection, "omnivia_semantic_observation_evidence") == 0
    assert count(owned.connection, "omnivia_semantic_observation_features") == 0
    assert read_observation_bundle(owned.connection, WORKSPACE_ID, "obs-1") is None


# --- cross-workspace opacity ------------------------------------------------------


def test_cross_workspace_evidence_read_returns_none_without_disclosure(
    owned: Owned, tmp_path: Path
) -> None:
    other_path = tmp_path / "other.sqlite"
    materialise_phase0_baseline(other_path)
    bootstrap_and_migrate(other_path, workspace_id=OTHER_WORKSPACE_ID)
    other = take_ownership(other_path, workspace_id=OTHER_WORKSPACE_ID)
    try:
        with semantic_evidence_writer(
            other.connection,
            other.identity,
            workspace_id=OTHER_WORKSPACE_ID,
            fencing_generation=other.generation,
        ) as write:
            write.register_evidence(evidence_item(workspace_id=OTHER_WORKSPACE_ID))
    finally:
        other.connection.close()

    assert read_evidence_item(owned.connection, WORKSPACE_ID, "ev-1") is None
    assert read_evidence_by_digest(owned.connection, WORKSPACE_ID, DIGEST_A) is None


def test_cross_workspace_evidence_link_write_fails_without_disclosure(
    owned: Owned, tmp_path: Path
) -> None:
    other_path = tmp_path / "other.sqlite"
    materialise_phase0_baseline(other_path)
    bootstrap_and_migrate(other_path, workspace_id=OTHER_WORKSPACE_ID)
    other = take_ownership(other_path, workspace_id=OTHER_WORKSPACE_ID)
    try:
        with semantic_evidence_writer(
            other.connection,
            other.identity,
            workspace_id=OTHER_WORKSPACE_ID,
            fencing_generation=other.generation,
        ) as write:
            # "ev-1" exists under OTHER_WORKSPACE_ID, never under WORKSPACE_ID.
            write.register_evidence(evidence_item(workspace_id=OTHER_WORKSPACE_ID))
    finally:
        other.connection.close()

    bundle = ObservationBundle(
        observation=observation(),
        evidence_links=(evidence_link(role=EvidenceSupportRole.SUPPORT, span_id=None),),
    )
    with pytest.raises(
        sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"
    ), writer(owned) as write:
        write.append_observation(bundle)

    assert count(owned.connection, "omnivia_semantic_observations") == 0


# --- stale fencing ----------------------------------------------------------------


def test_stale_fencing_generation_fails_and_writes_nothing(owned: Owned) -> None:
    with pytest.raises(StaleGeneration), semantic_evidence_writer(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation + 1,
    ) as write:
        write.register_evidence(evidence_item())

    assert count(owned.connection, "omnivia_semantic_evidence_items") == 0


# --- digest verification: clean and tampered --------------------------------------


def test_verify_evidence_observation_digests_passes_on_clean_data(owned: Owned) -> None:
    with writer(owned) as write:
        write.register_evidence(evidence_item())
        bundle = ObservationBundle(
            observation=observation(),
            evidence_links=(evidence_link(role=EvidenceSupportRole.SUPPORT, span_id="sp-1"),),
            features=(feature(),),
        )
        write.append_observation(bundle)

    verify_evidence_observation_digests(owned.connection, WORKSPACE_ID)


def test_verify_evidence_observation_digests_detects_evidence_tampering(
    owned: Owned,
) -> None:
    with writer(owned) as write:
        write.register_evidence(evidence_item())

    # The guard trigger refuses UPDATE/DELETE on Phase 2 tables; tamper with the
    # guard removed deliberately, only to simulate corruption for this assertion.
    with authorised(owned.connection, ddl=True):
        owned.connection.execute(
            "DROP TRIGGER omnivia_guard_semantic_evidence_items_update"
        )
    with authorised(owned.connection, mutations=True):
        owned.connection.execute(
            "UPDATE omnivia_semantic_evidence_items SET mime_type = 'application/json' "
            "WHERE workspace_id = ? AND evidence_id = ?",
            (WORKSPACE_ID, "ev-1"),
        )
    owned.connection.commit()

    with pytest.raises(StorageError, match="evidence digest verification failed"):
        verify_evidence_observation_digests(owned.connection, WORKSPACE_ID)


def test_digest_verification_detects_temporal_source_metadata_tampering(
    owned: Owned,
) -> None:
    item = evidence_item(
        source_time=parse_source_time(
            "2023-11-13T09:30:00Z", TemporalPrecision.MINUTE
        )
    )
    with writer(owned) as write:
        write.register_evidence(item)
    with authorised(owned.connection, ddl=True):
        owned.connection.execute(
            "DROP TRIGGER omnivia_guard_semantic_evidence_items_update"
        )
    with authorised(owned.connection, mutations=True):
        owned.connection.execute(
            "UPDATE omnivia_semantic_evidence_items "
            "SET source_time_original_text = '2023-11-13T10:30:00Z' "
            "WHERE workspace_id = ? AND evidence_id = ?",
            (WORKSPACE_ID, "ev-1"),
        )
    owned.connection.commit()

    with pytest.raises(StorageError, match="evidence digest verification failed"):
        verify_evidence_observation_digests(owned.connection, WORKSPACE_ID)


def test_verify_evidence_observation_digests_detects_observation_tampering(
    owned: Owned,
) -> None:
    with writer(owned) as write:
        write.register_evidence(evidence_item())
        bundle = ObservationBundle(
            observation=observation(),
            evidence_links=(evidence_link(role=EvidenceSupportRole.SUPPORT, span_id="sp-1"),),
        )
        write.append_observation(bundle)

    with authorised(owned.connection, ddl=True):
        owned.connection.execute(
            "DROP TRIGGER omnivia_guard_semantic_observations_update"
        )
    with authorised(owned.connection, mutations=True):
        owned.connection.execute(
            "UPDATE omnivia_semantic_observations SET normalized_form = 'tampered' "
            "WHERE workspace_id = ? AND observation_id = ?",
            (WORKSPACE_ID, "obs-1"),
        )
    owned.connection.commit()

    with pytest.raises(StorageError, match="observation digest verification failed"):
        verify_evidence_observation_digests(owned.connection, WORKSPACE_ID)


def test_verify_evidence_observation_digests_detects_link_tampering(owned: Owned) -> None:
    with writer(owned) as write:
        write.register_evidence(evidence_item())
        bundle = ObservationBundle(
            observation=observation(),
            evidence_links=(evidence_link(role=EvidenceSupportRole.SUPPORT, span_id="sp-1"),),
        )
        write.append_observation(bundle)

    with authorised(owned.connection, ddl=True):
        owned.connection.execute(
            "DROP TRIGGER omnivia_guard_semantic_observation_evidence_update"
        )
    with authorised(owned.connection, mutations=True):
        owned.connection.execute(
            "UPDATE omnivia_semantic_observation_evidence SET confidence_ppm = 100000 "
            "WHERE workspace_id = ? AND observation_id = ?",
            (WORKSPACE_ID, "obs-1"),
        )
    owned.connection.commit()

    with pytest.raises(StorageError, match="evidence-link digest verification failed"):
        verify_evidence_observation_digests(owned.connection, WORKSPACE_ID)


def test_verify_evidence_observation_digests_detects_feature_tampering(owned: Owned) -> None:
    with writer(owned) as write:
        write.register_evidence(evidence_item())
        bundle = ObservationBundle(
            observation=observation(),
            evidence_links=(evidence_link(role=EvidenceSupportRole.SUPPORT, span_id="sp-1"),),
            features=(feature(),),
        )
        write.append_observation(bundle)

    with authorised(owned.connection, ddl=True):
        owned.connection.execute(
            "DROP TRIGGER omnivia_guard_semantic_observation_features_update"
        )
    with authorised(owned.connection, mutations=True):
        owned.connection.execute(
            "UPDATE omnivia_semantic_observation_features SET feature_json = '999' "
            "WHERE workspace_id = ? AND observation_id = ? AND feature_name = ?",
            (WORKSPACE_ID, "obs-1", "length"),
        )
    owned.connection.commit()

    with pytest.raises(StorageError, match="observation-feature digest verification failed"):
        verify_evidence_observation_digests(owned.connection, WORKSPACE_ID)
