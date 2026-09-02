"""T-0688 acceptance for migration 0035's Workflow Runtime hardening schema.

Slice one: what 0035 *is* as a migration, before anything writes through it. It is
the unique consecutive successor to 0034; its text is DDL only, so no row that was
durable at 0034 is rewritten and no binding is backfilled; and what it adds is
exactly seven append-only tables, nine named indexes and three guards per table.
A populated 0034 head reaching 0035 keeps every prior workflow fact, gains seven
empty tables, and stays clean under integrity_check, foreign_key_check, the
canonical schema fingerprint, reopen and a verified backup/restore round trip.

Slice two: who may write, and what a runtime binding must say. No connection that
is not the current fenced writer may insert into any of the seven tables; the
current owner records exactly one binding per run, byte for byte as it wrote it;
and every claim the binding makes about its own identity, its run, its plan and
its clock is cross-checked against what is already durable.

Slice three: the transition bundle and the journal event it carries. They are one
atomic pair -- either order inside one guarded transaction, never one half alone --
fenced by aggregate revision and linked by a hash chain from a per-Run genesis link
that is a digest like every other link, never NULL.

Slice four: what a run's journal is judged by afterwards. Parity against the
existing writer, integrity verification of the chain, quarantine and release of a
single event, and a recorded -- never enacted -- retention boundary. None of the
four changes a public Run fact, and none of them deletes a row.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
import test_application_audit_idempotency_migration as m1
import test_workflow_runs_migration as m27
from omnivia_core_runtime.ownership.fencing import (
    assert_guards_intact,
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
    fingerprint_schema,
    foreign_key_check,
    integrity_check,
    open_database,
    split_sql_statements,
)
from omnivia_core_runtime.storage.inventory import capture_inventory
from omnivia_core_runtime.storage.migrations import (
    Migration,
    applied_migrations,
    apply_pending_migrations,
    canonical_schema_fingerprint,
    canonical_schema_tables,
    load_migrations,
    materialise_phase0_baseline,
    read_workspace_state,
)

from omnivia_core.contracts.v1.semantics_workflow import (
    compute_transition_bundle_payload_digest,
    validate_runtime_definition_binding,
    validate_runtime_journal_event,
    validate_transition_bundle,
)

MIGRATION_VERSION = 35
PREDECESSOR_VERSION = 34
MIGRATION_NAME = "0035_t0688_workflow_runtime_hardening.sql"
WORKSPACE_ID = m27.WORKSPACE_ID

BINDINGS = "omnivia_workflow_runtime_bindings"
BUNDLES = "omnivia_workflow_transition_bundles"
JOURNAL = "omnivia_workflow_runtime_journal_events"
PARITY = "omnivia_workflow_transition_parity_reports"
INTEGRITY = "omnivia_workflow_journal_integrity_reports"
QUARANTINE = "omnivia_workflow_journal_quarantine_events"
RETENTION = "omnivia_workflow_journal_retention_boundaries"

TABLES = (
    BINDINGS,
    BUNDLES,
    JOURNAL,
    PARITY,
    INTEGRITY,
    QUARANTINE,
    RETENTION,
)

INDEXES = {
    "omnivia_idx_workflow_runtime_bindings_binding",
    "omnivia_idx_workflow_transition_bundles_produced",
    "omnivia_idx_workflow_runtime_journal_events_order",
    "omnivia_idx_workflow_runtime_journal_events_run_event",
    "omnivia_idx_workflow_journal_integrity_reports_run",
    "omnivia_idx_workflow_journal_integrity_reports_run_report",
    "omnivia_idx_workflow_journal_quarantine_events_event",
    "omnivia_idx_workflow_journal_retention_boundaries_run",
    "omnivia_idx_workflow_transition_parity_reports_run",
}

TRIGGERS = {
    f"omnivia_guard_{table.removeprefix('omnivia_')}_{statement}"
    for table in TABLES
    for statement in ("insert", "update", "delete")
}

# Rebuilding or writing rows, as SQL says it. Bare `INSERT`/`UPDATE`/`DELETE` are not
# usable as markers here: 0035's own trigger headers and abort messages say all three
# in prose, and none of those spellings is a statement that touches a row.
FORBIDDEN = {
    "DML": re.compile(
        r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM)\b", re.IGNORECASE
    ),
    "rebuild": re.compile(
        r"\b(ALTER\s+TABLE|DROP\s+(TABLE|INDEX|TRIGGER|VIEW))\b", re.IGNORECASE
    ),
    "rename": re.compile(r"\bRENAME\s+TO\b", re.IGNORECASE),
}

CREATES = re.compile(
    r"^CREATE\s+(TABLE|UNIQUE\s+INDEX|INDEX|TRIGGER)\s+IF\s+NOT\s+EXISTS\s+"
    r"(?P<name>\w+)\b",
    re.IGNORECASE,
)

LEDGER = {"omnivia_migration_attempts", "omnivia_schema_migrations"}


def migration_under_test() -> Migration:
    found = [m for m in load_migrations() if m.version == MIGRATION_VERSION]
    assert len(found) == 1, [m.name for m in load_migrations()]
    return found[0]


MIGRATION = migration_under_test()
STATEMENTS = tuple(split_sql_statements(MIGRATION.sql))


@pytest.fixture
def migrated(tmp_path: Path) -> Path:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
    return path


@pytest.fixture
def owned(migrated: Path) -> Iterator[m1.Owned]:
    holder = m1.take_ownership(migrated)
    yield holder
    holder.connection.close()


def named_objects(connection: object) -> dict[str, set[str]]:
    """Every table, index and trigger the schema currently names."""
    return {
        kind: {
            str(row[0])
            for row in connection.execute(  # type: ignore[attr-defined]
                "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
            )
        }
        for kind in ("table", "index", "trigger")
    }


def advance_to_head(path: Path) -> list[Migration]:
    """Advance an already-bootstrapped workspace through the real migrator."""
    maintenance = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        state = read_workspace_state(maintenance)
        assert state is not None
        return apply_pending_migrations(
            maintenance,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            service_instance_id=m1.SERVICE_INSTANCE,
            fencing_generation=state.fencing_generation,
            workspace_id=WORKSPACE_ID,
        )
    finally:
        maintenance.close()


# --- what 0035 is, as text --------------------------------------------------------


def test_0035_is_the_unique_consecutive_successor_to_0034() -> None:
    migrations = load_migrations()
    versions = [migration.version for migration in migrations]
    assert versions == sorted(versions)
    assert versions[:MIGRATION_VERSION] == list(range(1, MIGRATION_VERSION + 1))
    assert versions.count(MIGRATION_VERSION) == 1
    assert [m.name for m in migrations if m.version == MIGRATION_VERSION] == [
        MIGRATION_NAME
    ]
    assert MIGRATION.version == PREDECESSOR_VERSION + 1


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


def test_every_statement_creates_a_new_object_and_none_writes_or_rebuilds() -> None:
    """DDL only, additive only: no backfill, and no existing object touched.

    Each statement is `CREATE ... IF NOT EXISTS`, and the whole text -- trigger
    bodies included -- contains no row-writing and no rebuilding statement.
    """
    created: dict[str, list[str]] = {"TABLE": [], "INDEX": [], "TRIGGER": []}
    for statement in STATEMENTS:
        match = CREATES.match(statement.strip())
        assert match is not None, statement[:120]
        kind = match.group(1).upper().removeprefix("UNIQUE ").strip()
        created[kind].append(match.group("name"))

    for label, pattern in FORBIDDEN.items():
        assert pattern.search(MIGRATION.sql) is None, label

    assert created["TABLE"] == list(TABLES)
    assert set(created["INDEX"]) == INDEXES
    assert set(created["TRIGGER"]) == TRIGGERS
    assert len(created["INDEX"]) == 9
    assert len(created["TRIGGER"]) == len(TABLES) * 3 == 21


# --- what 0035 adds to a database -------------------------------------------------


def test_applying_0035_adds_exactly_the_expected_objects(tmp_path: Path) -> None:
    path = tmp_path / "at-0034.sqlite"
    materialise_phase0_baseline(path)
    with m1.migration_catalogue_through(PREDECESSOR_VERSION):
        m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
        connection = open_database(path, OpenMode.EPHEMERAL)
        try:
            before = named_objects(connection)
        finally:
            connection.close()

    assert [m.version for m in advance_to_head(path)] == [MIGRATION_VERSION]

    connection = open_database(path, OpenMode.EPHEMERAL)
    try:
        after = named_objects(connection)
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
    finally:
        connection.close()

    assert after["table"] - before["table"] == set(TABLES)
    assert after["trigger"] - before["trigger"] == TRIGGERS
    # SQLite names an implicit index for each UNIQUE table constraint; only the
    # nine declared by name are this migration's own inventory.
    added_indexes = after["index"] - before["index"]
    assert INDEXES <= added_indexes
    assert {name for name in added_indexes if not name.startswith("sqlite_")} == INDEXES


def test_a_populated_0034_head_keeps_every_prior_fact_and_gains_no_binding(
    tmp_path: Path,
) -> None:
    """0035 over data: prior rows byte for byte, and seven empty new tables."""
    path = tmp_path / "populated-at-0034.sqlite"
    materialise_phase0_baseline(path)
    with m1.migration_catalogue_through(PREDECESSOR_VERSION):
        m1.bootstrap_and_migrate(path, workspace_id=WORKSPACE_ID)
        holder = m1.take_ownership(path)
        try:
            m27.seed_every_table(holder)
            before = capture_inventory(holder.connection)
            runs = holder.connection.execute(
                f"SELECT * FROM {m27.RUNS} ORDER BY run_id"
            ).fetchall()
            plans = holder.connection.execute(
                f"SELECT * FROM {m27.PLANS} ORDER BY workflow_id"
            ).fetchall()
        finally:
            holder.connection.close()

    assert runs and plans
    assert [m.version for m in advance_to_head(path)] == [MIGRATION_VERSION]

    connection = open_database(path, OpenMode.EPHEMERAL)
    try:
        after = capture_inventory(connection)
        for entry in before.tables:
            if entry.name in LEDGER:
                continue
            assert after.table(entry.name) == entry, entry.name
        assert set(after.table_names) - set(before.table_names) == set(TABLES)
        for table in TABLES:
            added = after.table(table)
            assert added is not None and added.row_count == 0, table
        assert (
            connection.execute(f"SELECT * FROM {m27.RUNS} ORDER BY run_id").fetchall()
            == runs
        )
        assert (
            connection.execute(
                f"SELECT * FROM {m27.PLANS} ORDER BY workflow_id"
            ).fetchall()
            == plans
        )
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
    finally:
        connection.close()


# --- the database stays sound afterwards ------------------------------------------


def test_the_canonical_schema_and_guards_cover_the_new_objects(migrated: Path) -> None:
    assert set(TABLES) <= canonical_schema_tables()
    connection = open_database(migrated, OpenMode.EPHEMERAL)
    try:
        named = named_objects(connection)
        assert set(TABLES) <= named["table"]
        assert INDEXES <= named["index"]
        assert TRIGGERS <= named["trigger"]
        verify_fingerprint(connection, canonical_schema_fingerprint())
        assert_guards_intact(connection)
    finally:
        connection.close()


def test_reopening_a_populated_workspace_stays_clean(owned: m1.Owned) -> None:
    m27.seed_every_table(owned)
    populated = capture_inventory(owned.connection)
    owned.connection.close()

    reopened = open_database(owned.path, OpenMode.EPHEMERAL)
    try:
        assert integrity_check(reopened) == []
        assert foreign_key_check(reopened) == []
        assert fingerprint_schema(reopened).matches(canonical_schema_fingerprint())
        assert capture_inventory(reopened) == populated
    finally:
        reopened.close()


def test_a_verified_backup_restores_the_populated_schema_intact(
    owned: m1.Owned, tmp_path: Path
) -> None:
    m27.seed_every_table(owned)
    populated = capture_inventory(owned.connection)
    owned.connection.close()

    installation = InstallationLayout(root=tmp_path / "installation-state")
    backup = create_verified_backup(
        owned.path,
        installation,
        workspace_id=WORKSPACE_ID,
        attempt_id=new_attempt_id(),
    )
    assert backup.verified

    restored = tmp_path / "restored.sqlite"
    restore_backup(backup.path, restored)
    connection = open_database(restored, OpenMode.EPHEMERAL)
    try:
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
        assert fingerprint_schema(connection).matches(canonical_schema_fingerprint())
        assert capture_inventory(connection) == populated
        for table in TABLES:
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (
                0,
            )
    finally:
        connection.close()


# --- slice two: ownership, and what a binding must say ----------------------------

BINDING_ID = "binding-0035-one"
BOUND_AT_US = m27.BASE_US + 100
BOUND_AT = "2026-01-01T00:00:00Z"

#: Every layer that can refuse a write made without current authority. Which one
#: answers depends on where the writer stands, and none of them is a CHECK or a
#: foreign key: matching this and nothing else is how these tests show the guard --
#: not a constraint behind it -- is what refused.
UNGUARDED = "not authorized|unguarded INSERT on"
APPEND_ONLY = "append-only; (UPDATE|DELETE) is never permitted"
CONSTRAINT = "CHECK constraint failed|malformed JSON"
NOT_NULL = "NOT NULL constraint failed"


def digest_of(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical(document: Mapping[str, object]) -> str:
    """The compact, key-sorted JSON the schema will accept as canonical."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def binding_document(**overrides: object) -> dict[str, object]:
    """A `RuntimeDefinitionBinding` pinning m27's seeded run to m27's seeded plan."""
    document: dict[str, object] = {
        "bindingSchemaVersion": "1.0.0",
        "bindingId": BINDING_ID,
        "workflowId": m27.WORKFLOW_ID,
        "workflowVersion": m27.WORKFLOW_VERSION,
        "releaseRef": {"releaseId": "release-0035"},
        "definitionDigest": m27.DEFINITION_HASH,
        "executionProfileDigest": "sha256:" + "3" * 64,
        "effectivePolicyDigest": "sha256:" + "4" * 64,
        "componentImplementationDigests": {"component-echo": "sha256:" + "5" * 64},
        "resourceBindingSnapshots": [],
        "boundAt": BOUND_AT,
        "boundBy": {"principalId": "core-service"},
    }
    document.update(overrides)
    return document


def binding_row(
    *,
    document: Mapping[str, object] | None = None,
    text: str | None = None,
    **overrides: object,
) -> dict[str, object]:
    body = canonical(document or binding_document()) if text is None else text
    row: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "run_id": m27.RUN_ID,
        "binding_id": BINDING_ID,
        "binding_schema_version": 1,
        "binding_json": body,
        "binding_digest": digest_of(body),
        "binding_byte_length": len(body.encode("utf-8")),
        "bound_at_us": BOUND_AT_US,
    }
    row.update(overrides)
    return row


def bind(holder: m1.Owned, row: dict[str, object]) -> None:
    with m27.guarded(holder):
        m27.insert(holder, BINDINGS, row)


def test_the_binding_this_slice_writes_is_the_contract_record_it_claims_to_be() -> None:
    """The row's JSON is a valid `RuntimeDefinitionBinding`, in canonical form."""
    document = binding_document()
    validate_runtime_definition_binding(document)
    body = canonical(document)
    connection = sqlite3.connect(":memory:")
    try:
        assert connection.execute("SELECT json(?)", (body,)).fetchone() == (body,)
    finally:
        connection.close()


@pytest.mark.parametrize("table", TABLES)
def test_a_connection_without_current_authority_cannot_insert_anywhere(
    owned: m1.Owned, table: str
) -> None:
    """Outside a fenced transaction, every one of the seven tables refuses.

    The column list is deliberately minimal, because the answer must not depend on
    the row: authority is settled before any constraint on it is, and a refusal
    naming a CHECK or a foreign key would fail this assertion rather than pass it.
    """
    m27.seed_every_table(owned)
    with pytest.raises(sqlite3.DatabaseError, match=UNGUARDED):
        owned.connection.execute(
            f"INSERT INTO {table} (workspace_id, run_id) VALUES (?, ?)",
            (WORKSPACE_ID, m27.RUN_ID),
        )


def test_the_current_owner_records_one_binding_and_reads_it_back_byte_for_byte(
    owned: m1.Owned,
) -> None:
    m27.seed_every_table(owned)
    row = binding_row()
    bind(owned, row)

    stored = owned.connection.execute(
        f"SELECT binding_id, binding_schema_version, binding_json, binding_digest, "
        f"binding_byte_length, bound_at_us FROM {BINDINGS} "
        "WHERE workspace_id = ? AND run_id = ?",
        (WORKSPACE_ID, m27.RUN_ID),
    ).fetchall()
    assert stored == [
        (
            BINDING_ID,
            1,
            row["binding_json"],
            row["binding_digest"],
            row["binding_byte_length"],
            BOUND_AT_US,
        )
    ]
    assert json.loads(str(row["binding_json"])) == binding_document()
    assert integrity_check(owned.connection) == []
    assert foreign_key_check(owned.connection) == []


DRIFT: dict[str, tuple[dict[str, object], str]] = {
    "workflow id": (
        binding_row(document=binding_document(workflowId="workflow-other")),
        "must name the run it binds",
    ),
    "workflow version": (
        binding_row(document=binding_document(workflowVersion="2.0.0")),
        "must name the run it binds",
    ),
    "definition digest": (
        binding_row(document=binding_document(definitionDigest="sha256:" + "9" * 64)),
        "must name the run it binds",
    ),
    "binding id": (binding_row(binding_id="binding-other"), CONSTRAINT),
    "schema version": (binding_row(binding_schema_version=2), CONSTRAINT),
    "json validity": (binding_row(text='{"bindingId":'), CONSTRAINT),
    "canonical shape": (
        binding_row(text=json.dumps(binding_document(), sort_keys=True)),
        CONSTRAINT,
    ),
    "byte length": (binding_row(binding_byte_length=1), CONSTRAINT),
    "clock": (binding_row(bound_at_us=m27.BASE_US + 1), "cannot predate the run"),
}


@pytest.mark.parametrize("case", sorted(DRIFT))
def test_a_binding_that_drifts_from_what_is_already_durable_refuses(
    owned: m1.Owned, case: str
) -> None:
    """Identity, plan, shape and clock: each claim is checked against the workspace."""
    row, expected = DRIFT[case]
    m27.seed_every_table(owned)
    with m27.guarded(owned), pytest.raises(sqlite3.DatabaseError, match=expected):
        m27.insert(owned, BINDINGS, row)
    assert owned.connection.execute(f"SELECT COUNT(*) FROM {BINDINGS}").fetchone() == (
        0,
    )


def test_a_run_may_never_hold_a_second_binding(owned: m1.Owned) -> None:
    m27.seed_every_table(owned)
    bind(owned, binding_row())
    second = binding_row(
        document=binding_document(bindingId="binding-0035-two"),
        binding_id="binding-0035-two",
    )
    with m27.guarded(owned), pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        m27.insert(owned, BINDINGS, second)
    assert owned.connection.execute(
        f"SELECT binding_id FROM {BINDINGS}"
    ).fetchall() == [(BINDING_ID,)]


@pytest.mark.parametrize(
    "statement",
    [
        f"UPDATE {BINDINGS} SET bound_at_us = bound_at_us + 1",
        f"DELETE FROM {BINDINGS}",
    ],
)
def test_a_recorded_binding_is_immutable_even_for_its_current_owner(
    owned: m1.Owned, statement: str
) -> None:
    m27.seed_every_table(owned)
    row = binding_row()
    bind(owned, row)
    with m27.guarded(owned), pytest.raises(sqlite3.DatabaseError, match=APPEND_ONLY):
        owned.connection.execute(statement)
    assert owned.connection.execute(
        f"SELECT binding_json, bound_at_us FROM {BINDINGS}"
    ).fetchall() == [(row["binding_json"], BOUND_AT_US)]


# --- slice three: the bundle and the journal event it carries ---------------------

OTHER_RUN_ID = "run-0035-other"
OTHER_JOB_ID = "job-run-0035-other"

#: Every event names a link, in its JSON and in its `previous_link_digest` column
#: alike, because the contract record has no absent form for one. The run's first
#: event names this genesis digest; the real writer derives it per Run, and SQL
#: checks only that it is a sha256 digest.
GENESIS_LINK = "sha256:" + "0" * 64
RECORDED_AT = "2026-01-01T00:00:01Z"

PAIR_MISSING = "FOREIGN KEY constraint failed"
BUNDLE_FOR_EVENT = "sequence must equal its bundle's expected revision"


def bundle_id_for(sequence: int) -> str:
    return f"bundle-0035-{sequence}"


def event_id_for(sequence: int) -> str:
    return f"event-0035-{sequence}"


def event_payload_document(sequence: int) -> dict[str, object]:
    return {"runId": m27.RUN_ID, "sequence": sequence, "transition": "recorded"}


def event_document(sequence: int, previous_link: str | None) -> dict[str, object]:
    return {
        "eventId": event_id_for(sequence),
        "runId": m27.RUN_ID,
        "sequence": sequence,
        "previousIntegrityLink": previous_link or GENESIS_LINK,
        "eventKind": "runtime.transition.recorded",
        "recordedAt": RECORDED_AT,
        "payloadDigest": digest_of(canonical(event_payload_document(sequence))),
    }


def bundle_document(
    sequence: int = 0, previous_link: str | None = None
) -> dict[str, object]:
    """A `RuntimeTransitionBundle` transitioning m27's seeded run to `sequence + 1`."""
    document: dict[str, object] = {
        "bundleSchemaVersion": "1.0.0",
        "bundleId": bundle_id_for(sequence),
        "runId": m27.RUN_ID,
        "expectedAggregateRevision": sequence,
        "event": event_document(sequence, previous_link),
        "producedAggregateRevision": sequence + 1,
    }
    document["payloadDigest"] = compute_transition_bundle_payload_digest(document)
    return document


def bundle_row(
    sequence: int = 0, previous_link: str | None = None, **overrides: object
) -> dict[str, object]:
    document = bundle_document(sequence, previous_link)
    body = canonical(document)
    row: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "run_id": m27.RUN_ID,
        "bundle_id": bundle_id_for(sequence),
        "binding_id": BINDING_ID,
        "expected_revision": sequence,
        "produced_revision": sequence + 1,
        "payload_digest": document["payloadDigest"],
        "bundle_json": body,
        "bundle_digest": digest_of(body),
        "bundle_byte_length": len(body.encode("utf-8")),
        "recorded_at_us": BOUND_AT_US + 10 + sequence,
    }
    row.update(overrides)
    return row


def event_row(
    sequence: int = 0, previous_link: str | None = None, **overrides: object
) -> dict[str, object]:
    payload = canonical(event_payload_document(sequence))
    body = canonical(event_document(sequence, previous_link))
    row: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "run_id": m27.RUN_ID,
        "bundle_id": bundle_id_for(sequence),
        "event_id": event_id_for(sequence),
        "sequence": sequence,
        "previous_link_digest": previous_link or GENESIS_LINK,
        "payload_digest": digest_of(payload),
        "event_json": body,
        "event_digest": digest_of(body),
        "event_byte_length": len(body.encode("utf-8")),
        "event_payload_json": payload,
        "event_payload_digest": digest_of(payload),
        "event_payload_byte_length": len(payload.encode("utf-8")),
        "recorded_at_us": BOUND_AT_US + 10 + sequence,
    }
    row.update(overrides)
    return row


@pytest.fixture
def bound(owned: m1.Owned) -> m1.Owned:
    """m27's seeded workspace, with a binding, and a second run holding none."""
    m27.seed_every_table(owned)
    bind(owned, binding_row())
    m27.seed_runtime_run(owned, run_id=OTHER_RUN_ID, job_id=OTHER_JOB_ID)
    with m27.guarded(owned):
        m27.insert(owned, m27.RUNS, m27.workflow_run_row(run_id=OTHER_RUN_ID))
    return owned


def record(holder: m1.Owned, *rows: tuple[str, dict[str, object]]) -> None:
    """Write rows as one guarded transaction, in exactly the order given."""
    with m27.guarded(holder):
        for table, row in rows:
            m27.insert(holder, table, row)


def counts(holder: m1.Owned) -> tuple[int, int]:
    return (
        holder.connection.execute(f"SELECT COUNT(*) FROM {BUNDLES}").fetchone()[0],
        holder.connection.execute(f"SELECT COUNT(*) FROM {JOURNAL}").fetchone()[0],
    )


def test_the_pair_this_slice_writes_is_the_contract_record_it_claims_to_be() -> None:
    """Bundle and event are valid contract records, and each body is canonical."""
    document = bundle_document()
    validate_transition_bundle(document)
    validate_runtime_journal_event(document["event"], run_id=m27.RUN_ID)
    row = event_row()
    connection = sqlite3.connect(":memory:")
    try:
        for body in (
            canonical(document),
            str(row["event_json"]),
            str(row["event_payload_json"]),
        ):
            assert connection.execute("SELECT json(?)", (body,)).fetchone() == (body,)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "event_first", [False, True], ids=["bundle first", "event first"]
)
def test_the_current_owner_records_a_bundle_and_its_event_atomically(
    bound: m1.Owned, event_first: bool
) -> None:
    """One guarded transaction, either order: the deferred pair commits together."""
    bundle, event = bundle_row(), event_row()
    halves = [(BUNDLES, bundle), (JOURNAL, event)]
    record(bound, *(reversed(halves) if event_first else halves))

    assert bound.connection.execute(
        f"SELECT bundle_id, binding_id, expected_revision, produced_revision, "
        f"payload_digest, bundle_json, bundle_digest, bundle_byte_length "
        f"FROM {BUNDLES}"
    ).fetchall() == [
        (
            bundle["bundle_id"],
            BINDING_ID,
            0,
            1,
            bundle["payload_digest"],
            bundle["bundle_json"],
            bundle["bundle_digest"],
            bundle["bundle_byte_length"],
        )
    ]
    assert bound.connection.execute(
        f"SELECT event_id, bundle_id, sequence, previous_link_digest, payload_digest, "
        f"event_json, event_digest, event_byte_length, event_payload_json, "
        f"event_payload_digest, event_payload_byte_length FROM {JOURNAL}"
    ).fetchall() == [
        (
            event["event_id"],
            event["bundle_id"],
            0,
            GENESIS_LINK,
            event["payload_digest"],
            event["event_json"],
            event["event_digest"],
            event["event_byte_length"],
            event["event_payload_json"],
            event["event_payload_digest"],
            event["event_payload_byte_length"],
        )
    ]
    # The two digest columns agree because Python computed one value and wrote it
    # twice; the schema compares the columns, and recomputes neither.
    assert event["payload_digest"] == event["event_payload_digest"]
    assert integrity_check(bound.connection) == []
    assert foreign_key_check(bound.connection) == []


@pytest.mark.parametrize("table", [BUNDLES, JOURNAL])
def test_neither_half_of_the_pair_may_commit_alone(bound: m1.Owned, table: str) -> None:
    """The deferred foreign key answers at COMMIT, and nothing durable survives it."""
    row = bundle_row() if table == BUNDLES else event_row()
    with pytest.raises(sqlite3.IntegrityError, match=PAIR_MISSING):
        record(bound, (table, row))
    # A COMMIT refused by a deferred foreign key leaves its transaction open.
    bound.connection.execute("ROLLBACK")
    assert counts(bound) == (0, 0)


def test_the_chain_continues_at_the_next_revision_and_links_to_its_predecessor(
    bound: m1.Owned,
) -> None:
    first = event_row()
    record(bound, (BUNDLES, bundle_row()), (JOURNAL, first))
    link = str(first["event_digest"])
    record(
        bound,
        (BUNDLES, bundle_row(1, link)),
        (JOURNAL, event_row(1, link)),
    )

    assert bound.connection.execute(
        f"SELECT expected_revision, produced_revision FROM {BUNDLES} "
        "ORDER BY produced_revision"
    ).fetchall() == [(0, 1), (1, 2)]
    assert bound.connection.execute(
        f"SELECT sequence, previous_link_digest FROM {JOURNAL} ORDER BY sequence"
    ).fetchall() == [(0, GENESIS_LINK), (1, link)]
    assert foreign_key_check(bound.connection) == []


def test_a_replayed_bundle_id_cannot_create_a_second_durable_pair(
    bound: m1.Owned,
) -> None:
    """The replay repeats a revision the run has already produced, and is refused."""
    record(bound, (BUNDLES, bundle_row()), (JOURNAL, event_row()))
    with pytest.raises(sqlite3.DatabaseError, match="expected revision must continue"):
        record(bound, (BUNDLES, bundle_row()), (JOURNAL, event_row()))
    assert counts(bound) == (1, 1)
    # Re-aimed at the revision the run is actually at, it disagrees with the event
    # already paired to that same bundle id.
    with pytest.raises(sqlite3.DatabaseError, match=BUNDLE_FOR_EVENT):
        record(bound, (BUNDLES, bundle_row(expected_revision=1, produced_revision=2)))
    assert counts(bound) == (1, 1)


def test_a_different_bundle_at_a_revision_already_produced_refuses(
    bound: m1.Owned,
) -> None:
    record(bound, (BUNDLES, bundle_row()), (JOURNAL, event_row()))
    rival = bundle_row(bundle_id="bundle-0035-rival")
    with pytest.raises(sqlite3.DatabaseError, match="expected revision must continue"):
        record(bound, (BUNDLES, rival))
    assert counts(bound) == (1, 1)


BUNDLE_REFUSALS: dict[str, tuple[dict[str, object], str]] = {
    "skipped revision": (
        {"expected_revision": 1, "produced_revision": 2},
        "expected revision must continue",
    ),
    "negative revision": ({"produced_revision": -1}, CONSTRAINT),
    "wrong produced revision": ({"produced_revision": 3}, CONSTRAINT),
    "wrong binding id": (
        {"binding_id": "binding-0035-other"},
        "must name the binding its run holds",
    ),
}


@pytest.mark.parametrize("case", sorted(BUNDLE_REFUSALS))
def test_a_bundle_that_misstates_its_fence_refuses(bound: m1.Owned, case: str) -> None:
    overrides, expected = BUNDLE_REFUSALS[case]
    with pytest.raises(sqlite3.DatabaseError, match=expected):
        record(bound, (BUNDLES, bundle_row(**overrides)))
    assert counts(bound) == (0, 0)


@pytest.mark.parametrize(
    "overrides",
    [{"bundle_id": bundle_id_for(9)}, {"run_id": OTHER_RUN_ID}],
    ids=["unknown bundle", "another run"],
)
def test_an_event_that_names_no_bundle_of_its_run_never_commits(
    bound: m1.Owned, overrides: dict[str, object]
) -> None:
    """No bundle to disagree with, so the deferred pair is what refuses -- at COMMIT."""
    with pytest.raises(sqlite3.IntegrityError, match=PAIR_MISSING):
        record(bound, (BUNDLES, bundle_row()), (JOURNAL, event_row(**overrides)))
    bound.connection.execute("ROLLBACK")
    assert counts(bound) == (0, 0)


def test_an_event_whose_sequence_is_not_its_bundles_expected_revision_refuses(
    bound: m1.Owned,
) -> None:
    """Sequence 0 is contiguous, but the bundle it names transitions revision 1."""
    link = str(event_row()["event_digest"])
    with pytest.raises(sqlite3.DatabaseError, match=BUNDLE_FOR_EVENT):
        record(
            bound,
            (BUNDLES, bundle_row()),
            (BUNDLES, bundle_row(1, link)),
            (JOURNAL, event_row(bundle_id=bundle_id_for(1))),
        )
    assert counts(bound) == (0, 0)


def test_a_bundle_that_disagrees_with_an_event_already_written_refuses(
    bound: m1.Owned,
) -> None:
    """The same pairing rule, answered by whichever half of the pair arrives second."""
    first = event_row(bundle_id=bundle_id_for(1))
    with pytest.raises(sqlite3.DatabaseError, match=BUNDLE_FOR_EVENT):
        record(
            bound,
            (JOURNAL, first),
            (
                JOURNAL,
                event_row(1, str(first["event_digest"]), bundle_id=bundle_id_for(0)),
            ),
            (BUNDLES, bundle_row()),
        )
    assert counts(bound) == (0, 0)


def test_an_event_whose_payload_digest_is_not_its_payloads_refuses(
    bound: m1.Owned,
) -> None:
    with pytest.raises(sqlite3.DatabaseError, match=CONSTRAINT):
        record(
            bound,
            (BUNDLES, bundle_row()),
            (JOURNAL, event_row(payload_digest="sha256:" + "7" * 64)),
        )
    assert counts(bound) == (0, 0)


def test_a_journal_sequence_gap_refuses(bound: m1.Owned) -> None:
    """Two bundles, but only the second one's event: sequence 1 has no predecessor."""
    link = str(event_row()["event_digest"])
    with pytest.raises(sqlite3.DatabaseError, match="contiguous from zero"):
        record(
            bound,
            (BUNDLES, bundle_row()),
            (BUNDLES, bundle_row(1, link)),
            (JOURNAL, event_row(1, link)),
        )
    assert counts(bound) == (0, 0)


def test_an_event_whose_previous_link_is_not_its_predecessor_refuses(
    bound: m1.Owned,
) -> None:
    record(bound, (BUNDLES, bundle_row()), (JOURNAL, event_row()))
    wrong = "sha256:" + "8" * 64
    with pytest.raises(sqlite3.DatabaseError, match="previous link must be"):
        record(
            bound,
            (BUNDLES, bundle_row(1, wrong)),
            (JOURNAL, event_row(1, wrong)),
        )
    assert counts(bound) == (1, 1)


@pytest.mark.parametrize(
    "previous_link,expected",
    [
        (None, NOT_NULL),
        ("sha256:" + "z" * 64, CONSTRAINT),
        ("sha256:" + "0" * 63, CONSTRAINT),
    ],
    ids=["null genesis link", "non-hex genesis link", "short genesis link"],
)
def test_a_genesis_event_without_a_well_formed_link_refuses(
    bound: m1.Owned, previous_link: str | None, expected: str
) -> None:
    """Sequence zero carries a digest like every other event -- NULL is not a link.

    The contract record's `previousIntegrityLink` has no absent form, so a run's
    first event must name its per-Run genesis digest. SQL cannot recompute that
    value, but it does refuse anything that is not a sha256 digest, and nothing
    durable survives the refusal.
    """
    with pytest.raises(sqlite3.DatabaseError, match=expected):
        record(
            bound,
            (BUNDLES, bundle_row()),
            (JOURNAL, event_row(previous_link_digest=previous_link)),
        )
    assert counts(bound) == (0, 0)


@pytest.mark.parametrize(
    "statement",
    [
        f"UPDATE {BUNDLES} SET produced_revision = produced_revision + 1",
        f"DELETE FROM {BUNDLES}",
        f"UPDATE {JOURNAL} SET previous_link_digest = NULL",
        f"DELETE FROM {JOURNAL}",
    ],
)
def test_a_recorded_pair_is_immutable_even_for_its_current_owner(
    bound: m1.Owned, statement: str
) -> None:
    record(bound, (BUNDLES, bundle_row()), (JOURNAL, event_row()))
    with m27.guarded(bound), pytest.raises(sqlite3.DatabaseError, match=APPEND_ONLY):
        bound.connection.execute(statement)
    assert counts(bound) == (1, 1)


# --- slice four: parity, integrity, quarantine and retention ----------------------

PARITY_ID = "parity-0035-one"
INTEGRITY_ID = "integrity-0035-one"
BOUNDARY_ID = "boundary-0035-one"
RETENTION_AUDIT = "audit-retention"
AUDIT_REQUIRED = "audit reference must belong to its workspace"

#: A digest no bundle in this suite derives, so a report naming it has diverged.
WRITER_DIGEST = "sha256:" + "a" * 64
BUNDLE_DIGEST = str(bundle_row()["bundle_digest"])

#: The second run's own binding, pair and integrity report. A quarantine naming any
#: of these from the first run is naming another run's evidence.
OTHER_BINDING_ID = "binding-0035-other"
OTHER_BUNDLE_ID = "bundle-0035-other"
OTHER_EVENT_ID = "event-0035-other"
OTHER_REPORT_ID = "integrity-0035-other"

DIAGNOSTIC_FOR = {
    "verified": None,
    "sequence_gap": "RT_JOURNAL_SEQUENCE_GAP",
    "integrity_failure": "RT_JOURNAL_INTEGRITY_FAILURE",
}


def parity_row(status: str = "match", **overrides: object) -> dict[str, object]:
    """A parity report on the run's first bundle, consistent with `status`."""
    body = canonical(
        {
            "reportId": PARITY_ID,
            "runId": m27.RUN_ID,
            "bundleId": bundle_id_for(0),
            "status": status,
        }
    )
    row: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "report_id": PARITY_ID,
        "run_id": m27.RUN_ID,
        "bundle_id": bundle_id_for(0),
        "existing_writer_digest": (
            BUNDLE_DIGEST if status == "match" else WRITER_DIGEST
        ),
        "bundle_derived_digest": BUNDLE_DIGEST,
        "status": status,
        "report_json": body,
        "report_digest": digest_of(body),
        "report_byte_length": len(body.encode("utf-8")),
        "recorded_at_us": BOUND_AT_US + 20,
    }
    row.update(overrides)
    return row


def integrity_row(outcome: str = "verified", **overrides: object) -> dict[str, object]:
    """One verification pass over one rollout stage, in a closed combination."""
    body = canonical(
        {"reportId": INTEGRITY_ID, "runId": m27.RUN_ID, "outcome": outcome}
    )
    row: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "report_id": INTEGRITY_ID,
        "run_id": m27.RUN_ID,
        "rollout_stage": "R0",
        "outcome": outcome,
        "first_affected_sequence": None if outcome == "verified" else 0,
        "diagnostic": DIAGNOSTIC_FOR.get(outcome),
        "observed_head": 0,
        "report_json": body,
        "report_digest": digest_of(body),
        "report_byte_length": len(body.encode("utf-8")),
        "verified_at_us": BOUND_AT_US + 30,
    }
    row.update(overrides)
    return row


def quarantine_row(
    sequence: int = 0, action: str = "quarantined", **overrides: object
) -> dict[str, object]:
    held = action == "quarantined"
    row: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "run_id": m27.RUN_ID,
        "disposition_sequence": sequence,
        "event_id": event_id_for(0),
        "action": action,
        "integrity_report_id": INTEGRITY_ID if held else None,
        "diagnostic": "RT_JOURNAL_QUARANTINED" if held else None,
        "deciding_actor": None if held else "core-service",
        "reason": None if held else "operator_release",
        "recorded_at_us": BOUND_AT_US + 40 + sequence,
    }
    row.update(overrides)
    return row


def retention_row(**overrides: object) -> dict[str, object]:
    """A boundary that removes nothing, and so leaves the run resumable."""
    row: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "boundary_id": BOUNDARY_ID,
        "run_id": m27.RUN_ID,
        "first_removed_sequence": None,
        "last_removed_sequence": None,
        "resumable_after": 1,
        "policy_ref": "policy-journal-90d",
        "evidence_ref": "evidence-0035",
        "audit_ref": RETENTION_AUDIT,
        "recorded_at_us": BOUND_AT_US + 50,
    }
    row.update(overrides)
    return row


@pytest.fixture
def paired(bound: m1.Owned) -> m1.Owned:
    """A bound run holding its first bundle and the journal event paired to it."""
    record(bound, (BUNDLES, bundle_row()), (JOURNAL, event_row()))
    return bound


@pytest.fixture
def flagged(paired: m1.Owned) -> m1.Owned:
    """That run, with the integrity report a quarantine may cite."""
    record(paired, (INTEGRITY, integrity_row("integrity_failure")))
    return paired


def seed_other_run(holder: m1.Owned) -> None:
    """Give the second run its own binding, pair and integrity report."""
    bind(
        holder,
        binding_row(
            document=binding_document(bindingId=OTHER_BINDING_ID),
            run_id=OTHER_RUN_ID,
            binding_id=OTHER_BINDING_ID,
        ),
    )
    record(
        holder,
        (
            BUNDLES,
            bundle_row(
                run_id=OTHER_RUN_ID,
                bundle_id=OTHER_BUNDLE_ID,
                binding_id=OTHER_BINDING_ID,
            ),
        ),
        (
            JOURNAL,
            event_row(
                run_id=OTHER_RUN_ID,
                bundle_id=OTHER_BUNDLE_ID,
                event_id=OTHER_EVENT_ID,
            ),
        ),
        (
            INTEGRITY,
            integrity_row(
                "integrity_failure", run_id=OTHER_RUN_ID, report_id=OTHER_REPORT_ID
            ),
        ),
    )


def rows_of(holder: m1.Owned, table: str, order: str) -> list[tuple[object, ...]]:
    return holder.connection.execute(
        f"SELECT * FROM {table} ORDER BY {order}"
    ).fetchall()


def public_run_state(holder: m1.Owned) -> list[list[tuple[object, ...]]]:
    """Everything a reader of the public Run sees, as rows."""
    return [
        rows_of(holder, m27.RUNS, "run_id"),
        rows_of(holder, m27.COMPLETIONS, "run_id"),
        rows_of(holder, m27.OBSERVATIONS, "run_id, step_id, observation_kind"),
    ]


# --- parity against the existing writer -------------------------------------------


@pytest.mark.parametrize("status", ["match", "diverged"])
def test_a_parity_report_says_exactly_what_the_two_digests_show(
    paired: m1.Owned, status: str
) -> None:
    """`match` iff the digests agree bit for bit, and the body persists exactly."""
    row = parity_row(status)
    record(paired, (PARITY, row))
    assert (row["existing_writer_digest"] == row["bundle_derived_digest"]) is (
        status == "match"
    )
    assert paired.connection.execute(
        f"SELECT report_id, run_id, bundle_id, existing_writer_digest, "
        f"bundle_derived_digest, status, report_json, report_digest, "
        f"report_byte_length, recorded_at_us FROM {PARITY}"
    ).fetchall() == [
        (
            PARITY_ID,
            m27.RUN_ID,
            bundle_id_for(0),
            row["existing_writer_digest"],
            BUNDLE_DIGEST,
            status,
            row["report_json"],
            row["report_digest"],
            row["report_byte_length"],
            row["recorded_at_us"],
        )
    ]
    assert foreign_key_check(paired.connection) == []


def test_a_bundle_carries_at_most_one_parity_report(paired: m1.Owned) -> None:
    record(paired, (PARITY, parity_row()))
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        record(paired, (PARITY, parity_row("diverged", report_id="parity-0035-two")))
    assert paired.connection.execute(f"SELECT report_id FROM {PARITY}").fetchall() == [
        (PARITY_ID,)
    ]


PARITY_REFUSALS: dict[str, tuple[dict[str, object], str]] = {
    "match on unequal digests": (
        {"existing_writer_digest": WRITER_DIGEST},
        CONSTRAINT,
    ),
    "diverged on equal digests": (
        {"status": "diverged", "existing_writer_digest": BUNDLE_DIGEST},
        CONSTRAINT,
    ),
    "unknown status": ({"status": "unknown"}, CONSTRAINT),
    "byte length": ({"report_byte_length": 1}, CONSTRAINT),
    "non-canonical body": ({"report_json": '{ "reportId": "x" }'}, CONSTRAINT),
    "another run": ({"run_id": OTHER_RUN_ID}, PAIR_MISSING),
    "unknown bundle": ({"bundle_id": bundle_id_for(9)}, PAIR_MISSING),
}


@pytest.mark.parametrize("case", sorted(PARITY_REFUSALS))
def test_a_parity_report_that_misstates_its_comparison_refuses(
    paired: m1.Owned, case: str
) -> None:
    overrides, expected = PARITY_REFUSALS[case]
    with pytest.raises(sqlite3.DatabaseError, match=expected):
        record(paired, (PARITY, parity_row(**overrides)))
    assert paired.connection.execute(f"SELECT COUNT(*) FROM {PARITY}").fetchone() == (
        0,
    )


# --- integrity verification of the chain ------------------------------------------


@pytest.mark.parametrize(
    "outcome,stage",
    [("verified", "R0"), ("sequence_gap", "R1"), ("integrity_failure", "R2")],
)
def test_an_integrity_report_records_one_closed_outcome_per_stage(
    paired: m1.Owned, outcome: str, stage: str
) -> None:
    row = integrity_row(outcome, rollout_stage=stage)
    record(paired, (INTEGRITY, row))
    assert paired.connection.execute(
        f"SELECT rollout_stage, outcome, first_affected_sequence, diagnostic, "
        f"observed_head, report_json, report_digest, report_byte_length "
        f"FROM {INTEGRITY}"
    ).fetchall() == [
        (
            stage,
            outcome,
            row["first_affected_sequence"],
            DIAGNOSTIC_FOR[outcome],
            0,
            row["report_json"],
            row["report_digest"],
            row["report_byte_length"],
        )
    ]
    assert foreign_key_check(paired.connection) == []


INTEGRITY_REFUSALS: dict[str, dict[str, object]] = {
    "verified naming a sequence": integrity_row(first_affected_sequence=0),
    "verified naming a diagnostic": integrity_row(diagnostic="RT_JOURNAL_SEQUENCE_GAP"),
    "gap without a sequence": integrity_row(
        "sequence_gap", first_affected_sequence=None
    ),
    "gap under the failure diagnostic": integrity_row(
        "sequence_gap", diagnostic="RT_JOURNAL_INTEGRITY_FAILURE"
    ),
    "failure without a sequence": integrity_row(
        "integrity_failure", first_affected_sequence=None
    ),
    "failure under the gap diagnostic": integrity_row(
        "integrity_failure", diagnostic="RT_JOURNAL_SEQUENCE_GAP"
    ),
    "gap without a diagnostic": integrity_row("sequence_gap", diagnostic=None),
    "failure without a diagnostic": integrity_row("integrity_failure", diagnostic=None),
    "unknown outcome": integrity_row("unknown"),
    "unknown diagnostic": integrity_row(diagnostic="RT_JOURNAL_ELSEWHERE"),
    "unknown rollout stage": integrity_row(rollout_stage="R3"),
    "byte length": integrity_row(report_byte_length=1),
}


@pytest.mark.parametrize("case", sorted(INTEGRITY_REFUSALS))
def test_an_integrity_report_outside_its_closed_combinations_refuses(
    paired: m1.Owned, case: str
) -> None:
    """`verified` is silent, and a finding names its own code -- never the other's."""
    with pytest.raises(sqlite3.DatabaseError, match=CONSTRAINT):
        record(paired, (INTEGRITY, INTEGRITY_REFUSALS[case]))
    assert paired.connection.execute(
        f"SELECT COUNT(*) FROM {INTEGRITY}"
    ).fetchone() == (0,)


# --- quarantine and release -------------------------------------------------------


def test_quarantine_and_release_append_contiguously_and_move_no_public_fact(
    flagged: m1.Owned,
) -> None:
    before = public_run_state(flagged)
    record(flagged, (QUARANTINE, quarantine_row()))
    record(flagged, (QUARANTINE, quarantine_row(1, "released")))

    assert flagged.connection.execute(
        f"SELECT disposition_sequence, event_id, action, integrity_report_id, "
        f"diagnostic, deciding_actor, reason FROM {QUARANTINE} "
        "ORDER BY disposition_sequence"
    ).fetchall() == [
        (
            0,
            event_id_for(0),
            "quarantined",
            INTEGRITY_ID,
            "RT_JOURNAL_QUARANTINED",
            None,
            None,
        ),
        (
            1,
            event_id_for(0),
            "released",
            None,
            None,
            "core-service",
            "operator_release",
        ),
    ]
    assert public_run_state(flagged) == before
    assert counts(flagged) == (1, 1)
    assert foreign_key_check(flagged.connection) == []


def test_a_quarantine_disposition_may_not_skip_the_run_s_first_sequence(
    flagged: m1.Owned,
) -> None:
    with pytest.raises(sqlite3.DatabaseError, match="contiguous from zero per run"):
        record(flagged, (QUARANTINE, quarantine_row(1)))
    record(flagged, (QUARANTINE, quarantine_row()))
    with pytest.raises(sqlite3.DatabaseError, match="contiguous from zero per run"):
        record(flagged, (QUARANTINE, quarantine_row(2, "released")))
    assert flagged.connection.execute(
        f"SELECT disposition_sequence FROM {QUARANTINE}"
    ).fetchall() == [(0,)]


RELEASE_OUTSTANDING = "must discharge an outstanding quarantine"
CARRY_FORWARD = "must carry forward the event citation it holds"

QUARANTINE_REFUSALS: dict[str, tuple[dict[str, object], str]] = {
    "held without its report": (
        quarantine_row(integrity_report_id=None),
        CONSTRAINT,
    ),
    "held without its diagnostic": (quarantine_row(diagnostic=None), CONSTRAINT),
    "held naming an actor": (
        quarantine_row(deciding_actor="core-service"),
        CONSTRAINT,
    ),
    "held naming a reason": (quarantine_row(reason="operator_release"), CONSTRAINT),
    # Sequence one, because a release at sequence zero discharges nothing and is refused
    # for that instead -- a different rule, tested on its own below.
    "released naming a report": (
        quarantine_row(1, "released", integrity_report_id=INTEGRITY_ID),
        CONSTRAINT,
    ),
    "released naming a diagnostic": (
        quarantine_row(1, "released", diagnostic="RT_JOURNAL_QUARANTINED"),
        CONSTRAINT,
    ),
    "released without an actor": (
        quarantine_row(1, "released", deciding_actor=None),
        CONSTRAINT,
    ),
    "released without a reason": (
        quarantine_row(1, "released", reason=None),
        CONSTRAINT,
    ),
    "unknown action": (quarantine_row(0, "archived"), CONSTRAINT),
    "unknown diagnostic": (
        quarantine_row(diagnostic="RT_JOURNAL_ELSEWHERE"),
        CONSTRAINT,
    ),
    "unknown event": (quarantine_row(event_id=event_id_for(9)), PAIR_MISSING),
    "unknown report": (
        quarantine_row(integrity_report_id="integrity-0035-absent"),
        PAIR_MISSING,
    ),
}


@pytest.mark.parametrize("case", sorted(QUARANTINE_REFUSALS))
def test_a_quarantine_disposition_outside_its_closed_form_refuses(
    flagged: m1.Owned, case: str
) -> None:
    row, expected = QUARANTINE_REFUSALS[case]
    held = row["disposition_sequence"] == 1
    if held:
        record(flagged, (QUARANTINE, quarantine_row()))
    with pytest.raises(sqlite3.DatabaseError, match=expected):
        record(flagged, (QUARANTINE, row))
    assert flagged.connection.execute(
        f"SELECT COUNT(*) FROM {QUARANTINE}"
    ).fetchone() == (int(held),)


def test_a_release_may_not_be_a_runs_first_disposition(flagged: m1.Owned) -> None:
    """A release discharges a hold, so there has to be one to discharge.

    Sequence zero is contiguous and carries no citation forward, so nothing else in this
    trigger stops it -- and a run whose only disposition says `released` reads as a
    decision somebody made about a quarantine that never existed.
    """
    with pytest.raises(sqlite3.DatabaseError, match=RELEASE_OUTSTANDING):
        record(flagged, (QUARANTINE, quarantine_row(0, "released")))
    assert flagged.connection.execute(
        f"SELECT COUNT(*) FROM {QUARANTINE}"
    ).fetchone() == (0,)

    record(flagged, (QUARANTINE, quarantine_row()))
    record(flagged, (QUARANTINE, quarantine_row(1, "released")))
    assert flagged.connection.execute(
        f"SELECT disposition_sequence, action FROM {QUARANTINE} "
        "ORDER BY disposition_sequence"
    ).fetchall() == [(0, "quarantined"), (1, "released")]


def dispositions(holder: m1.Owned) -> list[tuple[object, ...]]:
    return [
        tuple(row)
        for row in holder.connection.execute(
            f"SELECT disposition_sequence, action, event_id FROM {QUARANTINE} "
            "ORDER BY disposition_sequence"
        )
    ]


@pytest.fixture
def twice_flagged(flagged: m1.Owned) -> m1.Owned:
    """Two holds citing two different surviving events, the second one outstanding.

    Two events, because a stack whose members all name the same row cannot show which
    of them a release actually discharged.
    """
    link = str(event_row()["event_digest"])
    record(flagged, (BUNDLES, bundle_row(1, link)), (JOURNAL, event_row(1, link)))
    record(
        flagged,
        (QUARANTINE, quarantine_row(0, event_id=event_id_for(0))),
        (QUARANTINE, quarantine_row(1, event_id=event_id_for(1))),
    )
    return flagged


def test_a_release_discharges_the_latest_outstanding_hold_and_no_other(
    twice_flagged: m1.Owned,
) -> None:
    """Stacked holds come off newest first, and each release names the one it takes.

    A release citing a hold that is not the outstanding one is somebody answering a
    finding that is not the one in front of them -- or answering one another release has
    already discharged -- so the trigger refuses it either way, and the remaining stack
    is unchanged.
    """
    held = dispositions(twice_flagged)
    with pytest.raises(sqlite3.DatabaseError, match=CARRY_FORWARD):
        record(
            twice_flagged,
            (QUARANTINE, quarantine_row(2, "released", event_id=event_id_for(0))),
        )
    assert dispositions(twice_flagged) == held

    record(
        twice_flagged,
        (QUARANTINE, quarantine_row(2, "released", event_id=event_id_for(1))),
    )
    with pytest.raises(sqlite3.DatabaseError, match=CARRY_FORWARD):
        record(
            twice_flagged,
            (QUARANTINE, quarantine_row(3, "released", event_id=event_id_for(1))),
        )
    record(
        twice_flagged,
        (QUARANTINE, quarantine_row(3, "released", event_id=event_id_for(0))),
    )
    assert dispositions(twice_flagged) == [
        (0, "quarantined", event_id_for(0)),
        (1, "quarantined", event_id_for(1)),
        (2, "released", event_id_for(1)),
        (3, "released", event_id_for(0)),
    ]


def test_a_hold_appended_after_a_release_is_the_next_one_discharged(
    twice_flagged: m1.Owned,
) -> None:
    """The stack keeps its depth across a release, and the newest hold is still the top.

    A run held, released and held again is the ordinary case for one that keeps failing
    verification, and it is where a rule reading only sequence order comes apart: the
    hold appended last comes off first, and the one under it -- appended before both
    releases -- is what is left standing.
    """
    record(
        twice_flagged,
        (QUARANTINE, quarantine_row(2, "released", event_id=event_id_for(1))),
        (QUARANTINE, quarantine_row(3, event_id=event_id_for(1))),
    )
    standing = dispositions(twice_flagged)
    with pytest.raises(sqlite3.DatabaseError, match=CARRY_FORWARD):
        record(
            twice_flagged,
            (QUARANTINE, quarantine_row(4, "released", event_id=event_id_for(0))),
        )
    assert dispositions(twice_flagged) == standing

    record(
        twice_flagged,
        (QUARANTINE, quarantine_row(4, "released", event_id=event_id_for(1))),
    )
    with pytest.raises(sqlite3.DatabaseError, match=CARRY_FORWARD):
        record(
            twice_flagged,
            (QUARANTINE, quarantine_row(5, "released", event_id=event_id_for(1))),
        )
    record(
        twice_flagged,
        (QUARANTINE, quarantine_row(5, "released", event_id=event_id_for(0))),
    )
    with pytest.raises(sqlite3.DatabaseError, match=RELEASE_OUTSTANDING):
        record(
            twice_flagged,
            (QUARANTINE, quarantine_row(6, "released", event_id=event_id_for(0))),
        )
    assert dispositions(twice_flagged) == [
        (0, "quarantined", event_id_for(0)),
        (1, "quarantined", event_id_for(1)),
        (2, "released", event_id_for(1)),
        (3, "quarantined", event_id_for(1)),
        (4, "released", event_id_for(1)),
        (5, "released", event_id_for(0)),
    ]


@pytest.mark.parametrize("event_id", [event_id_for(0), event_id_for(1)])
def test_a_release_past_the_last_outstanding_hold_refuses(
    twice_flagged: m1.Owned, event_id: str
) -> None:
    """Two holds are two decisions and no more; a third answers nothing left held."""
    record(
        twice_flagged,
        (QUARANTINE, quarantine_row(2, "released", event_id=event_id_for(1))),
        (QUARANTINE, quarantine_row(3, "released", event_id=event_id_for(0))),
    )
    discharged = dispositions(twice_flagged)
    with pytest.raises(sqlite3.DatabaseError, match=RELEASE_OUTSTANDING):
        record(
            twice_flagged,
            (QUARANTINE, quarantine_row(4, "released", event_id=event_id)),
        )
    assert dispositions(twice_flagged) == discharged


@pytest.mark.parametrize(
    "overrides",
    [{"event_id": OTHER_EVENT_ID}, {"integrity_report_id": OTHER_REPORT_ID}],
    ids=["another run's event", "another run's report"],
)
def test_a_quarantine_that_names_another_runs_evidence_refuses(
    flagged: m1.Owned, overrides: dict[str, object]
) -> None:
    """Both associations are run-scoped: same workspace is not close enough."""
    seed_other_run(flagged)
    with pytest.raises(sqlite3.IntegrityError, match=PAIR_MISSING):
        record(flagged, (QUARANTINE, quarantine_row(**overrides)))
    assert flagged.connection.execute(
        f"SELECT COUNT(*) FROM {QUARANTINE}"
    ).fetchone() == (0,)


# --- a quarantine with no surviving event to cite ---------------------------------

CITATION_REQUIRED = "may omit its event only for a sequence gap"
FINDING_REQUIRED = "must cite an integrity report that found a fault"


@pytest.fixture
def gapped(paired: m1.Owned) -> m1.Owned:
    """That run, with the `sequence_gap` report a citationless quarantine may cite."""
    record(paired, (INTEGRITY, integrity_row("sequence_gap")))
    return paired


def test_a_sequence_gap_quarantine_may_hold_a_run_with_no_event_to_cite(
    gapped: m1.Owned,
) -> None:
    """A journal that is gone entirely is the one fault with nothing left to name.

    Requiring an event citation here would make the worst journal fault the only one
    this schema cannot hold, so a `sequence_gap` quarantine may omit it -- and the
    release that follows carries the same absence forward rather than inventing one.
    """
    before = public_run_state(gapped)
    record(gapped, (QUARANTINE, quarantine_row(event_id=None)))
    record(gapped, (QUARANTINE, quarantine_row(1, "released", event_id=None)))

    assert gapped.connection.execute(
        f"SELECT disposition_sequence, event_id, action, integrity_report_id "
        f"FROM {QUARANTINE} ORDER BY disposition_sequence"
    ).fetchall() == [
        (0, None, "quarantined", INTEGRITY_ID),
        (1, None, "released", None),
    ]
    assert public_run_state(gapped) == before
    assert counts(gapped) == (1, 1)
    assert foreign_key_check(gapped.connection) == []


CITATIONLESS_REFUSALS: dict[str, tuple[str, dict[str, object], str]] = {
    "an integrity failure with nothing named": (
        "integrity_failure",
        {"event_id": None},
        CITATION_REQUIRED,
    ),
    "a report that found nothing": (
        "verified",
        {},
        FINDING_REQUIRED,
    ),
    "a report that found nothing, with nothing named": (
        "verified",
        {"event_id": None},
        FINDING_REQUIRED,
    ),
    "no report at all": (
        "sequence_gap",
        {"event_id": None, "integrity_report_id": None},
        CITATION_REQUIRED,
    ),
}


@pytest.mark.parametrize("case", sorted(CITATIONLESS_REFUSALS))
def test_a_quarantine_that_omits_its_event_without_a_gap_refuses(
    paired: m1.Owned, case: str
) -> None:
    outcome, overrides, expected = CITATIONLESS_REFUSALS[case]
    record(paired, (INTEGRITY, integrity_row(outcome)))
    with pytest.raises(sqlite3.DatabaseError, match=expected):
        record(paired, (QUARANTINE, quarantine_row(**overrides)))
    assert paired.connection.execute(
        f"SELECT COUNT(*) FROM {QUARANTINE}"
    ).fetchone() == (0,)


def test_a_citationless_quarantine_may_not_borrow_another_runs_gap(
    gapped: m1.Owned,
) -> None:
    """Run scope holds with no event named: the report still has to be this run's."""
    seed_other_run(gapped)
    record(
        gapped,
        (
            INTEGRITY,
            integrity_row("sequence_gap", run_id=OTHER_RUN_ID, report_id="gap-other"),
        ),
    )
    with pytest.raises(sqlite3.DatabaseError, match=CITATION_REQUIRED):
        record(
            gapped,
            (
                QUARANTINE,
                quarantine_row(event_id=None, integrity_report_id="gap-other"),
            ),
        )
    assert gapped.connection.execute(
        f"SELECT COUNT(*) FROM {QUARANTINE}"
    ).fetchone() == (0,)


@pytest.mark.parametrize(
    "held,released",
    [(None, event_id_for(0)), (event_id_for(0), None)],
    ids=["a release that invents a citation", "a release that drops one"],
)
def test_a_release_that_restates_the_citation_it_holds_refuses(
    paired: m1.Owned, held: str | None, released: str | None
) -> None:
    """A release changes who decided, never what was held."""
    outcome = "sequence_gap" if held is None else "integrity_failure"
    record(paired, (INTEGRITY, integrity_row(outcome)))
    record(paired, (QUARANTINE, quarantine_row(event_id=held)))
    with pytest.raises(sqlite3.DatabaseError, match=CARRY_FORWARD):
        record(paired, (QUARANTINE, quarantine_row(1, "released", event_id=released)))
    assert paired.connection.execute(
        f"SELECT COUNT(*) FROM {QUARANTINE}"
    ).fetchone() == (1,)


# --- recorded retention boundaries ------------------------------------------------


def record_boundary(holder: m1.Owned, row: dict[str, object]) -> None:
    with m27.guarded(holder):
        m27.audit(holder, RETENTION_AUDIT)
        m27.insert(holder, RETENTION, row)


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"first_removed_sequence": 0, "last_removed_sequence": 0, "resumable_after": 0},
        {"first_removed_sequence": 2, "last_removed_sequence": 9, "resumable_after": 0},
    ],
    ids=["nothing removed", "one sequence removed", "a range removed"],
)
def test_a_retention_boundary_records_a_removable_range_and_removes_nothing(
    paired: m1.Owned, overrides: dict[str, object]
) -> None:
    with m27.guarded(paired):
        m27.audit(paired, RETENTION_AUDIT)
    before = (
        rows_of(paired, JOURNAL, "sequence"),
        rows_of(paired, m27.EVIDENCE, "run_id, evidence_kind"),
        rows_of(paired, "omnivia_application_audit_events", "audit_ref"),
    )
    row = retention_row(**overrides)
    record_boundary(paired, row)

    assert paired.connection.execute(
        f"SELECT boundary_id, run_id, first_removed_sequence, last_removed_sequence, "
        f"resumable_after, policy_ref, evidence_ref, audit_ref FROM {RETENTION}"
    ).fetchall() == [
        (
            BOUNDARY_ID,
            m27.RUN_ID,
            row["first_removed_sequence"],
            row["last_removed_sequence"],
            row["resumable_after"],
            row["policy_ref"],
            row["evidence_ref"],
            RETENTION_AUDIT,
        )
    ]
    assert (
        rows_of(paired, JOURNAL, "sequence"),
        rows_of(paired, m27.EVIDENCE, "run_id, evidence_kind"),
        rows_of(paired, "omnivia_application_audit_events", "audit_ref"),
    ) == before
    assert foreign_key_check(paired.connection) == []


RETENTION_REFUSALS: dict[str, tuple[dict[str, object], str]] = {
    "first without last": ({"first_removed_sequence": 0}, CONSTRAINT),
    "last without first": ({"last_removed_sequence": 0}, CONSTRAINT),
    "reversed range": (
        {"first_removed_sequence": 2, "last_removed_sequence": 1, "resumable_after": 0},
        CONSTRAINT,
    ),
    "removal still resumable": (
        {"first_removed_sequence": 0, "last_removed_sequence": 0},
        CONSTRAINT,
    ),
    "unknown resumable value": ({"resumable_after": 2}, CONSTRAINT),
    "no policy ref": ({"policy_ref": None}, NOT_NULL),
    "no evidence ref": ({"evidence_ref": None}, NOT_NULL),
    # A missing audit reference is refused by the trigger that checks the audit
    # fact belongs to this workspace, which no NULL can satisfy either.
    "no audit ref": ({"audit_ref": None}, AUDIT_REQUIRED),
    "audit ref of no such workspace fact": (
        {"audit_ref": "audit-elsewhere"},
        AUDIT_REQUIRED,
    ),
}


@pytest.mark.parametrize("case", sorted(RETENTION_REFUSALS))
def test_a_retention_boundary_that_misstates_its_range_or_refs_refuses(
    paired: m1.Owned, case: str
) -> None:
    overrides, expected = RETENTION_REFUSALS[case]
    with pytest.raises(sqlite3.DatabaseError, match=expected):
        record_boundary(paired, retention_row(**overrides))
    assert paired.connection.execute(
        f"SELECT COUNT(*) FROM {RETENTION}"
    ).fetchone() == (0,)


# --- nothing recorded here is ever rewritten --------------------------------------


def settle(holder: m1.Owned) -> None:
    """Every table this slice covers, populated once for the run that is bound."""
    record(
        holder, (PARITY, parity_row()), (INTEGRITY, integrity_row("integrity_failure"))
    )
    record(holder, (QUARANTINE, quarantine_row()))
    record_boundary(holder, retention_row())


@pytest.mark.parametrize(
    "statement",
    [
        f"UPDATE {PARITY} SET status = 'diverged'",
        f"DELETE FROM {PARITY}",
        f"UPDATE {INTEGRITY} SET outcome = 'verified'",
        f"DELETE FROM {INTEGRITY}",
        f"UPDATE {QUARANTINE} SET action = 'released'",
        f"DELETE FROM {QUARANTINE}",
        f"UPDATE {RETENTION} SET resumable_after = 0",
        f"DELETE FROM {RETENTION}",
    ],
)
def test_a_recorded_judgement_is_immutable_even_for_its_current_owner(
    paired: m1.Owned, statement: str
) -> None:
    settle(paired)
    with m27.guarded(paired), pytest.raises(sqlite3.DatabaseError, match=APPEND_ONLY):
        paired.connection.execute(statement)
    assert [
        paired.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (PARITY, INTEGRITY, QUARANTINE, RETENTION)
    ] == [1, 1, 1, 1]


def test_the_fully_populated_runtime_schema_stays_sound(paired: m1.Owned) -> None:
    settle(paired)
    seed_other_run(paired)
    assert integrity_check(paired.connection) == []
    assert foreign_key_check(paired.connection) == []
    assert fingerprint_schema(paired.connection).matches(canonical_schema_fingerprint())
    assert [
        paired.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in TABLES
    ] == [2, 2, 2, 1, 2, 1, 1]
