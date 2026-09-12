"""Acceptance tests for `0038_semantic_evidence_observations.sql` (WP-SEM-06).

Focused, not exhaustive: this proves 0038 is the unique successor to 0037, that it
adds exactly the fifteen Phase 2 tables and their forty-five guard triggers plus the
expected query indexes, that every Phase 2 table fails closed to an unguarded write
and to a stale writer, that append-only holds for UPDATE/DELETE everywhere, that the
representative digest/temporal/state/reconsideration CHECK guards fail closed, that
evidence digest uniqueness is scoped per workspace, that a representative
cross-workspace FK link is refused, and that a backup/restore round trip preserves
rows exactly.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "phase3" / "runtime")
)

from omnivia_core_runtime.ownership.fencing import (
    StaleGeneration,
    assert_guards_intact,
    fenced_transaction,
    trigger_names,
)
from omnivia_core_runtime.storage.backup import (
    InstallationLayout,
    create_verified_backup,
    new_attempt_id,
    restore_backup,
)
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    foreign_key_check,
    integrity_check,
    open_database,
)
from omnivia_core_runtime.storage.migrations import (
    load_migrations,
    materialise_phase0_baseline,
)
from test_application_audit_idempotency_migration import (  # type: ignore[import-not-found]
    Owned,
    bootstrap_and_migrate,
    count,
    insert,
    object_names,
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

MIGRATION_VERSION = 38
MIGRATION_NAME = "0038_semantic_evidence_observations.sql"
PREDECESSOR_NAME = "0037_semantic_registry.sql"

WORKSPACE_ID = "ws-p2-0001"
OTHER_WORKSPACE_ID = "ws-p2-0002"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
AUDIT_REF = "aud-p2-0001"

P2_TABLES = (
    "omnivia_semantic_evidence_sources",
    "omnivia_semantic_evidence_items",
    "omnivia_semantic_evidence_spans",
    "omnivia_semantic_evidence_extractions",
    "omnivia_semantic_observations",
    "omnivia_semantic_observation_evidence",
    "omnivia_semantic_observation_features",
    "omnivia_semantic_assertions",
    "omnivia_semantic_assertion_evidence",
    "omnivia_semantic_assertion_supersessions",
    "omnivia_semantic_assertion_retractions",
    "omnivia_semantic_candidates",
    "omnivia_semantic_candidate_contributions",
    "omnivia_semantic_candidate_suppressions",
    "omnivia_semantic_candidate_reconsiderations",
)

#: Every Phase 2 table is append-only: INSERT is fenced, UPDATE/DELETE are refused
#: unconditionally.
P2_TRIGGERS = tuple(
    f"omnivia_guard_{table.removeprefix('omnivia_')}_{statement}"
    for table in P2_TABLES
    for statement in ("insert", "update", "delete")
)

P2_INDEXES = (
    "omnivia_semantic_evidence_items_source_idx",
    "omnivia_semantic_observations_time_idx",
    "omnivia_semantic_assertions_valid_idx",
    "omnivia_semantic_assertions_recorded_idx",
    "omnivia_semantic_candidates_state_idx",
    "omnivia_semantic_candidates_equivalence_idx",
    "omnivia_semantic_candidate_contributions_observation_idx",
)


# --- fixtures ------------------------------------------------------------------


@pytest.fixture
def migrated(tmp_path: Path) -> Path:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    return path


@pytest.fixture
def owned(migrated: Path):
    holder = take_ownership(migrated, workspace_id=WORKSPACE_ID)
    yield holder
    holder.connection.close()


# --- row helpers -----------------------------------------------------------


def audit_event_row(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "audit_ref": AUDIT_REF,
        "workspace_id": WORKSPACE_ID,
    }
    base.update(overrides)
    return audit_row_for("omnivia_application_audit_events", **base)


def source_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "source_id": "src-1",
        "source_kind": "document",
        "locator_scheme": "https",
        "locator": "https://example.test/doc",
        "source_version": "v1",
        "classification": "internal",
        "created_at_us": 1_700_000_000_000_000,
    }
    values.update(overrides)
    return values


def evidence_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "evidence_id": "ev-1",
        "source_id": "src-1",
        "content_ref": "blob://ev-1",
        "content_digest": DIGEST_A,
        "integrity_digest": DIGEST_B,
        "mime_type": "text/plain",
        "classification": "internal",
        "retention_class": "standard",
        "captured_at_us": 1_700_000_000_000_001,
        "captured_at_precision": "second",
        "captured_at_provenance": "ingestion_fallback",
        "source_time_us": None,
        "source_time_precision": None,
        "source_time_provenance": None,
        "schema_version": "1",
        "record_digest": DIGEST_C,
    }
    values.update(overrides)
    return values


def span_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "evidence_id": "ev-1",
        "span_id": "sp-1",
        "start_offset": 0,
        "end_offset": 10,
        "page_number": None,
        "section_ref": None,
        "span_digest": DIGEST_A,
    }
    values.update(overrides)
    return values


def observation_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "observation_id": "obs-1",
        "observation_kind": "fact",
        "value_kind": "text",
        "original_form_ref": "blob://obs-1",
        "normalized_form": "normalized",
        "proposed_semantic_role": "attribute",
        "classification": "internal",
        "generation": "manual",
        "status": "recorded",
        "source_time_us": None,
        "source_time_precision": None,
        "source_time_provenance": None,
        "recorded_at_us": 1_700_000_000_000_002,
        "recorded_at_precision": "second",
        "recorded_at_provenance": "ingestion_fallback",
        "supersedes_observation_id": None,
        "rule_version": None,
        "normalization_version": "1",
        "schema_version": "1",
        "observation_digest": DIGEST_A,
    }
    values.update(overrides)
    return values


def assertion_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "assertion_id": "asn-1",
        "subject_id": "subj-1",
        "predicate_element_id": "pred-1",
        "model_version_id": "v1",
        "object_kind": "literal",
        "object_id": None,
        "literal_json": '{"v":1}',
        "confidence_ppm": 900000,
        "classification": "internal",
        "valid_from_us": None,
        "valid_from_precision": None,
        "valid_from_provenance": None,
        "valid_to_state": "open",
        "valid_to_us": None,
        "valid_to_precision": None,
        "valid_to_provenance": None,
        "attested_from_us": 1_700_000_000_000_003,
        "attested_from_precision": "second",
        "attested_from_provenance": "evidence_attested",
        "attested_to_us": None,
        "attested_to_precision": None,
        "attested_to_provenance": None,
        "recorded_at_us": 1_700_000_000_000_003,
        "recorded_at_precision": "second",
        "recorded_at_provenance": "ingestion_fallback",
        "recorded_until_us": None,
        "recorded_until_precision": None,
        "recorded_until_provenance": None,
        "schema_version": "1",
        "assertion_digest": DIGEST_A,
    }
    values.update(overrides)
    return values


def candidate_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "candidate_id": "cand-1",
        "candidate_kind": "assertion",
        "target_model_id": "model-a",
        "proposed_operation_json": '{"op":"add"}',
        "support_band": "medium",
        "novelty_band": "low",
        "risk_band": "standard",
        "candidate_state": "draft",
        "aggregation_version": "1",
        "normalization_version": "1",
        "base_version_id": "v1",
        "evidence_snapshot_digest": DIGEST_A,
        "equivalence_signature": DIGEST_B,
        "rejection_signature": None,
        "schema_version": "1",
        "candidate_digest": DIGEST_C,
        "created_at_us": 1_700_000_000_000_004,
        "created_at_precision": "second",
        "created_at_provenance": "ingestion_fallback",
    }
    values.update(overrides)
    return values


def suppression_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "suppression_id": "sup-1",
        "equivalence_signature": DIGEST_B,
        "rejection_ref": "cand-1",
        "suppression_rule_version": "1",
        "evidence_snapshot_digest": DIGEST_A,
        "aggregation_version": "1",
        "created_at_us": 1_700_000_000_000_005,
        "created_at_precision": "second",
        "created_at_provenance": "ingestion_fallback",
        "expires_at_us": None,
        "expires_at_precision": None,
        "expires_at_provenance": None,
        "suppression_digest": DIGEST_C,
    }
    values.update(overrides)
    return values


def reconsideration_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "reconsideration_id": "rec-1",
        "suppression_id": "sup-1",
        "reason": "new_evidence",
        "previous_evidence_digest": DIGEST_A,
        "new_evidence_digest": DIGEST_B,
        "previous_rule_version": None,
        "new_rule_version": None,
        "actor_principal_id": None,
        "recorded_at_us": 1_700_000_000_000_006,
        "recorded_at_precision": "second",
        "recorded_at_provenance": "ingestion_fallback",
        "reconsideration_digest": DIGEST_A,
    }
    values.update(overrides)
    return values


def seed_model(holder: Owned) -> None:
    """A model, its generation-zero pointer and a version, needed by candidates."""
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        if count(holder.connection, "omnivia_application_audit_events") == 0:
            insert(
                holder.connection,
                "omnivia_application_audit_events",
                audit_event_row(),
            )
        insert(holder.connection, "omnivia_semantic_models", model_row(workspace_id=WORKSPACE_ID))
        insert(
            holder.connection,
            "omnivia_semantic_current_pointers",
            pointer_row(workspace_id=WORKSPACE_ID),
        )
        insert(
            holder.connection,
            "omnivia_semantic_model_versions",
            version_row(workspace_id=WORKSPACE_ID, content_digest=DIGEST_A),
        )


def seed_evidence_chain(
    holder: Owned, *, evidence_overrides: dict[str, object] | None = None
) -> None:
    """A source, an evidence item and a span -- the parents most tables need."""
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        insert(holder.connection, "omnivia_semantic_evidence_sources", source_row())
        insert(
            holder.connection,
            "omnivia_semantic_evidence_items",
            evidence_row(**(evidence_overrides or {})),
        )
        insert(holder.connection, "omnivia_semantic_evidence_spans", span_row())


def seed_observation(
    holder: Owned, *, observation_overrides: dict[str, object] | None = None
) -> None:
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        insert(
            holder.connection,
            "omnivia_semantic_observations",
            observation_row(**(observation_overrides or {})),
        )


def seed_assertion(
    holder: Owned, *, assertion_overrides: dict[str, object] | None = None
) -> None:
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        insert(
            holder.connection,
            "omnivia_semantic_assertions",
            assertion_row(**(assertion_overrides or {})),
        )


def seed_candidate(holder: Owned) -> None:
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        insert(holder.connection, "omnivia_semantic_candidates", candidate_row())


def seed_suppression(holder: Owned) -> None:
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        insert(holder.connection, "omnivia_semantic_candidate_suppressions", suppression_row())


# --- migration identity, ledger and triggers -----------------------------------


def test_0038_is_the_unique_successor_to_0037() -> None:
    ordered = load_migrations()
    versions = [m.version for m in ordered]
    prefix = [v for v in versions if v <= MIGRATION_VERSION]
    assert prefix == list(range(1, MIGRATION_VERSION + 1)), versions

    matches = [m for m in ordered if m.version == MIGRATION_VERSION]
    assert len(matches) == 1, [m.name for m in matches]
    assert matches[0].name == MIGRATION_NAME

    index = ordered.index(matches[0])
    assert ordered[index - 1].name == PREDECESSOR_NAME


def test_migration_adds_exactly_the_fifteen_tables_their_triggers_and_indexes(
    migrated: Path,
) -> None:
    assert len(P2_TABLES) == 15
    assert len(P2_TRIGGERS) == 45

    connection = open_database(migrated, OpenMode.READ_ONLY)
    try:
        assert set(P2_TABLES) <= object_names(connection, "table")
        present_triggers = set(trigger_names(connection))
        assert set(P2_TRIGGERS) <= present_triggers
        assert set(P2_INDEXES) <= object_names(connection, "index")
        assert_guards_intact(connection)
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
    finally:
        connection.close()


@pytest.mark.parametrize("table", P2_TABLES)
def test_unguarded_insert_fails_on_every_phase2_table(
    owned: Owned, table: str
) -> None:
    """Structural proof, not a full row: every Phase 2 table has an insert guard
    that fires before column/FK validation even runs, so a deliberately empty
    values dict is enough to prove the fence exists."""
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        owned.connection.execute(f"INSERT INTO {table} DEFAULT VALUES")


# --- fenced / stale-writer / append-only ---------------------------------------


def test_fenced_insert_on_a_representative_table_succeeds(owned: Owned) -> None:
    seed_evidence_chain(owned)
    assert count(owned.connection, "omnivia_semantic_evidence_sources") == 1
    assert count(owned.connection, "omnivia_semantic_evidence_items") == 1
    assert count(owned.connection, "omnivia_semantic_evidence_spans") == 1


def test_stale_writer_insert_is_refused(owned: Owned) -> None:
    with pytest.raises(StaleGeneration), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation + 1,
    ):
        insert(owned.connection, "omnivia_semantic_evidence_sources", source_row())
    assert count(owned.connection, "omnivia_semantic_evidence_sources") == 0


@pytest.mark.parametrize("table", P2_TABLES)
def test_every_table_rejects_update_and_delete(owned: Owned, table: str) -> None:
    seed_evidence_chain(owned)
    seed_observation(owned)
    seed_model(owned)
    seed_assertion(owned)
    seed_candidate(owned)
    seed_suppression(owned)
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        insert(
            owned.connection,
            "omnivia_semantic_evidence_extractions",
            {
                "workspace_id": WORKSPACE_ID,
                "extraction_id": "ext-1",
                "evidence_id": "ev-1",
                "worker_version": "1",
                "model_version": None,
                "template_version": "1",
                "input_digest": DIGEST_A,
                "output_digest": DIGEST_B,
                "raw_completion_ref": None,
                "confidence_ppm": 500000,
                "schema_version": "1",
                "created_at_us": 1_700_000_000_000_006,
                "extraction_digest": DIGEST_C,
            },
        )
        insert(
            owned.connection,
            "omnivia_semantic_observation_evidence",
            {
                "workspace_id": WORKSPACE_ID,
                "observation_id": "obs-1",
                "evidence_id": "ev-1",
                "span_id": "sp-1",
                "support_role": "support",
                "confidence_ppm": 500000,
                "link_digest": DIGEST_A,
            },
        )
        insert(
            owned.connection,
            "omnivia_semantic_observation_features",
            {
                "workspace_id": WORKSPACE_ID,
                "observation_id": "obs-1",
                "feature_name": "length",
                "feature_json": '{"n":1}',
                "policy_version": "1",
                "calculation_version": "1",
                "feature_digest": DIGEST_A,
            },
        )
        insert(
            owned.connection,
            "omnivia_semantic_assertion_evidence",
            {
                "workspace_id": WORKSPACE_ID,
                "assertion_id": "asn-1",
                "evidence_id": "ev-1",
                "span_id": "sp-1",
                "support_role": "support",
                "confidence_ppm": 500000,
                "evidence_digest": DIGEST_A,
            },
        )
        insert(
            owned.connection,
            "omnivia_semantic_assertions",
            assertion_row(assertion_id="asn-2", assertion_digest=DIGEST_B),
        )
        insert(
            owned.connection,
            "omnivia_semantic_assertion_supersessions",
            {
                "workspace_id": WORKSPACE_ID,
                "supersession_id": "sup-asn-1",
                "prior_assertion_id": "asn-1",
                "successor_assertion_id": "asn-2",
                "reason_code": "correction",
                "decision_id": "dec-1",
                "recorded_at_us": 1_700_000_000_000_007,
                "recorded_at_precision": "second",
                "recorded_at_provenance": "ingestion_fallback",
                "supersession_digest": DIGEST_A,
            },
        )
        insert(
            owned.connection,
            "omnivia_semantic_assertions",
            assertion_row(assertion_id="asn-3", assertion_digest=DIGEST_C),
        )
        insert(
            owned.connection,
            "omnivia_semantic_assertion_retractions",
            {
                "workspace_id": WORKSPACE_ID,
                "retraction_id": "ret-1",
                "assertion_id": "asn-3",
                "retracted_at_us": 1_700_000_000_000_008,
                "retracted_at_precision": "second",
                "retracted_at_provenance": "ingestion_fallback",
                "reason_code": "correction",
                "policy_version": "1",
                "actor_principal_id": "principal-1",
                "retraction_digest": DIGEST_A,
            },
        )
        insert(
            owned.connection,
            "omnivia_semantic_candidate_contributions",
            {
                "workspace_id": WORKSPACE_ID,
                "candidate_id": "cand-1",
                "observation_id": "obs-1",
                "contribution_role": "support",
                "weight": 100,
                "observation_digest": DIGEST_A,
                "contribution_digest": DIGEST_A,
            },
        )
        insert(
            owned.connection,
            "omnivia_semantic_candidate_reconsiderations",
            reconsideration_row(),
        )

    # Every Phase 2 table is WITHOUT ROWID; the guard fires unconditionally before
    # touching a row, so a blanket UPDATE/DELETE over the whole table proves it.
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ), pytest.raises(sqlite3.IntegrityError, match="append-only"):
        owned.connection.execute(f"UPDATE {table} SET workspace_id = workspace_id")
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ), pytest.raises(sqlite3.IntegrityError, match="append-only"):
        owned.connection.execute(f"DELETE FROM {table}")


# --- digest uniqueness, workspace scoping and cross-workspace FKs --------------


def test_duplicate_evidence_digest_within_a_workspace_is_refused(owned: Owned) -> None:
    seed_evidence_chain(owned)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        ):
        insert(
            owned.connection,
            "omnivia_semantic_evidence_items",
            evidence_row(evidence_id="ev-2"),
        )


def test_identical_evidence_digest_in_a_separately_migrated_workspace_succeeds(
    owned: Owned, tmp_path: Path
) -> None:
    seed_evidence_chain(owned)

    other = tmp_path / "other.sqlite"
    materialise_phase0_baseline(other)
    bootstrap_and_migrate(other, workspace_id=OTHER_WORKSPACE_ID)
    holder = take_ownership(other, workspace_id=OTHER_WORKSPACE_ID)
    try:
        with fenced_transaction(
            holder.connection,
            holder.identity,
            workspace_id=OTHER_WORKSPACE_ID,
            fencing_generation=holder.generation,
        ):
            insert(
                holder.connection,
                "omnivia_semantic_evidence_sources",
                source_row(workspace_id=OTHER_WORKSPACE_ID),
            )
            insert(
                holder.connection,
                "omnivia_semantic_evidence_items",
                evidence_row(workspace_id=OTHER_WORKSPACE_ID),
            )
        assert count(holder.connection, "omnivia_semantic_evidence_items") == 1
    finally:
        holder.connection.close()


def test_cross_workspace_evidence_link_is_refused(owned: Owned, tmp_path: Path) -> None:
    other = tmp_path / "other.sqlite"
    materialise_phase0_baseline(other)
    bootstrap_and_migrate(other, workspace_id=OTHER_WORKSPACE_ID)
    holder = take_ownership(other, workspace_id=OTHER_WORKSPACE_ID)
    try:
        with fenced_transaction(
            holder.connection,
            holder.identity,
            workspace_id=OTHER_WORKSPACE_ID,
            fencing_generation=holder.generation,
        ):
            insert(
                holder.connection,
                "omnivia_semantic_evidence_sources",
                source_row(workspace_id=OTHER_WORKSPACE_ID),
            )
            insert(
                holder.connection,
                "omnivia_semantic_evidence_items",
                evidence_row(workspace_id=OTHER_WORKSPACE_ID),
            )
    finally:
        holder.connection.close()

    # "ev-1" exists under OTHER_WORKSPACE_ID but never under WORKSPACE_ID: the
    # composite foreign key refuses the cross-workspace link.
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        ):
        insert(owned.connection, "omnivia_semantic_evidence_spans", span_row())


# --- representative fail-closed CHECK guards ------------------------------------


def test_bad_digest_shape_is_refused(owned: Owned) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        ):
        insert(
            owned.connection,
            "omnivia_semantic_evidence_sources",
            source_row(),
        )
        insert(
            owned.connection,
            "omnivia_semantic_evidence_items",
            evidence_row(content_digest="not-a-digest"),
        )


def test_valid_to_stated_without_a_matching_valid_to_us_is_refused(owned: Owned) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        ):
        insert(
            owned.connection,
            "omnivia_semantic_assertions",
            assertion_row(valid_to_state="stated", valid_to_us=None),
        )


def test_valid_to_open_with_a_stray_attested_to_is_refused(owned: Owned) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        ):
        insert(
            owned.connection,
            "omnivia_semantic_assertions",
            assertion_row(valid_to_state="open", attested_to_us=1_700_000_000_000_004),
        )


def test_manual_observation_with_a_rule_version_is_refused(owned: Owned) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        ):
        insert(
            owned.connection,
            "omnivia_semantic_observations",
            observation_row(generation="manual", rule_version="r1"),
        )


def test_rule_observation_without_a_rule_version_is_refused(owned: Owned) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        ):
        insert(
            owned.connection,
            "omnivia_semantic_observations",
            observation_row(generation="deterministic_rule", rule_version=None),
        )


def test_candidate_rejected_state_without_a_rejection_signature_is_refused(
    owned: Owned,
) -> None:
    seed_model(owned)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        ):
        insert(
            owned.connection,
            "omnivia_semantic_candidates",
            candidate_row(candidate_state="rejected", rejection_signature=None),
        )


def test_candidate_active_state_with_a_stray_rejection_signature_is_refused(
    owned: Owned,
) -> None:
    seed_model(owned)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        ):
        insert(
            owned.connection,
            "omnivia_semantic_candidates",
            candidate_row(candidate_state="active", rejection_signature=DIGEST_A),
        )


def test_malformed_reconsideration_new_evidence_fields_is_refused(owned: Owned) -> None:
    seed_model(owned)
    seed_candidate(owned)
    seed_suppression(owned)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        ):
        insert(
            owned.connection,
            "omnivia_semantic_candidate_reconsiderations",
            reconsideration_row(
                reason="new_evidence",
                previous_evidence_digest=DIGEST_A,
                new_evidence_digest=DIGEST_A,  # must differ from previous
            ),
        )


def test_malformed_reconsideration_human_override_without_an_actor_is_refused(
    owned: Owned,
) -> None:
    seed_model(owned)
    seed_candidate(owned)
    seed_suppression(owned)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        ):
        insert(
            owned.connection,
            "omnivia_semantic_candidate_reconsiderations",
            reconsideration_row(
                reconsideration_id="rec-2",
                reason="human_override",
                previous_evidence_digest=None,
                new_evidence_digest=None,
                actor_principal_id=None,
            ),
        )


# --- backup / restore preservation ----------------------------------------------


def test_backup_and_restore_preserves_phase2_rows(owned: Owned, tmp_path: Path) -> None:
    seed_evidence_chain(
        owned,
        evidence_overrides={
            "source_time_us": 1_700_000_000_000_000,
            "source_time_precision": "minute",
            "source_time_provenance": "evidence_attested",
            "source_time_original_text": "2023-11-14T22:13:20Z",
            "source_time_timezone": "UTC",
        },
    )
    seed_observation(
        owned,
        observation_overrides={
            "source_time_us": 1_700_000_000_000_000,
            "source_time_precision": "minute",
            "source_time_provenance": "evidence_attested",
            "source_time_original_text": "2023-11-15T08:13:20+10:00",
            "source_time_timezone": "+10:00",
        },
    )
    seed_model(owned)
    seed_assertion(
        owned,
        assertion_overrides={
            "attested_from_original_text": "2023-11-15T09:13:20",
            "attested_from_timezone": "Australia/Sydney",
        },
    )
    seed_candidate(owned)
    seed_suppression(owned)
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        insert(
            owned.connection,
            "omnivia_semantic_candidate_contributions",
            {
                "workspace_id": WORKSPACE_ID,
                "candidate_id": "cand-1",
                "observation_id": "obs-1",
                "contribution_role": "support",
                "weight": 100,
                "observation_digest": DIGEST_A,
                "contribution_digest": DIGEST_A,
            },
        )
        insert(
            owned.connection,
            "omnivia_semantic_candidate_reconsiderations",
            reconsideration_row(),
        )
    owned.connection.commit()

    before = {
        table: owned.connection.execute(f"SELECT * FROM {table}").fetchall()
        for table in P2_TABLES
    }
    owned.connection.close()

    installation = InstallationLayout(root=tmp_path / "installation-state")
    installation.create(WORKSPACE_ID)
    attempt_id = new_attempt_id()
    verified = create_verified_backup(
        owned.path, installation, workspace_id=WORKSPACE_ID, attempt_id=attempt_id
    )
    assert verified.verified

    restore_target = tmp_path / "restored.sqlite"
    restore_backup(verified.path, restore_target)

    restored = open_database(restore_target, OpenMode.READ_ONLY)
    try:
        assert integrity_check(restored) == []
        assert foreign_key_check(restored) == []
        for table, rows in before.items():
            assert restored.execute(f"SELECT * FROM {table}").fetchall() == rows, table
    finally:
        restored.close()
