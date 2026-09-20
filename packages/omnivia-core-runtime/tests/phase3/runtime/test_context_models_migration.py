"""Acceptance for migration 0026's durable Context Model foundation.

What 0026 is: a unique consecutive successor to 0025, pinned by content checksum,
whose objects are exactly four append-only tables, three named indexes and twelve
statement triggers. A Context Model version is immutable, its lifecycle is a chain of
append-only facts rather than a column somebody overwrites, and it may only be
published once it names an exact sealed governed L2 version -- accepted, canonical and
digest-identical -- as its grounding.

What it is not: no repository, no service, no handler, no renderer, no Chat state, no
workflow state and no Provider invocation state. Nothing here acts on a Context Model;
it only records one.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_governed_truth_and_relations_migration as m3
from omnivia_core_runtime.ownership.fencing import close_guard, fenced_transaction
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    authorised,
    foreign_key_check,
    integrity_check,
    open_database,
)
from omnivia_core_runtime.storage.migrations import (
    applied_migrations,
    load_migrations,
    materialise_phase0_baseline,
)

MIGRATION_VERSION = 26
PREDECESSOR_VERSION = 25
MIGRATION_NAME = "0026_context_models.sql"
WORKSPACE_ID = m3.WORKSPACE_ID
BASE_US = m3.BASE_US + 1_000

MODELS = "omnivia_context_models"
VERSIONS = "omnivia_context_model_versions"
GROUNDING = "omnivia_context_model_grounding_refs"
LIFECYCLE = "omnivia_context_model_lifecycle_events"
TABLES = (MODELS, VERSIONS, GROUNDING, LIFECYCLE)

INDEXES = {
    "omnivia_idx_context_models_subtype",
    "omnivia_idx_context_model_versions_identity",
    "omnivia_idx_context_model_grounding_refs_governed",
}

TRIGGERS = {
    f"omnivia_guard_{table.removeprefix('omnivia_')}_{statement}"
    for table in TABLES
    for statement in ("insert", "update", "delete")
}

MODEL_ID = "context-model-0001"
GENERATOR = "generator-1"
REVIEWER = "reviewer-9"
CONTENT_DIGEST = "sha256:" + "a" * 64
INSTRUCTION_DIGEST = "sha256:" + "b" * 64

#: The sealed, accepted, canonical governed L2 version `m3.seed_accepted_version`
#: leaves behind, and the digest it was sealed with.
GOVERNED_RECORD = "record-1"
GOVERNED_VERSION = "version-accepted"
GOVERNED_DIGEST = m3.DIGEST_B


def migration_under_test() -> Any:
    found = [m for m in load_migrations() if m.version == MIGRATION_VERSION]
    assert len(found) == 1, [m.name for m in load_migrations()]
    return found[0]


MIGRATION = migration_under_test()


@pytest.fixture
def migrated(tmp_path: Path) -> Path:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    with m1.migration_catalogue_through(MIGRATION_VERSION):
        m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    return path


@pytest.fixture
def owned(migrated: Path) -> Iterator[m3.m2.Owned]:
    holder = m3.m2.take_ownership(migrated)
    m3.m2.seed_chain(holder)
    with guarded(holder):
        for number in range(1, 9):
            m3.insert(
                holder.connection,
                "omnivia_application_audit_events",
                m3.audit_row(f"audit-{number}"),
            )
    yield holder
    holder.connection.close()


def guarded(holder: m3.m2.Owned) -> Any:
    return fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    )


def insert(holder: m3.m2.Owned, table: str, row: dict[str, object]) -> None:
    m3.insert(holder.connection, table, row)


def model_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "context_model_id": MODEL_ID,
        "subtype": "project",
        "created_by_actor_id": GENERATOR,
        "created_at_us": BASE_US,
        "audit_ref": "audit-3",
    }
    values.update(overrides)
    return values


def version_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "context_model_id": MODEL_ID,
        "version_number": 1,
        "subtype": "project",
        "content_json": '{"summary":"a durable context model"}',
        "content_digest": CONTENT_DIGEST,
        "generated_by_actor_id": GENERATOR,
        "generated_by_actor_kind": "service",
        "instruction_digest": INSTRUCTION_DIGEST,
        "parent_version_number": None,
        "created_at_us": BASE_US + 1,
        "audit_ref": "audit-4",
    }
    values.update(overrides)
    return values


def grounding_row(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "context_model_id": MODEL_ID,
        "version_number": 1,
        "governed_record_id": GOVERNED_RECORD,
        "governed_record_version_id": GOVERNED_VERSION,
        "grounded_content_digest": GOVERNED_DIGEST,
        "recorded_at_us": BASE_US + 2,
    }
    values.update(overrides)
    return values


def lifecycle_row(
    sequence: int,
    from_state: str | None,
    to_state: str,
    **overrides: object,
) -> dict[str, object]:
    values: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "context_model_id": MODEL_ID,
        "version_number": 1,
        "event_sequence": sequence,
        "from_state": from_state,
        "to_state": to_state,
        "actor_id": GENERATOR if from_state is None else REVIEWER,
        "occurred_at_us": BASE_US + 10 + sequence,
        "reason": None,
        "audit_ref": "audit-5",
    }
    values.update(overrides)
    return values


def seed_generated_version(holder: m3.m2.Owned, *, grounded: bool = True) -> None:
    """A Context Model whose first version exists and has been generated."""
    if grounded:
        m3.seed_accepted_version(holder)
    with guarded(holder):
        insert(holder, MODELS, model_row())
        insert(holder, VERSIONS, version_row())
        insert(holder, LIFECYCLE, lifecycle_row(1, None, "generated"))
        if grounded:
            insert(holder, GROUNDING, grounding_row())


# --- what the migration is ----------------------------------------------------


def test_0026_is_the_unique_consecutive_successor_to_0025() -> None:
    versions = [m.version for m in load_migrations()]
    assert versions == sorted(versions)
    assert versions[:MIGRATION_VERSION] == list(range(1, MIGRATION_VERSION + 1))
    assert MIGRATION.version == PREDECESSOR_VERSION + 1
    assert MIGRATION.name == MIGRATION_NAME


def test_the_ledger_records_this_exact_migration_text(migrated: Path) -> None:
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        recorded = applied_migrations(connection)
    finally:
        connection.close()
    assert recorded[MIGRATION_VERSION] == MIGRATION.checksum
    assert (
        hashlib.sha256(MIGRATION.sql.encode("utf-8")).hexdigest() == MIGRATION.checksum
    )


def test_schema_inventory_contains_the_expected_new_objects(migrated: Path) -> None:
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        named = {
            kind: {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
                )
            }
            for kind in ("table", "index", "trigger")
        }
        assert set(TABLES) <= named["table"]
        assert INDEXES <= named["index"]
        assert TRIGGERS <= named["trigger"]
        assert len(TRIGGERS) == 12
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
    finally:
        connection.close()


def test_integrity_and_foreign_keys_stay_clean_once_populated(
    owned: m3.m2.Owned,
) -> None:
    seed_generated_version(owned)
    assert integrity_check(owned.connection) == []
    assert foreign_key_check(owned.connection) == []


# --- what the tables refuse ---------------------------------------------------


@pytest.mark.parametrize("table", TABLES)
def test_inserts_require_the_fenced_owner(owned: m3.m2.Owned, table: str) -> None:
    row = {
        MODELS: model_row(),
        VERSIONS: version_row(),
        GROUNDING: grounding_row(),
        LIFECYCLE: lifecycle_row(1, None, "generated"),
    }[table]
    close_guard(owned.connection)
    with (
        authorised(owned.connection, mutations=True),
        pytest.raises(sqlite3.DatabaseError, match=f"unguarded INSERT on {table}"),
    ):
        insert(owned, table, row)
    assert owned.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)


def test_an_unknown_subtype_is_refused(owned: m3.m2.Owned) -> None:
    with guarded(owned), pytest.raises(sqlite3.IntegrityError):
        insert(owned, MODELS, model_row(subtype="mood_board"))


def test_the_lifecycle_chain_generated_reviewed_published_is_admitted(
    owned: m3.m2.Owned,
) -> None:
    seed_generated_version(owned)
    with guarded(owned):
        insert(owned, LIFECYCLE, lifecycle_row(2, "generated", "reviewed"))
        insert(owned, LIFECYCLE, lifecycle_row(3, "reviewed", "published"))

    assert [
        row[0]
        for row in owned.connection.execute(
            f"SELECT to_state FROM {LIFECYCLE} ORDER BY event_sequence"
        )
    ] == ["generated", "reviewed", "published"]


def test_an_invalid_transition_is_refused(owned: m3.m2.Owned) -> None:
    seed_generated_version(owned)
    with guarded(owned), pytest.raises(sqlite3.IntegrityError):
        insert(owned, LIFECYCLE, lifecycle_row(2, "generated", "published"))


def test_a_terminal_version_can_never_be_reopened(owned: m3.m2.Owned) -> None:
    seed_generated_version(owned)
    with guarded(owned):
        insert(
            owned,
            LIFECYCLE,
            lifecycle_row(2, "generated", "withdrawn", reason="withdrawn_by_owner"),
        )
    with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="current state"):
        insert(owned, LIFECYCLE, lifecycle_row(3, "generated", "reviewed"))


def test_publishing_without_grounding_is_refused(owned: m3.m2.Owned) -> None:
    seed_generated_version(owned, grounded=False)
    with guarded(owned):
        insert(owned, LIFECYCLE, lifecycle_row(2, "generated", "reviewed"))
    with (
        guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="grounding reference"),
    ):
        insert(owned, LIFECYCLE, lifecycle_row(3, "reviewed", "published"))


# --- what grounding must name -------------------------------------------------


@pytest.mark.parametrize(
    ("seed", "overrides"),
    (
        ("accepted", {"grounded_content_digest": "sha256:" + "9" * 64}),
        ("accepted", {"governed_record_version_id": "version-1"}),
        ("candidate", {"governed_record_version_id": "version-1"}),
        ("rejected", {"governed_record_version_id": "version-rejected"}),
    ),
    ids=(
        "digest-drift",
        "unsealed-name",
        "candidate-layer",
        "rejected-governed-version",
    ),
)
def test_grounding_must_name_an_exact_sealed_accepted_canonical_governed_version(
    owned: m3.m2.Owned, seed: str, overrides: dict[str, object]
) -> None:
    if seed == "rejected":
        m3.seed_governed_outcome(owned, "rejected")
    elif seed == "candidate":
        m3.seed_human_candidate(owned)
    else:
        m3.seed_accepted_version(owned)
    with guarded(owned):
        insert(owned, MODELS, model_row())
        insert(owned, VERSIONS, version_row())
        insert(owned, LIFECYCLE, lifecycle_row(1, None, "generated"))
    with (
        guarded(owned),
        pytest.raises(sqlite3.IntegrityError, match="exact sealed governed L2 version"),
    ):
        insert(owned, GROUNDING, grounding_row(**overrides))


def test_a_rejected_governed_version_is_sealed_but_never_canonical(
    owned: m3.m2.Owned,
) -> None:
    m3.seed_governed_outcome(owned, "rejected")
    assert owned.connection.execute(
        "SELECT layer, governance_disposition, authority_level "
        f"FROM {m3.VIEW} WHERE governed_record_version_id = 'version-rejected'"
    ).fetchone() == ("governed", "rejected", "reviewed")


# --- what nothing may rewrite -------------------------------------------------


def test_every_context_model_table_is_append_only(owned: m3.m2.Owned) -> None:
    seed_generated_version(owned)
    for table in TABLES:
        with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="append-only"):
            owned.connection.execute(f"UPDATE {table} SET workspace_id = workspace_id")
        with guarded(owned), pytest.raises(sqlite3.IntegrityError, match="append-only"):
            owned.connection.execute(f"DELETE FROM {table}")
