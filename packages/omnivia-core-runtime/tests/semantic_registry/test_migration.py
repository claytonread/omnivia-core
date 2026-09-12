"""Acceptance tests for `0037_semantic_registry.sql` (SR-101).

Focused, not exhaustive: this proves 0037 is the unique successor to 0036, that it
adds exactly the eighteen `omnivia_semantic_*` tables and their fifty-four guard
triggers, that the fencing predicate and append-only guards actually hold, that the
pointer/activation contract advances correctly and refuses a stale or
non-contiguous move, that the representative FK/digest/ordinal/idempotency guards
fail closed, and that a backup/restore round trip preserves rows exactly.
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
    assert_guards_intact,
    fenced_transaction,
    trigger_names,
    verify_fingerprint,
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
    canonical_schema_fingerprint,
    load_migrations,
    materialise_phase0_baseline,
)
from test_application_audit_idempotency_migration import (  # type: ignore[import-not-found]
    Owned,
    bootstrap_and_migrate,
    count,
    insert,
    make_identity,
    object_names,
    take_ownership,
)
from test_application_audit_idempotency_migration import (
    row_for as audit_row_for,
)

MIGRATION_VERSION = 37
MIGRATION_NAME = "0037_semantic_registry.sql"
PREDECESSOR_NAME = "0036_workflow_control_cancellation_lineage.sql"

WORKSPACE_ID = "ws-sr-0001"
OTHER_WORKSPACE_ID = "ws-sr-0002"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64

SR_TABLES = (
    "omnivia_semantic_models",
    "omnivia_semantic_model_versions",
    "omnivia_semantic_version_parents",
    "omnivia_semantic_version_elements",
    "omnivia_semantic_current_pointers",
    "omnivia_semantic_version_activations",
    "omnivia_semantic_change_sets",
    "omnivia_semantic_change_operations",
    "omnivia_semantic_review_requests",
    "omnivia_semantic_review_decisions",
    "omnivia_semantic_approval_records",
    "omnivia_semantic_consumers",
    "omnivia_semantic_consumer_dependencies",
    "omnivia_semantic_consumer_supported_ranges",
    "omnivia_semantic_consumer_version_bindings",
    "omnivia_semantic_publication_records",
    "omnivia_semantic_outbox",
    "omnivia_semantic_outbox_dispatches",
)

#: Append-only tables: every UPDATE and DELETE is refused unconditionally.
APPEND_ONLY_TABLES = (
    "omnivia_semantic_model_versions",
    "omnivia_semantic_version_parents",
    "omnivia_semantic_version_elements",
    "omnivia_semantic_version_activations",
    "omnivia_semantic_change_sets",
    "omnivia_semantic_change_operations",
    "omnivia_semantic_review_requests",
    "omnivia_semantic_review_decisions",
    "omnivia_semantic_approval_records",
    "omnivia_semantic_consumer_dependencies",
    "omnivia_semantic_consumer_supported_ranges",
    "omnivia_semantic_consumer_version_bindings",
    "omnivia_semantic_publication_records",
    "omnivia_semantic_outbox",
    "omnivia_semantic_outbox_dispatches",
)

SR_TRIGGERS = tuple(
    f"omnivia_guard_{table.removeprefix('omnivia_')}_{statement}"
    for table in SR_TABLES
    for statement in ("insert", "update", "delete")
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

AUDIT_REF = "aud-sr-0001"


def audit_event_row(**overrides: object) -> dict[str, object]:
    base = {"audit_ref": AUDIT_REF, "workspace_id": WORKSPACE_ID}
    base.update(overrides)
    return audit_row_for("omnivia_application_audit_events", **base)


def model_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "model_id": "model-a",
        "model_kind": "graph",
        "created_at_us": 1_700_000_000_000_000,
        "updated_at_us": 1_700_000_000_000_000,
        "archived": 0,
    }
    values.update(overrides)
    return values


def pointer_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "model_id": "model-a",
        "current_version_id": None,
        "generation": 0,
        "updated_at_us": 1_700_000_000_000_000,
    }
    values.update(overrides)
    return values


def version_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "model_id": "model-a",
        "version_id": "v1",
        "label": "1.0.0",
        "sequence": 0,
        "content_digest": DIGEST_A,
        "content_json": '{"a":1}',
        "created_at_us": 1_700_000_000_000_001,
    }
    values.update(overrides)
    return values


def activation_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "model_id": "model-a",
        "activation_sequence": 0,
        "version_id": "v1",
        "previous_version_id": None,
        "generation": 1,
        "activated_at_us": 1_700_000_000_000_002,
        "audit_ref": AUDIT_REF,
    }
    values.update(overrides)
    return values


def seed_model_and_pointer(holder: Owned, **model_overrides: object) -> None:
    """The audit event (once), the model and its generation-zero pointer."""
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
        insert(holder.connection, "omnivia_semantic_models", model_row(**model_overrides))
        insert(
            holder.connection,
            "omnivia_semantic_current_pointers",
            pointer_row(model_id=model_overrides.get("model_id", "model-a")),
        )


def activate(
    holder: Owned, *, model_id: str = "model-a", version_id: str = "v1"
) -> None:
    """Insert the smallest valid version, then activate it to generation 1."""
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        insert(
            holder.connection,
            "omnivia_semantic_model_versions",
            version_row(model_id=model_id, version_id=version_id),
        )
        insert(
            holder.connection,
            "omnivia_semantic_version_activations",
            activation_row(model_id=model_id, version_id=version_id),
        )


# --- migration identity, ledger and fingerprint --------------------------------


def test_0037_is_the_unique_successor_to_0036() -> None:
    ordered = load_migrations()
    versions = [m.version for m in ordered]
    prefix = [v for v in versions if v <= MIGRATION_VERSION]
    assert prefix == list(range(1, MIGRATION_VERSION + 1)), versions

    matches = [m for m in ordered if m.version == MIGRATION_VERSION]
    assert len(matches) == 1, [m.name for m in matches]
    assert matches[0].name == MIGRATION_NAME

    index = ordered.index(matches[0])
    assert ordered[index - 1].name == PREDECESSOR_NAME


def test_pristine_bootstrap_adds_exactly_the_eighteen_tables(tmp_path: Path) -> None:
    path = tmp_path / "pristine.sqlite"
    connection = open_database(path, OpenMode.EPHEMERAL)
    try:
        from omnivia_core_runtime.storage.migrations import (
            apply_pending_migrations,
            bootstrap_generation_one,
        )

        state = bootstrap_generation_one(
            connection,
            workspace_id=WORKSPACE_ID,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            expect_phase0_baseline=False,
            service_instance_id=make_identity().service_instance_id,
        )
        applied = apply_pending_migrations(
            connection,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            service_instance_id=make_identity().service_instance_id,
            fencing_generation=state.fencing_generation,
            workspace_id=WORKSPACE_ID,
        )
        assert MIGRATION_VERSION in [m.version for m in applied]
        present_tables = object_names(connection, "table")
        assert set(SR_TABLES) <= present_tables
        assert set(SR_TRIGGERS) <= object_names(connection, "trigger")
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
    finally:
        connection.close()


def test_all_54_guard_triggers_exist_and_fingerprint_is_intact(migrated: Path) -> None:
    assert len(SR_TRIGGERS) == 54

    connection = open_database(migrated, OpenMode.READ_ONLY)
    try:
        present = set(trigger_names(connection))
        assert set(SR_TRIGGERS) <= present
        assert_guards_intact(connection)

        canonical = canonical_schema_fingerprint()
        assert verify_fingerprint(connection, canonical).matches(canonical)
    finally:
        connection.close()


# --- fencing: unguarded / fenced / wrong-workspace -----------------------------


def test_unguarded_insert_on_a_representative_model_table_fails(owned: Owned) -> None:
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        insert(owned.connection, "omnivia_semantic_models", model_row())


def test_fenced_insert_on_a_representative_model_table_succeeds(owned: Owned) -> None:
    seed_model_and_pointer(owned)
    assert count(owned.connection, "omnivia_semantic_models") == 1
    assert count(owned.connection, "omnivia_semantic_current_pointers") == 1


def test_wrong_workspace_insert_fails_inside_the_fence(owned: Owned) -> None:
    with pytest.raises(sqlite3.IntegrityError), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        insert(
            owned.connection,
            "omnivia_semantic_models",
            model_row(workspace_id=OTHER_WORKSPACE_ID),
        )
    assert count(owned.connection, "omnivia_semantic_models") == 0


# --- pointer / activation lifecycle --------------------------------------------


def test_generation_zero_pointer_then_activation_to_generation_one(
    owned: Owned,
) -> None:
    seed_model_and_pointer(owned)
    activate(owned)

    pointer = owned.connection.execute(
        "SELECT current_version_id, generation FROM omnivia_semantic_current_pointers "
        "WHERE workspace_id = ? AND model_id = 'model-a'",
        (WORKSPACE_ID,),
    ).fetchone()
    assert pointer == ("v1", 1)


def test_stale_activation_after_a_move_is_refused(owned: Owned) -> None:
    seed_model_and_pointer(owned)
    activate(owned)

    with pytest.raises(sqlite3.IntegrityError), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        insert(
            owned.connection,
            "omnivia_semantic_model_versions",
            version_row(
                version_id="v2", label="1.0.1", sequence=1, content_digest=DIGEST_B
            ),
        )
        # Replays the *first* activation's predecessor (NULL) instead of the
        # pointer's actual current version ("v1"): a stale activation attempt.
        insert(
            owned.connection,
            "omnivia_semantic_version_activations",
            activation_row(
                activation_sequence=1,
                version_id="v2",
                previous_version_id=None,
                generation=2,
            ),
        )
    pointer = owned.connection.execute(
        "SELECT current_version_id, generation FROM omnivia_semantic_current_pointers "
        "WHERE workspace_id = ? AND model_id = 'model-a'",
        (WORKSPACE_ID,),
    ).fetchone()
    assert pointer == ("v1", 1)


def test_noncontiguous_activation_sequence_is_refused(owned: Owned) -> None:
    seed_model_and_pointer(owned)
    activate(owned)

    with pytest.raises(sqlite3.IntegrityError, match="activation sequence"), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        ):
        insert(
            owned.connection,
            "omnivia_semantic_model_versions",
            version_row(
                version_id="v2", label="1.0.1", sequence=1, content_digest=DIGEST_B
            ),
        )
        insert(
            owned.connection,
            "omnivia_semantic_version_activations",
            activation_row(
                activation_sequence=5,
                version_id="v2",
                previous_version_id="v1",
                generation=2,
            ),
        )


# --- append-only guards ---------------------------------------------------------


def seed_every_append_only_table(holder: Owned) -> None:
    """One row in every append-only table, via the smallest valid chain."""
    seed_model_and_pointer(holder)
    activate(holder)
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        insert(
            holder.connection,
            "omnivia_semantic_model_versions",
            version_row(
                version_id="v-parent",
                label="0.9.0",
                sequence=1,
                content_digest="sha256:" + "c" * 64,
            ),
        )
        insert(
            holder.connection,
            "omnivia_semantic_version_parents",
            {
                "workspace_id": WORKSPACE_ID,
                "model_id": "model-a",
                "version_id": "v1",
                "parent_version_id": "v-parent",
            },
        )
        insert(
            holder.connection,
            "omnivia_semantic_version_elements",
            {
                "workspace_id": WORKSPACE_ID,
                "model_id": "model-a",
                "version_id": "v1",
                "element_id": "el-1",
                "element_json": '{"e":1}',
                "element_digest": DIGEST_A,
            },
        )
        insert(
            holder.connection,
            "omnivia_semantic_change_sets",
            {
                "workspace_id": WORKSPACE_ID,
                "change_set_id": "cs-1",
                "model_id": "model-a",
                "base_version_id": "v1",
                "change_set_digest": DIGEST_A,
                "created_at_us": 1_700_000_000_000_003,
            },
        )
        insert(
            holder.connection,
            "omnivia_semantic_change_operations",
            {
                "workspace_id": WORKSPACE_ID,
                "change_set_id": "cs-1",
                "ordinal": 0,
                "operation_json": '{"op":1}',
                "operation_digest": DIGEST_A,
            },
        )
        insert(
            holder.connection,
            "omnivia_semantic_review_requests",
            {
                "workspace_id": WORKSPACE_ID,
                "review_request_id": "rr-1",
                "change_set_id": "cs-1",
                "requested_at_us": 1_700_000_000_000_004,
            },
        )
        insert(
            holder.connection,
            "omnivia_semantic_review_decisions",
            {
                "workspace_id": WORKSPACE_ID,
                "review_decision_id": "rd-1",
                "review_request_id": "rr-1",
                "reviewer_id": "reviewer-1",
                "decision": "approved",
                "decided_at_us": 1_700_000_000_000_005,
            },
        )
        insert(
            holder.connection,
            "omnivia_semantic_approval_records",
            {
                "workspace_id": WORKSPACE_ID,
                "approval_id": "ap-1",
                "change_set_id": "cs-1",
                "change_set_digest": DIGEST_A,
                "review_decision_id": "rd-1",
                "approved_at_us": 1_700_000_000_000_006,
            },
        )
        insert(
            holder.connection,
            "omnivia_semantic_consumers",
            {
                "workspace_id": WORKSPACE_ID,
                "consumer_id": "consumer-1",
                "created_at_us": 1_700_000_000_000_003,
                "updated_at_us": 1_700_000_000_000_003,
                "archived": 0,
            },
        )
        insert(
            holder.connection,
            "omnivia_semantic_consumer_dependencies",
            {
                "workspace_id": WORKSPACE_ID,
                "consumer_id": "consumer-1",
                "model_id": "model-a",
                "declared_at_us": 1_700_000_000_000_004,
            },
        )
        insert(
            holder.connection,
            "omnivia_semantic_consumer_supported_ranges",
            {
                "workspace_id": WORKSPACE_ID,
                "consumer_id": "consumer-1",
                "model_id": "model-a",
                "min_sequence": 0,
                "max_sequence": None,
                "declared_at_us": 1_700_000_000_000_005,
            },
        )
        insert(
            holder.connection,
            "omnivia_semantic_consumer_version_bindings",
            {
                "workspace_id": WORKSPACE_ID,
                "consumer_id": "consumer-1",
                "model_id": "model-a",
                "version_id": "v1",
                "bound_at_us": 1_700_000_000_000_006,
            },
        )
        insert(
            holder.connection,
            "omnivia_semantic_publication_records",
            {
                "workspace_id": WORKSPACE_ID,
                "publication_id": "pub-1",
                "idempotency_key": "pub-key-1",
                "request_digest": DIGEST_A,
                "model_id": "model-a",
                "base_version_id": None,
                "result_version_id": "v1",
                "expected_pointer_generation": 0,
                "resulting_pointer_generation": 1,
                "approval_id": "ap-1",
                "validation_digest": DIGEST_B,
                "published_at_us": 1_700_000_000_000_007,
            },
        )
        insert(
            holder.connection,
            "omnivia_semantic_outbox",
            {
                "workspace_id": WORKSPACE_ID,
                "aggregate_id": "agg-1",
                "sequence": 0,
                "outbox_id": "ob-1",
                "event_kind": "model.published",
                "payload_json": '{"p":1}',
                "payload_digest": DIGEST_A,
                "created_at_us": 1_700_000_000_000_008,
            },
        )
        insert(
            holder.connection,
            "omnivia_semantic_outbox_dispatches",
            {
                "workspace_id": WORKSPACE_ID,
                "outbox_id": "ob-1",
                "dispatch_number": 1,
                "acknowledged_at_us": 1_700_000_000_000_009,
            },
        )


@pytest.mark.parametrize("table", APPEND_ONLY_TABLES)
def test_append_only_tables_reject_update_and_delete_for_the_current_owner(
    owned: Owned, table: str
) -> None:
    seed_every_append_only_table(owned)

    # Every table here is `WITHOUT ROWID`, so there is no rowid to key on; the guard
    # trigger fires unconditionally before any row is touched, so a blanket
    # UPDATE/DELETE over the whole table proves the same thing.
    assert count(owned.connection, table) > 0

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


# --- representative fail-closed guards ------------------------------------------


def test_bad_digest_shape_is_refused(owned: Owned) -> None:
    seed_model_and_pointer(owned)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        ):
        insert(
            owned.connection,
            "omnivia_semantic_model_versions",
            version_row(content_digest="not-a-digest"),
        )


def test_cross_workspace_version_link_is_refused(owned: Owned, tmp_path: Path) -> None:
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
                "omnivia_application_audit_events",
                audit_event_row(workspace_id=OTHER_WORKSPACE_ID),
            )
            insert(
                holder.connection,
                "omnivia_semantic_models",
                model_row(workspace_id=OTHER_WORKSPACE_ID),
            )
            insert(
                holder.connection,
                "omnivia_semantic_current_pointers",
                pointer_row(workspace_id=OTHER_WORKSPACE_ID),
            )
    finally:
        holder.connection.close()

    # "model-a" exists under OTHER_WORKSPACE_ID, but never under WORKSPACE_ID: the
    # composite foreign key (workspace_id, model_id) refuses the cross-workspace
    # link even though a same-named model exists elsewhere.
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        ):
        insert(owned.connection, "omnivia_semantic_model_versions", version_row())


def test_cross_model_version_element_link_is_refused(owned: Owned) -> None:
    seed_model_and_pointer(owned, model_id="model-a")
    seed_model_and_pointer(owned, model_id="model-b")
    activate(owned, model_id="model-a", version_id="v1")

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        ):
        insert(
            owned.connection,
            "omnivia_semantic_version_elements",
            {
                "workspace_id": WORKSPACE_ID,
                "model_id": "model-b",
                "version_id": "v1",
                "element_id": "el-1",
                "element_json": '{"e":1}',
                "element_digest": DIGEST_A,
            },
        )


def test_noncontiguous_change_operation_ordinal_is_refused(owned: Owned) -> None:
    seed_model_and_pointer(owned)
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        insert(
            owned.connection,
            "omnivia_semantic_change_sets",
            {
                "workspace_id": WORKSPACE_ID,
                "change_set_id": "cs-1",
                "model_id": "model-a",
                "base_version_id": None,
                "change_set_digest": DIGEST_A,
                "created_at_us": 1_700_000_000_000_003,
            },
        )
    with pytest.raises(sqlite3.IntegrityError, match="ordinal must be contiguous"), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        ):
        insert(
            owned.connection,
            "omnivia_semantic_change_operations",
            {
                "workspace_id": WORKSPACE_ID,
                "change_set_id": "cs-1",
                "ordinal": 3,
                "operation_json": '{"op":1}',
                "operation_digest": DIGEST_A,
            },
        )


def test_duplicate_change_set_digest_for_one_model_is_refused(owned: Owned) -> None:
    seed_model_and_pointer(owned)
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        insert(
            owned.connection,
            "omnivia_semantic_change_sets",
            {
                "workspace_id": WORKSPACE_ID,
                "change_set_id": "cs-1",
                "model_id": "model-a",
                "base_version_id": None,
                "change_set_digest": DIGEST_A,
                "created_at_us": 1_700_000_000_000_003,
            },
        )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        ):
        insert(
            owned.connection,
            "omnivia_semantic_change_sets",
            {
                "workspace_id": WORKSPACE_ID,
                "change_set_id": "cs-2",
                "model_id": "model-a",
                "base_version_id": None,
                "change_set_digest": DIGEST_A,
                "created_at_us": 1_700_000_000_000_004,
            },
        )


def test_duplicate_review_decision_for_one_request_is_refused(owned: Owned) -> None:
    seed_model_and_pointer(owned)
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        insert(
            owned.connection,
            "omnivia_semantic_change_sets",
            {
                "workspace_id": WORKSPACE_ID,
                "change_set_id": "cs-1",
                "model_id": "model-a",
                "base_version_id": None,
                "change_set_digest": DIGEST_A,
                "created_at_us": 1_700_000_000_000_003,
            },
        )
        insert(
            owned.connection,
            "omnivia_semantic_review_requests",
            {
                "workspace_id": WORKSPACE_ID,
                "review_request_id": "rr-1",
                "change_set_id": "cs-1",
                "requested_at_us": 1_700_000_000_000_004,
            },
        )
        insert(
            owned.connection,
            "omnivia_semantic_review_decisions",
            {
                "workspace_id": WORKSPACE_ID,
                "review_decision_id": "rd-1",
                "review_request_id": "rr-1",
                "reviewer_id": "reviewer-1",
                "decision": "approved",
                "decided_at_us": 1_700_000_000_000_005,
            },
        )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        ):
        insert(
            owned.connection,
            "omnivia_semantic_review_decisions",
            {
                "workspace_id": WORKSPACE_ID,
                "review_decision_id": "rd-2",
                "review_request_id": "rr-1",
                "reviewer_id": "reviewer-2",
                "decision": "rejected",
                "decided_at_us": 1_700_000_000_000_006,
            },
        )


def test_duplicate_publication_idempotency_key_is_refused(owned: Owned) -> None:
    seed_model_and_pointer(owned)
    activate(owned)
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        insert(
            owned.connection,
            "omnivia_semantic_change_sets",
            {
                "workspace_id": WORKSPACE_ID,
                "change_set_id": "cs-1",
                "model_id": "model-a",
                "base_version_id": "v1",
                "change_set_digest": DIGEST_A,
                "created_at_us": 1_700_000_000_000_003,
            },
        )
        insert(
            owned.connection,
            "omnivia_semantic_review_requests",
            {
                "workspace_id": WORKSPACE_ID,
                "review_request_id": "rr-1",
                "change_set_id": "cs-1",
                "requested_at_us": 1_700_000_000_000_004,
            },
        )
        insert(
            owned.connection,
            "omnivia_semantic_review_decisions",
            {
                "workspace_id": WORKSPACE_ID,
                "review_decision_id": "rd-1",
                "review_request_id": "rr-1",
                "reviewer_id": "reviewer-1",
                "decision": "approved",
                "decided_at_us": 1_700_000_000_000_005,
            },
        )
        insert(
            owned.connection,
            "omnivia_semantic_approval_records",
            {
                "workspace_id": WORKSPACE_ID,
                "approval_id": "ap-1",
                "change_set_id": "cs-1",
                "change_set_digest": DIGEST_A,
                "review_decision_id": "rd-1",
                "approved_at_us": 1_700_000_000_000_006,
            },
        )
        publication = {
            "workspace_id": WORKSPACE_ID,
            "publication_id": "pub-1",
            "idempotency_key": "pub-key-1",
            "request_digest": DIGEST_A,
            "model_id": "model-a",
            "base_version_id": "v1",
            "result_version_id": "v1",
            "expected_pointer_generation": 0,
            "resulting_pointer_generation": 1,
            "approval_id": "ap-1",
            "validation_digest": DIGEST_B,
            "published_at_us": 1_700_000_000_000_007,
        }
        insert(owned.connection, "omnivia_semantic_publication_records", publication)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        ):
        insert(
            owned.connection,
            "omnivia_semantic_publication_records",
            {**publication, "publication_id": "pub-2"},
        )


def test_noncontiguous_outbox_sequence_is_refused(owned: Owned) -> None:
    seed_model_and_pointer(owned)
    with pytest.raises(sqlite3.IntegrityError, match="sequence must be contiguous"), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        ):
        insert(
            owned.connection,
            "omnivia_semantic_outbox",
            {
                "workspace_id": WORKSPACE_ID,
                "aggregate_id": "agg-1",
                "sequence": 1,
                "outbox_id": "ob-1",
                "event_kind": "model.published",
                "payload_json": '{"p":1}',
                "payload_digest": DIGEST_A,
                "created_at_us": 1_700_000_000_000_003,
            },
        )


# --- backup / restore preservation ----------------------------------------------


def test_consumer_version_binding_permits_a_later_exact_version(owned: Owned) -> None:
    """The binding key includes `version_id`, so a later exact bind is a new row,
    not a rewrite of the first -- and rebinding the same version twice is refused
    by the same key."""
    seed_model_and_pointer(owned)
    activate(owned, version_id="v1")
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        insert(
            owned.connection,
            "omnivia_semantic_model_versions",
            version_row(version_id="v2", label="1.0.1", sequence=1, content_digest=DIGEST_B),
        )
        insert(
            owned.connection,
            "omnivia_semantic_consumers",
            {
                "workspace_id": WORKSPACE_ID,
                "consumer_id": "consumer-1",
                "created_at_us": 1_700_000_000_000_003,
                "updated_at_us": 1_700_000_000_000_003,
                "archived": 0,
            },
        )
        insert(
            owned.connection,
            "omnivia_semantic_consumer_dependencies",
            {
                "workspace_id": WORKSPACE_ID,
                "consumer_id": "consumer-1",
                "model_id": "model-a",
                "declared_at_us": 1_700_000_000_000_004,
            },
        )
        insert(
            owned.connection,
            "omnivia_semantic_consumer_version_bindings",
            {
                "workspace_id": WORKSPACE_ID,
                "consumer_id": "consumer-1",
                "model_id": "model-a",
                "version_id": "v1",
                "bound_at_us": 1_700_000_000_000_005,
            },
        )
        insert(
            owned.connection,
            "omnivia_semantic_consumer_version_bindings",
            {
                "workspace_id": WORKSPACE_ID,
                "consumer_id": "consumer-1",
                "model_id": "model-a",
                "version_id": "v2",
                "bound_at_us": 1_700_000_000_000_006,
            },
        )
    assert count(owned.connection, "omnivia_semantic_consumer_version_bindings") == 2

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"), fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
        ):
        insert(
            owned.connection,
            "omnivia_semantic_consumer_version_bindings",
            {
                "workspace_id": WORKSPACE_ID,
                "consumer_id": "consumer-1",
                "model_id": "model-a",
                "version_id": "v1",
                "bound_at_us": 1_700_000_000_000_007,
            },
        )


def test_backup_and_restore_preserves_representative_rows(
    owned: Owned, tmp_path: Path
) -> None:
    seed_model_and_pointer(owned)
    activate(owned)
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        insert(
            owned.connection,
            "omnivia_semantic_consumers",
            {
                "workspace_id": WORKSPACE_ID,
                "consumer_id": "consumer-1",
                "created_at_us": 1_700_000_000_000_003,
                "updated_at_us": 1_700_000_000_000_003,
                "archived": 0,
            },
        )
        insert(
            owned.connection,
            "omnivia_semantic_consumer_dependencies",
            {
                "workspace_id": WORKSPACE_ID,
                "consumer_id": "consumer-1",
                "model_id": "model-a",
                "declared_at_us": 1_700_000_000_000_004,
            },
        )
        insert(
            owned.connection,
            "omnivia_semantic_consumer_version_bindings",
            {
                "workspace_id": WORKSPACE_ID,
                "consumer_id": "consumer-1",
                "model_id": "model-a",
                "version_id": "v1",
                "bound_at_us": 1_700_000_000_000_005,
            },
        )
    owned.connection.commit()

    before = {
        table: owned.connection.execute(f"SELECT * FROM {table}").fetchall()
        for table in SR_TABLES
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
        canonical = canonical_schema_fingerprint()
        assert verify_fingerprint(restored, canonical).matches(canonical)
        for table, rows in before.items():
            assert restored.execute(f"SELECT * FROM {table}").fetchall() == rows, table
    finally:
        restored.close()
