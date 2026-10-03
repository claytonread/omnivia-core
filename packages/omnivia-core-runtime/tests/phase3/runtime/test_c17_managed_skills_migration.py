"""C17 acceptance for migration 0064's managed Skills registry records.

What 0064 is: eight append-only tables and twenty-four statement triggers recording skill
drafts, their numbered revisions, proposals to the publisher queue, immutable published
versions, deprecations, install history, and the exact manifests a Run was admitted with.
It executes nothing, and no table has a column through which a skill could name authority.

What this file holds to.

*It is additive and exactly pinned.* A workspace populated at 0063 reaches 0064 with every
prior row byte-identical and the new tables empty, and the text of 0064 is pinned by hash
here and in `contracts/migrations/v1/allocations.json`.

*Writes are fenced.* Nothing is inserted outside the current fenced service writer, and
UPDATE and DELETE abort on every table for the current owner too.

*The lifecycle is enforced by the database as well as by the writer.* A version publishes
exactly the revision its proposal submitted, pins only published dependencies of other skills,
and is published once per name and version. Install history alternates. A Run's bindings are
written only under its admission and are closed by a seal.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import test_application_audit_idempotency_migration as m1
import test_c17_managed_skills_storage as c17
import test_workflow_runs_migration as m27
from omnivia_core_runtime.ownership.fencing import fenced_transaction
from omnivia_core_runtime.storage import migrations as migrations_module
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    StorageError,
    foreign_key_check,
    integrity_check,
    open_database,
)
from omnivia_core_runtime.storage.inventory import (
    capture_inventory,
    compare_inventories,
)
from omnivia_core_runtime.storage.managed_skills import RoleClosure
from omnivia_core_runtime.storage.migrations import (
    applied_migrations,
    apply_pending_migrations,
    load_migrations,
    materialise_phase0_baseline,
    read_workspace_state,
)

from omnivia_core.contracts.v1.semantics_skills import (
    ClosureEntry,
    canonical_skill_manifest,
    skill_manifest_id,
)

MIGRATION_VERSION = 64
PREDECESSOR_VERSION = 63
MIGRATION_NAME = "0064_managed_skills_registry.sql"

#: The exact text of 0064, as the runtime and `scripts/check-migration-allocations.py` both
#: hash it. Editing the migration moves this and `allocations.json` together or fails here.
PINNED_SHA256 = "fd3169622556f27ddfee9be00be25ea7125afa1301a24fbb3d80e25065797f8b"

WORKSPACE_ID = m27.WORKSPACE_ID

DRAFTS = "omnivia_skill_drafts"
REVISIONS = "omnivia_skill_draft_revisions"
PROPOSALS = "omnivia_skill_proposals"
VERSIONS = "omnivia_skill_versions"
DEPRECATIONS = "omnivia_skill_deprecations"
INSTALLS = "omnivia_skill_install_events"
BINDINGS = "omnivia_skill_run_bindings"
SEALS = "omnivia_skill_run_binding_seals"
TABLES = (DRAFTS, REVISIONS, PROPOSALS, VERSIONS, DEPRECATIONS, INSTALLS, BINDINGS, SEALS)
INDEXES = {"omnivia_idx_skill_install_events_name"}
TRIGGERS = {
    f"omnivia_guard_{table.removeprefix('omnivia_')}_{statement}"
    for table in TABLES
    for statement in ("insert", "update", "delete")
}

#: Columns that would let a skill state authority. None may exist, in any table.
FORBIDDEN_COLUMNS = {
    "permission",
    "permissions",
    "grant",
    "grants",
    "tool",
    "tools",
    "allowed_tools",
    "budget",
    "network",
    "filesystem",
    "credential",
    "credentials",
    "secret",
    "secrets",
    "escalation",
    "sandbox",
    "scopes",
    "capabilities",
}


def migration_under_test() -> Any:
    found = [m for m in load_migrations() if m.version == MIGRATION_VERSION]
    assert len(found) == 1, [m.name for m in load_migrations()]
    return found[0]


MIGRATION = migration_under_test()


@pytest.fixture
def migrated(tmp_path: Path) -> Path:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path)
    return path


@pytest.fixture
def owned(migrated: Path) -> Iterator[m1.Owned]:
    holder = m1.take_ownership(migrated)
    yield holder
    holder.connection.close()


def guarded(holder: m1.Owned) -> Any:
    return fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    )


def raw_insert(holder: m1.Owned, table: str, values: dict[str, object]) -> None:
    holder.connection.execute(
        f"INSERT INTO {table} ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
        tuple(values.values()),
    )


# --- the migration itself ------------------------------------------------------------


def test_0064_is_the_unique_consecutive_successor_to_0063() -> None:
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
    assert hashlib.sha256(MIGRATION.sql.encode("utf-8")).hexdigest() == MIGRATION.checksum


def test_the_migration_text_is_pinned_and_matches_the_allocation_authority() -> None:
    assert MIGRATION.checksum == PINNED_SHA256
    authority = json.loads(
        (
            Path(__file__).resolve().parents[5] / "contracts" / "migrations" / "v1" / "allocations.json"
        ).read_text(encoding="utf-8")
    )
    entry = next(e for e in authority["allocations"] if e["number"] == MIGRATION_VERSION)
    assert entry["filename"] == MIGRATION_NAME
    assert entry["owner"] == "Agent Runtime"
    assert entry["state"] == "candidate"
    assert entry["sha256"] == PINNED_SHA256
    assert entry["accepted_commit"] is None


def test_schema_inventory_contains_only_the_expected_new_objects(migrated: Path) -> None:
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        names = {
            kind: {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
                ).fetchall()
            }
            for kind in ("table", "index", "trigger")
        }
        assert set(TABLES) <= names["table"]
        assert INDEXES <= names["index"]
        assert TRIGGERS <= names["trigger"]
        assert len(TRIGGERS) == 24
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
    finally:
        connection.close()


def test_0064_contains_no_dml_and_drops_nothing() -> None:
    body = "\n".join(
        line for line in MIGRATION.sql.upper().splitlines() if not line.lstrip().startswith("--")
    )
    for forbidden in ("INSERT INTO", "DROP ", "ALTER "):
        # Trigger bodies only ever RAISE; the migration itself writes no row.
        assert forbidden not in body, forbidden


def test_no_table_has_a_column_through_which_a_skill_could_state_authority(
    migrated: Path,
) -> None:
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        for table in TABLES:
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            assert columns and not columns & FORBIDDEN_COLUMNS, (table, columns)
    finally:
        connection.close()


def test_a_populated_0063_head_reaches_0064_with_every_prior_fact_intact(tmp_path: Path) -> None:
    path = tmp_path / "at-0063.sqlite"
    materialise_phase0_baseline(path)
    with m1.migration_catalogue_through(PREDECESSOR_VERSION):
        m1.bootstrap_and_migrate(path)
        holder = m1.take_ownership(path)
        try:
            m27.seed_workflow_run(holder)
            before = capture_inventory(holder.connection)
        finally:
            holder.connection.close()

    with m1.migration_catalogue_through(MIGRATION_VERSION):
        maintenance = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
        try:
            state = read_workspace_state(maintenance)
            assert state is not None
            applied = apply_pending_migrations(
                maintenance,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=m1.SERVICE_INSTANCE,
                fencing_generation=state.fencing_generation,
                workspace_id=WORKSPACE_ID,
            )
            after = capture_inventory(maintenance)
            assert integrity_check(maintenance) == []
            assert foreign_key_check(maintenance) == []
        finally:
            maintenance.close()

    assert [m.version for m in applied] == [MIGRATION_VERSION]
    ledger = {"omnivia_migration_attempts", "omnivia_schema_migrations"}
    for entry in before.tables:
        if entry.name not in ledger:
            assert after.table(entry.name) == entry, entry.name
    assert set(after.table_names) - set(before.table_names) == set(TABLES)
    for table in TABLES:
        added = after.table(table)
        assert added is not None and added.row_count == 0
    differences = compare_inventories(before, after)
    assert differences and all(
        any(name in difference for name in ledger) for difference in differences
    ), differences


def test_an_edited_migration_is_refused_rather_than_accepted(migrated: Path) -> None:
    connection = open_database(migrated, OpenMode.EXCLUSIVE_MAINTENANCE)
    original = migrations_module.load_migrations
    try:
        state = read_workspace_state(connection)
        assert state is not None
        edited = tuple(
            m
            if m.version != MIGRATION_VERSION
            else migrations_module.Migration(version=m.version, name=m.name, sql=m.sql + "\n-- drift\n")
            for m in load_migrations()
        )
        migrations_module.load_migrations = lambda: edited
        with pytest.raises(StorageError, match="has changed"):
            apply_pending_migrations(
                connection,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=m1.SERVICE_INSTANCE,
                fencing_generation=state.fencing_generation,
                workspace_id=WORKSPACE_ID,
            )
    finally:
        migrations_module.load_migrations = original
        connection.close()


# --- fencing and append-only ---------------------------------------------------------


def populated(owned: m1.Owned) -> c17.Registry:
    """One row in every table: a version, its history, an install and a bound Run."""
    registry = c17.Registry(owned)
    published = registry.publish(c17.manifest())
    registry.install(published.manifest_id)
    m27.seed_workflow_run(owned)
    with registry.writer() as w:
        w.bind_run(
            run_id=m27.RUN_ID,
            roles=[store_closure(published.manifest_id)],
            bound_at_us=m27.BASE_US + 20,
            audit_ref="aud-job-run-0001",
            allocate_binding_id=lambda: "binding-1",
        )
    registry.deprecate(published.manifest_id)
    return registry


def store_closure(manifest_id: str) -> Any:
    return RoleClosure(
        c17.ROLE, (ClosureEntry(manifest_id, "triage", "1.0.0", "highest_compatible"),)
    )


def test_every_table_is_populated_by_the_fixture(owned: m1.Owned) -> None:
    populated(owned)
    for table in TABLES:
        count = owned.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert count >= 1, table


@pytest.mark.parametrize("table", TABLES)
def test_inserts_require_the_fenced_owner(owned: m1.Owned, table: str) -> None:
    with pytest.raises(sqlite3.DatabaseError, match="unguarded INSERT|not authorized"):
        owned.connection.execute(f"INSERT INTO {table} (workspace_id) VALUES (?)", (WORKSPACE_ID,))


@pytest.mark.parametrize("table", TABLES)
def test_skill_records_are_append_only(owned: m1.Owned, table: str) -> None:
    populated(owned)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"), guarded(owned):
        owned.connection.execute(f"UPDATE {table} SET workspace_id = workspace_id")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"), guarded(owned):
        owned.connection.execute(f"DELETE FROM {table}")


# --- the lifecycle, as the database sees it ------------------------------------------


def draft_to_proposal(registry: c17.Registry, name: str = "triage") -> tuple[str, str]:
    head = registry.draft(c17.manifest(name))
    registry.submit(head.draft.draft_id)
    return head.draft.draft_id, f"proposal-{head.draft.draft_id}"


def version_row(registry: c17.Registry, draft_id: str, proposal_id: str, **overrides: object) -> dict[str, Any]:
    manifest = c17.manifest()
    values: dict[str, Any] = {
        "workspace_id": WORKSPACE_ID,
        "manifest_id": skill_manifest_id(manifest),
        "skill_name": "triage",
        "version": "1.0.0",
        "manifest_json": canonical_skill_manifest(manifest),
        "draft_id": draft_id,
        "draft_revision": 1,
        "proposal_id": proposal_id,
        "review_evidence_json": json.dumps(c17.EVIDENCE),
        "published_by": c17.PUBLISHER,
        "published_at_us": c17.BASE + 500,
        "audit_ref": "audit-skills-1",
    }
    values.update(overrides)
    return values


def test_a_version_must_publish_exactly_the_submitted_revision(owned: m1.Owned) -> None:
    registry = c17.Registry(owned)
    draft_id, proposal_id = draft_to_proposal(registry)
    other = c17.manifest(instructions="Not what was submitted.")
    with pytest.raises(sqlite3.IntegrityError, match="exactly the revision its proposal submitted"), guarded(owned):
        raw_insert(
            owned,
            VERSIONS,
            version_row(
                registry,
                draft_id,
                proposal_id,
                manifest_id=skill_manifest_id(other),
                manifest_json=canonical_skill_manifest(other),
            ),
        )
    with guarded(owned):
        raw_insert(owned, VERSIONS, version_row(registry, draft_id, proposal_id))


def test_a_version_pins_only_published_dependencies_of_other_skills(owned: m1.Owned) -> None:
    registry = c17.Registry(owned)
    ghost = "skill-" + "9" * 64
    manifest = c17.manifest("app", dependencies=(("library", ghost),))
    head = registry.draft(manifest)
    registry.submit(head.draft.draft_id)
    with pytest.raises(sqlite3.IntegrityError, match="only published dependencies of other skills"), guarded(owned):
        raw_insert(
            owned,
            VERSIONS,
            version_row(
                registry,
                head.draft.draft_id,
                f"proposal-{head.draft.draft_id}",
                manifest_id=skill_manifest_id(manifest),
                skill_name="app",
                manifest_json=canonical_skill_manifest(manifest),
            ),
        )


def test_a_name_and_version_publish_once_even_when_the_content_differs(owned: m1.Owned) -> None:
    registry = c17.Registry(owned)
    registry.publish(c17.manifest())
    # The writer refuses this first; the unique key is what holds when it is bypassed.
    second = c17.manifest(instructions="Another body, the same version.")
    head = registry.draft(second)
    registry.submit(head.draft.draft_id)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"), guarded(owned):
        raw_insert(
            owned,
            VERSIONS,
            version_row(
                registry,
                head.draft.draft_id,
                f"proposal-{head.draft.draft_id}",
                manifest_id=skill_manifest_id(second),
                manifest_json=canonical_skill_manifest(second),
            ),
        )


@pytest.mark.parametrize(
    ("column", "value"),
    [("review_evidence_json", "{}"), ("review_evidence_json", "not json"), ("published_by", "")],
)
def test_malformed_version_columns_are_refused(owned: m1.Owned, column: str, value: str) -> None:
    registry = c17.Registry(owned)
    draft_id, proposal_id = draft_to_proposal(registry)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"), guarded(owned):
        raw_insert(owned, VERSIONS, version_row(registry, draft_id, proposal_id, **{column: value}))


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("manifest_id", "skill-short"),
        ("manifest_id", "skill-" + "G" * 64),
        ("manifest_id", "sha256:" + "a" * 64),
        ("reason", "Not.A.Code"),
        ("reason", "trailing."),
    ],
)
def test_malformed_deprecation_columns_are_refused(
    owned: m1.Owned, column: str, value: str
) -> None:
    registry = c17.Registry(owned)
    published = registry.publish(c17.manifest())
    row = {
        "workspace_id": WORKSPACE_ID,
        "deprecation_id": "deprecation-raw",
        "manifest_id": published.manifest_id,
        "reason": "superseded.by_newer",
        "deprecated_by": c17.PUBLISHER,
        "deprecated_at_us": c17.BASE + 900,
        "audit_ref": "audit-skills-1",
        column: value,
    }
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"), guarded(owned):
        raw_insert(owned, DEPRECATIONS, row)


def test_install_history_alternates_starts_with_an_install_and_blocks_deprecated_versions(
    owned: m1.Owned,
) -> None:
    registry = c17.Registry(owned)
    first = registry.publish(c17.manifest("triage", "1.0.0"))
    second = registry.publish(c17.manifest("triage", "2.0.0"))

    def event(manifest_id: str, kind: str, sequence: int, ident: str) -> dict[str, Any]:
        return {
            "workspace_id": WORKSPACE_ID,
            "install_event_id": ident,
            "manifest_id": manifest_id,
            "skill_name": "triage",
            "event_sequence": sequence,
            "event_kind": kind,
            "actor": c17.OPERATOR,
            "occurred_at_us": c17.BASE + 900 + sequence,
            "audit_ref": "audit-skills-1",
        }

    with pytest.raises(sqlite3.IntegrityError, match="starts with an install"), guarded(owned):
        raw_insert(owned, INSTALLS, event(first.manifest_id, "remove", 1, "e1"))
    with guarded(owned):
        raw_insert(owned, INSTALLS, event(first.manifest_id, "install", 1, "e2"))
    with pytest.raises(sqlite3.IntegrityError, match="must alternate"), guarded(owned):
        raw_insert(owned, INSTALLS, event(first.manifest_id, "install", 2, "e3"))
    with pytest.raises(sqlite3.IntegrityError, match="must be contiguous"), guarded(owned):
        raw_insert(owned, INSTALLS, event(first.manifest_id, "remove", 3, "e4"))
    registry.deprecate(second.manifest_id)
    with pytest.raises(sqlite3.IntegrityError, match="deprecated skill version cannot be installed"), guarded(owned):
        raw_insert(owned, INSTALLS, event(second.manifest_id, "install", 1, "e5"))


def test_a_run_binding_selection_must_be_installed_and_not_deprecated(owned: m1.Owned) -> None:
    registry = c17.Registry(owned)
    published = registry.publish(c17.manifest())
    m27.seed_workflow_run(owned)

    def binding(selection: str) -> dict[str, Any]:
        return {
            "workspace_id": WORKSPACE_ID,
            "run_binding_id": f"binding-{selection}",
            "run_id": m27.RUN_ID,
            "binding_position": 1,
            "role_id": c17.ROLE,
            "manifest_id": published.manifest_id,
            "skill_name": "triage",
            "selection": selection,
            "binding_digest": "sha256:" + "0" * 64,
            "bound_at_us": m27.BASE_US + 20,
            "audit_ref": "aud-job-run-0001",
        }

    with pytest.raises(sqlite3.IntegrityError, match="installed and not deprecated"), guarded(owned):
        raw_insert(owned, BINDINGS, binding("explicit"))
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"), guarded(owned):
        raw_insert(owned, BINDINGS, binding("anything_else"))
    # A dependency is pinned by the manifest that needs it, so it need not be installed.
    with guarded(owned):
        raw_insert(owned, BINDINGS, binding("dependency"))


def test_a_seal_must_count_exactly_the_bindings_it_seals(owned: m1.Owned) -> None:
    registry = c17.Registry(owned)
    published = registry.publish(c17.manifest())
    registry.install(published.manifest_id)
    m27.seed_workflow_run(owned)
    with pytest.raises(sqlite3.IntegrityError, match="count exactly the bindings it seals"), guarded(owned):
        raw_insert(
            owned,
            SEALS,
            {
                "workspace_id": WORKSPACE_ID,
                "run_id": m27.RUN_ID,
                "binding_count": 1,
                "set_digest": "sha256:" + "1" * 64,
                "sealed_at_us": m27.BASE_US + 20,
                "audit_ref": "aud-job-run-0001",
            },
        )
