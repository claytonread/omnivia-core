"""DatasetState storage component (SPEC-CORE-DATA-001 WP07 PR 1; migration 0062).

Proves the additive migration, its three guards, the current-state projection and
`storage.dataset_state`. Rows are written the way a mutation settles them -- the
application audit event, then the observation, inside one caller-owned
`fenced_transaction` -- or as raw SQL where a test has to show the schema refusing
on its own. Nothing here wires an operation: `analysis.start` stays the milestone-1
refusal that `test_analysis_start_refusal.py` pins.

Evidence is a closed shape: every refusal of a document is pinned at the writer and
at the raw INSERT, and the reader re-checks what it decodes.

Refusal-only cases share one module-scoped workspace. Every refused write rolls its
whole fence back, and each case asserts that it left nothing behind.
"""

from __future__ import annotations

import dataclasses
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import pytest
import test_blobs_staged_sources_and_evidence_migration as m2
from omnivia_core_runtime.ownership.fencing import (
    assert_guards_intact,
    close_guard,
    fenced_transaction,
    guarded_tables,
)
from omnivia_core_runtime.service.mutation import MutationSettlementContext
from omnivia_core_runtime.storage import dataset_state
from omnivia_core_runtime.storage.backup import (
    backup_database,
    new_attempt_id,
    restore_backup,
    verify_backup,
)
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    authorised,
    fingerprint_schema,
    foreign_key_check,
    integrity_check,
    open_database,
    split_sql_statements,
)
from omnivia_core_runtime.storage.decisions import content_digest
from omnivia_core_runtime.storage.inventory import capture_inventory
from omnivia_core_runtime.storage.migrations import (
    applied_migrations,
    apply_pending_migrations,
    canonical_schema_fingerprint,
    load_migrations,
    materialise_phase0_baseline,
    phase0_baseline_sql,
    read_workspace_state,
)

from omnivia_core.contracts.v1 import to_canonical_json

WORKSPACE_ID = m2.WORKSPACE_ID
BASE_US = m2.BASE_US
#: The largest instant a closed shape admits: the largest signed 64-bit integer.
MAX_INSTANT = 2**63 - 1

MIGRATION_VERSION = 62
MIGRATION_NAME = "0062_analysis_dataset_state.sql"
PREDECESSOR_NAME = "0061_engineering_legacy_identity.sql"
TABLE = "omnivia_analysis_dataset_state_observations"
VIEW = "omnivia_analysis_dataset_state_current"
GUARDS = {
    "INSERT": "omnivia_guard_analysis_dataset_state_observations_insert",
    "UPDATE": "omnivia_guard_analysis_dataset_state_observations_update",
    "DELETE": "omnivia_guard_analysis_dataset_state_observations_delete",
}

#: The accepted vocabularies, stated literally as the second, independently reviewed
#: copy: moving a value in the module or in the schema's CHECKs fails here.
VOCABULARIES: dict[str, tuple[str, ...]] = {
    "initial_readiness": ("not_started", "initialising", "catching_up", "ready", "blocked"),
    "completeness": ("complete", "partial", "unknown"),
    "continuity": ("verified", "gap_detected", "unknown", "not_applicable"),
    "operational_health": ("healthy", "degraded", "unavailable", "error", "unknown"),
    "schema_compatibility": ("compatible", "requires_review", "incompatible", "unknown"),
    "evidence_availability": ("available", "limited", "unavailable"),
    "content_observation": ("empty", "nonempty", "unknown"),
}

#: The evidence vocabularies, stated the same way.
EVIDENCE_VOCABULARIES: dict[str, tuple[str, ...]] = {
    "proof_kind": (
        "complete_enumeration",
        "consistent_snapshot",
        "contiguous_log",
        "bounded_observation",
        "none",
    ),
    "evidence_kind": (
        "snapshot",
        "stream_caught_up",
        "cursor_poll",
        "complete_reconcile",
        "captured_query",
        "none",
    ),
}

SCOPE_DIGEST = "sha256:" + "5" * 64
MANIFEST_DIGEST = "sha256:" + "6" * 64


def _coverage(**overrides: Any) -> dict[str, Any]:
    """A closed coverage document, bound to `SCOPE_DIGEST`."""
    document: dict[str, Any] = {
        "scope_digest": SCOPE_DIGEST,
        "accepted_rows": 12,
        "rejected_rows": 0,
        "conflicting_rows": 0,
        "deduplicated_rows": 0,
        "expected_source_rows": 12,
        "proof_kind": "complete_enumeration",
        "proof_refs": ["listing-2026-10-04"],
    }
    document.update(overrides)
    return document


def _source(**overrides: Any) -> dict[str, Any]:
    """A closed source observation, bound to `SCOPE_DIGEST` and verified at `BASE_US`."""
    document: dict[str, Any] = {
        "source_ref": {"id": "source-erp", "revision_id": "source-erp-r4"},
        "source_incarnation": "source-incarnation-1",
        "observation_interval": {
            "start_inclusive_at_us": BASE_US - 60_000_000,
            "end_exclusive_at_us": BASE_US,
        },
        "source_cutoff_at_us": BASE_US,
        "verification_at_us": BASE_US,
        "evidence_kind": "snapshot",
        "snapshot_token_ref": "snapshot-token-1",
        "applied_checkpoint_ref": None,
        "scope_digest": SCOPE_DIGEST,
        "evidence_refs": ["source-evidence-1"],
    }
    document.update(overrides)
    return document


COVERAGE_JSON = to_canonical_json(_coverage())
SOURCE_JSON = to_canonical_json(_source())

CHECK = "CHECK constraint failed"
AUDIT = "exact successful audit"
GENERATION = "advance by exactly one"
UNGUARDED = f"unguarded INSERT on {TABLE}"
#: The trigger's own refusals of an evidence document, one static message per statement,
#: so each refusal names the layer that made it.
JSON_FORM = "must be valid JSON"
OBJECT_FORM = "must be a JSON object"
TOO_DEEP = "nested deeper than 32 levels"
COVERAGE_SHAPE = "coverage evidence is outside its closed shape"
SOURCE_SHAPE = "source observation evidence is outside its closed shape"
BOUND = "is not bound to its row"
#: The reader's refusals of stored evidence before it decodes it: its storage bound, and its type.
STORED_BOUND = "is outside its byte bound"
STORED_TEXT = "is not text"


def _not_null(column: str) -> str:
    return rf"NOT NULL constraint failed: {TABLE}\.{column}"


# --- harness: the established bootstrap, fence and audit shapes ----------------------


def _bootstrap(path: Path) -> m2.Owned:
    materialise_phase0_baseline(path)
    m2.bootstrap_and_migrate(path)
    return m2.take_ownership(path)


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[m2.Owned]:
    holder = _bootstrap(tmp_path / "workspace.sqlite")
    yield holder
    holder.connection.close()


@pytest.fixture(scope="module")
def refusing(tmp_path_factory: pytest.TempPathFactory) -> Iterator[m2.Owned]:
    holder = _bootstrap(tmp_path_factory.mktemp("refusing") / "workspace.sqlite")
    yield holder
    holder.connection.close()


def _fenced(holder: m2.Owned) -> AbstractContextManager[sqlite3.Connection]:
    return fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    )


def _audit(
    connection: sqlite3.Connection,
    *,
    ref: str,
    at_us: int,
    outcome_class: str = "succeeded",
) -> str:
    """One application audit event, as the executor writes it before the domain write."""
    connection.execute(
        "INSERT INTO omnivia_application_audit_events (audit_ref, workspace_id, "
        "principal_id, operation, purpose, request_id, correlation_id, trace_id, "
        "granted_authority_json, outcome_class, error_code, recorded_at_us) "
        "VALUES (?, ?, 'core-service', 'test.dataset_state', 'p', ?, ?, ?, '{}', ?, ?, ?)",
        (
            ref,
            WORKSPACE_ID,
            ref,
            ref,
            ref,
            outcome_class,
            None if outcome_class == "succeeded" else "refused_for_test",
            at_us,
        ),
    )
    return ref


def _settlement(ref: str, at_us: int) -> MutationSettlementContext:
    return MutationSettlementContext(
        audit_ref=ref, claim_id=f"clm-{ref}", outcome_id=f"out-{ref}", settled_at_us=at_us
    )


def _observe(
    holder: m2.Owned, observation: dataset_state.DatasetStateObservation, *, at_us: int
) -> int:
    """Settle one observation as a mutation does: its audit, then the write, one fence."""
    with _fenced(holder) as fenced:
        ref = _audit(fenced, ref=f"aud-dss-{at_us}", at_us=at_us)
        return dataset_state.record_observation(
            fenced, _settlement(ref, at_us), workspace_id=WORKSPACE_ID, observation=observation
        )


def _observation(**overrides: Any) -> dataset_state.DatasetStateObservation:
    fields: dict[str, Any] = {
        "dataset_id": "dataset-invoices",
        "dataset_revision": "dataset-invoices-r1",
        "dataset_incarnation": "incarnation-1",
        "initial_readiness": "ready",
        "completeness": "complete",
        "continuity": "verified",
        "operational_health": "healthy",
        "schema_compatibility": "compatible",
        "content_observation": "nonempty",
        "evidence_availability": "available",
        "observed_authority_epoch": "authority-epoch-7",
        "scope_digest": SCOPE_DIGEST,
        "coverage": _coverage(),
        "source_observation": _source(),
        "verified_at_us": BASE_US,
        "freshness_deadline_at_us": BASE_US + 3_600_000_000,
        "manifest_id": "manifest-invoices",
        "manifest_revision": "manifest-invoices-r3",
        "manifest_digest": MANIFEST_DIGEST,
    }
    fields.update(overrides)
    return dataset_state.DatasetStateObservation(**fields)


def _raw_row(**overrides: object) -> dict[str, object]:
    """One complete observation in column form, for writing past the module."""
    row: dict[str, object] = {
        "workspace_id": WORKSPACE_ID,
        "dataset_id": "dataset-raw",
        "state_generation": 1,
        "dataset_revision": "dataset-raw-r1",
        "dataset_incarnation": "incarnation-1",
        "manifest_id": None,
        "manifest_revision": None,
        "manifest_digest": None,
        "initial_readiness": "ready",
        "completeness": "complete",
        "continuity": "verified",
        "operational_health": "healthy",
        "schema_compatibility": "compatible",
        "content_observation": "nonempty",
        "evidence_availability": "available",
        "observed_authority_epoch": "authority-epoch-7",
        "scope_digest": SCOPE_DIGEST,
        "coverage_json": COVERAGE_JSON,
        "coverage_digest": content_digest(COVERAGE_JSON),
        "source_observation_json": SOURCE_JSON,
        "source_observation_digest": content_digest(SOURCE_JSON),
        "freshness_deadline_at_us": None,
        "verified_at_us": BASE_US,
        "recorded_at_us": BASE_US,
        "audit_ref": "aud-dss-raw",
    }
    row.update(overrides)
    return row


def _write_raw(
    connection: sqlite3.Connection, *, at_us: int = BASE_US, **overrides: object
) -> None:
    """Write one raw row under its own successful audit, inside the caller's fence."""
    ref = _audit(connection, ref=f"aud-dss-raw-{at_us}", at_us=at_us)
    row = _raw_row(**{"recorded_at_us": at_us, "audit_ref": ref, **overrides})
    m2.insert(connection, TABLE, row)


class _Undo(Exception):
    """Raised inside a fence to roll back a write that was only a control."""


def _admitted_then_undone(holder: m2.Owned, **overrides: object) -> None:
    """Control: the schema admits this row here; the write is then rolled back."""
    with pytest.raises(_Undo), _fenced(holder) as fenced:
        _write_raw(fenced, **overrides)
        raise _Undo


def _refused(holder: m2.Owned, refusal: str, **overrides: object) -> None:
    """The schema refuses this row, and the refusal leaves nothing behind."""
    before = m2.count(holder.connection, TABLE)
    with pytest.raises(sqlite3.DatabaseError, match=refusal), _fenced(holder) as fenced:
        _write_raw(fenced, **overrides)
    assert m2.count(holder.connection, TABLE) == before


def _columns(connection: sqlite3.Connection, name: str) -> list[str]:
    return [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{name}")')]


def _replayed(connection: sqlite3.Connection) -> set[tuple[Any, ...]]:
    """Each dataset's current state, rebuilt by replaying the log in generation order."""
    columns = _columns(connection, TABLE)
    current: dict[tuple[Any, Any], tuple[Any, ...]] = {}
    for row in connection.execute(
        f"SELECT {', '.join(columns)} FROM {TABLE} ORDER BY state_generation"
    ):
        values = dict(zip(columns, row, strict=True))
        current[(values["workspace_id"], values["dataset_id"])] = tuple(row)
    return set(current.values())


def _projected(connection: sqlite3.Connection) -> set[tuple[Any, ...]]:
    columns = ", ".join(_columns(connection, TABLE))
    return {tuple(row) for row in connection.execute(f"SELECT {columns} FROM {VIEW}")}


def _history(connection: sqlite3.Connection, dataset_id: str) -> tuple[Any, ...]:
    return dataset_state.read_state_history(
        connection, workspace_id=WORKSPACE_ID, dataset_id=dataset_id
    )


def _current(connection: sqlite3.Connection, dataset_id: str) -> Any:
    return dataset_state.read_current_state(
        connection, workspace_id=WORKSPACE_ID, dataset_id=dataset_id
    )


def _text(document: object) -> str:
    """A raw evidence document as a writer spells it: sorted keys, no whitespace."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def _unsorted(document: dict[str, Any]) -> str:
    """The same fields in reverse key order: every field present, none out of place."""
    return json.dumps(dict(reversed(list(document.items()))), separators=(",", ":"))


def _stored(base: str, text: str) -> dict[str, object]:
    """Column overrides that store `text` under its own digest, as a writer would.
    `base` is the document's column stem: `coverage` or `source_observation`."""
    return {f"{base}_json": text, f"{base}_digest": content_digest(text)}


def _bare_table() -> sqlite3.Connection:
    """The observation table and its projection, with their CHECKs and no guard trigger, so
    a row written here is stored exactly as given and the reader's checks refuse it."""
    connection = sqlite3.connect(":memory:")
    migration = next(item for item in load_migrations() if item.version == MIGRATION_VERSION)
    for statement in split_sql_statements(migration.sql)[:2]:
        connection.execute(statement)
    return connection


# --- the migration itself ------------------------------------------------------------


def test_0062_is_the_unique_successor_of_0061_and_executes_no_dml() -> None:
    migrations = load_migrations()
    found = [item for item in migrations if item.version == MIGRATION_VERSION]
    assert [item.name for item in found] == [MIGRATION_NAME]
    predecessor = migrations[migrations.index(found[0]) - 1]
    assert (predecessor.version, predecessor.name) == (MIGRATION_VERSION - 1, PREDECESSOR_NAME)

    # The statements as the migrator runs them, comments stripped: five declarations,
    # and no statement anywhere -- trigger bodies included -- that writes a row.
    statements = split_sql_statements(found[0].sql)
    declared = [
        re.match(r"CREATE (TABLE|VIEW|TRIGGER) IF NOT EXISTS (\w+) ", " ".join(item.split()))
        for item in statements
    ]
    assert [None if match is None else match.groups() for match in declared] == [
        ("TABLE", TABLE),
        ("VIEW", VIEW),
        ("TRIGGER", GUARDS["INSERT"]),
        ("TRIGGER", GUARDS["UPDATE"]),
        ("TRIGGER", GUARDS["DELETE"]),
    ]
    writes = re.compile(
        r"\b(INSERT\s+(OR\s+\w+\s+)?INTO|REPLACE\s+INTO|UPDATE\s+(OR\s+\w+\s+)?\w+\s+SET"
        r"|DELETE\s+FROM|DROP|ALTER)\b",
        re.IGNORECASE,
    )
    assert writes.search("\n".join(statements)) is None


def test_0062_adds_exactly_its_table_projection_and_three_guards() -> None:
    def objects(through: int) -> set[tuple[str, str]]:
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript(phase0_baseline_sql())
            for migration in load_migrations():
                if migration.version <= through:
                    connection.executescript(migration.sql)
            return {
                (str(row[0]), str(row[1]))
                for row in connection.execute(
                    "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                )
            }
        finally:
            connection.close()

    before = objects(MIGRATION_VERSION - 1)
    after = objects(MIGRATION_VERSION)
    assert before <= after
    assert after - before == {
        ("table", TABLE),
        ("view", VIEW),
        *(("trigger", name) for name in GUARDS.values()),
    }


def test_0062_orders_members_by_canonical_text_and_never_by_a_json_table_id() -> None:
    """SQLite documents a JSON table's `id` as housekeeping with no order, so no comparison in
    the migration may read member order from one."""
    migration = next(item for item in load_migrations() if item.version == MIGRATION_VERSION)
    ordering = re.compile(r"\.id\s*(<|>)|(<|>)=?\s*[\w.]*\.id\b")
    assert ordering.search(migration.sql) is None


def test_0062_walks_depth_inside_a_subquery_and_never_as_a_statement_clause() -> None:
    """A trigger's statements do not take a common table expression directly, only one that a
    sub-select embeds, so each recursive walk must open inside parentheses."""
    migration = next(item for item in load_migrations() if item.version == MIGRATION_VERSION)
    walks = list(re.finditer(r"\bWITH\s+RECURSIVE\b", migration.sql))
    assert len(walks) == 2
    for walk in walks:
        assert migration.sql[: walk.start()].rstrip().endswith("(")


def test_the_migrated_workspace_is_guarded_canonical_and_clean(owned: m2.Owned) -> None:
    connection = owned.connection
    migration = next(item for item in load_migrations() if item.version == MIGRATION_VERSION)
    assert applied_migrations(connection)[MIGRATION_VERSION] == migration.checksum
    for statement, name in GUARDS.items():
        declaration = " ".join(m2.object_sql(connection, name).split())
        assert f"BEFORE {statement} ON {TABLE} " in declaration
    assert TABLE in guarded_tables()
    assert VIEW in m2.object_names(connection, "view")
    assert VIEW not in guarded_tables()
    assert_guards_intact(connection)
    assert fingerprint_schema(connection).matches(canonical_schema_fingerprint())
    assert integrity_check(connection) == []
    assert foreign_key_check(connection) == []


def test_a_populated_0061_workspace_upgrades_to_0062_without_data_loss(tmp_path: Path) -> None:
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    with m2.migration_catalogue_through(MIGRATION_VERSION - 1):
        m2.bootstrap_and_migrate(path)
    predecessor = m2.take_ownership(path)
    try:
        m2.seed_chain(predecessor)
        with _fenced(predecessor) as fenced:
            _audit(fenced, ref="aud-dss-before-0062", at_us=BASE_US)
        assert TABLE not in m2.object_names(predecessor.connection, "table")
        before = capture_inventory(predecessor.connection)
    finally:
        predecessor.connection.close()

    with m2.migration_catalogue_through(MIGRATION_VERSION):
        maintenance = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
        try:
            state = read_workspace_state(maintenance)
            assert state is not None
            applied = apply_pending_migrations(
                maintenance,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=m2.SERVICE_INSTANCE,
                fencing_generation=state.fencing_generation,
                workspace_id=WORKSPACE_ID,
            )
            assert [item.version for item in applied] == [MIGRATION_VERSION]
            after = capture_inventory(maintenance)
        finally:
            maintenance.close()

    # Every pre-existing table keeps its exact columns, rows and values; only the
    # migration ledger records the step, and the one new table starts empty.
    ledger = {"omnivia_schema_migrations", "omnivia_migration_attempts"}
    assert before.total_rows > 0
    for table in before.tables:
        if table.name not in ledger:
            assert after.table(table.name) == table, table.name
    assert set(after.table_names) - set(before.table_names) == {TABLE}
    created = after.table(TABLE)
    assert created is not None and created.row_count == 0

    upgraded = m2.take_ownership(path)
    try:
        assert_guards_intact(upgraded.connection)
        assert fingerprint_schema(upgraded.connection).matches(canonical_schema_fingerprint())
        assert _observe(upgraded, _observation(), at_us=BASE_US + 1) == 1
        assert integrity_check(upgraded.connection) == []
        assert foreign_key_check(upgraded.connection) == []
    finally:
        upgraded.connection.close()


# --- fencing and immutability ----------------------------------------------------------


def test_an_unfenced_insert_is_refused_and_an_existing_fence_appends(owned: m2.Owned) -> None:
    connection = owned.connection
    # The service connection outside any fence: the authorizer refuses the write.
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        dataset_state.record_observation(
            connection,
            _settlement("aud-dss-unfenced", BASE_US),
            workspace_id=WORKSPACE_ID,
            observation=_observation(),
        )
    # Authority moved inside the transaction: the persisted predicate refuses.
    with pytest.raises(sqlite3.DatabaseError, match=UNGUARDED), _fenced(owned) as fenced:
        ref = _audit(fenced, ref="aud-dss-stale", at_us=BASE_US)
        fenced.execute(
            "UPDATE omnivia_workspace_state "
            "SET fencing_generation = fencing_generation + 1 WHERE singleton = 1"
        )
        dataset_state.record_observation(
            fenced,
            _settlement(ref, BASE_US),
            workspace_id=WORKSPACE_ID,
            observation=_observation(),
        )
    assert m2.count(connection, TABLE) == 0

    # The caller's own fence, already holding its audit event, appends.
    assert _observe(owned, _observation(), at_us=BASE_US + 1) == 1
    assert _observe(owned, _observation(completeness="partial"), at_us=BASE_US + 2) == 2
    assert [record.audit_ref for record in _history(connection, "dataset-invoices")] == [
        f"aud-dss-{BASE_US + 1}",
        f"aud-dss-{BASE_US + 2}",
    ]

    # No guard at all: a writer the authorizer lets through still meets the trigger.
    close_guard(connection)
    with pytest.raises(sqlite3.DatabaseError, match=UNGUARDED), authorised(
        connection, mutations=True
    ):
        m2.insert(connection, TABLE, _raw_row(dataset_id="dataset-unguarded"))
    assert m2.count(connection, TABLE) == 2


def test_update_delete_and_replace_are_refused_even_under_a_valid_fence(
    owned: m2.Owned,
) -> None:
    _observe(owned, _observation(), at_us=BASE_US)
    connection = owned.connection
    stored = connection.execute(f"SELECT * FROM {TABLE}").fetchall()
    for statement in (
        f"UPDATE {TABLE} SET completeness = 'partial'",
        f"UPDATE {TABLE} SET workspace_id = workspace_id",
        f"UPDATE OR REPLACE {TABLE} SET state_generation = 2",
        f"DELETE FROM {TABLE}",
    ):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"), _fenced(owned) as fenced:
            fenced.execute(statement)
    # REPLACE would delete the conflicting row without the DELETE trigger; the
    # generation rule refuses it first.
    with pytest.raises(sqlite3.DatabaseError, match=GENERATION), _fenced(owned) as fenced:
        fenced.execute(f"INSERT OR REPLACE INTO {TABLE} SELECT * FROM {TABLE}")
    assert connection.execute(f"SELECT * FROM {TABLE}").fetchall() == stored


def test_generation_gaps_and_repeats_are_refused_for_each_dataset(owned: m2.Owned) -> None:
    with _fenced(owned) as fenced:
        _write_raw(fenced, at_us=BASE_US)
    for generation in (1, 3, 0, -1):
        _refused(owned, GENERATION, at_us=BASE_US + 10, state_generation=generation)
    _refused(owned, GENERATION, at_us=BASE_US + 10, dataset_id="dataset-other", state_generation=2)
    with _fenced(owned) as fenced:
        _write_raw(fenced, at_us=BASE_US + 20, state_generation=2)
        _write_raw(fenced, at_us=BASE_US + 21, dataset_id="dataset-other", state_generation=1)
    assert owned.connection.execute(
        f"SELECT dataset_id, state_generation FROM {TABLE} ORDER BY dataset_id, state_generation"
    ).fetchall() == [("dataset-other", 1), ("dataset-raw", 1), ("dataset-raw", 2)]


def test_a_revision_or_incarnation_change_continues_the_dataset_generation_stream(
    owned: m2.Owned,
) -> None:
    """A dataset's generation counts its observations, not its revisions or incarnations:
    r1/inc1, r2/inc1 and r2/inc2 are generations 1, 2 and 3, and a second dataset starts
    its own stream at 1."""
    stated = [
        ("dataset-invoices-r1", "incarnation-1"),
        ("dataset-invoices-r2", "incarnation-1"),
        ("dataset-invoices-r2", "incarnation-2"),
    ]
    for position, (revision, incarnation) in enumerate(stated, start=1):
        observation = _observation(dataset_revision=revision, dataset_incarnation=incarnation)
        assert _observe(owned, observation, at_us=BASE_US + position) == position
    current = _current(owned.connection, "dataset-invoices")
    assert current.state_generation == 3
    assert (current.observation.dataset_revision, current.observation.dataset_incarnation) == (
        "dataset-invoices-r2",
        "incarnation-2",
    )
    assert [record.state_generation for record in _history(owned.connection, "dataset-invoices")] == [
        1,
        2,
        3,
    ]

    assert _observe(owned, _observation(dataset_id="dataset-other"), at_us=BASE_US + 4) == 1

    # A raw attempt to reset the stream, or to repeat one of its generations, is refused.
    _refused(
        owned, GENERATION, at_us=BASE_US + 10, dataset_id="dataset-invoices", state_generation=1
    )
    _refused(
        owned, GENERATION, at_us=BASE_US + 11, dataset_id="dataset-invoices", state_generation=3
    )
    assert [record.state_generation for record in _history(owned.connection, "dataset-invoices")] == [
        1,
        2,
        3,
    ]


def test_cross_workspace_and_mismatched_audit_references_are_refused(
    refusing: m2.Owned,
) -> None:
    _admitted_then_undone(refusing)
    # A row for another workspace fails the guard's workspace binding.
    _refused(refusing, UNGUARDED, workspace_id=m2.OTHER_WORKSPACE_ID)
    # An audit reference this workspace never recorded, such as another workspace's.
    _refused(refusing, AUDIT, audit_ref="aud-recorded-elsewhere")
    # Its own audit event, but at another instant.
    _refused(refusing, AUDIT, recorded_at_us=BASE_US + 1)
    # A refused or failed request accounts for nothing.
    for outcome_class in ("refused", "failed"):
        with pytest.raises(sqlite3.DatabaseError, match=AUDIT), _fenced(refusing) as fenced:
            ref = _audit(
                fenced, ref="aud-dss-unsettled", at_us=BASE_US, outcome_class=outcome_class
            )
            m2.insert(fenced, TABLE, _raw_row(audit_ref=ref))
    assert m2.count(refusing.connection, TABLE) == 0


def test_the_composite_audit_key_alone_refuses_a_cross_workspace_reference() -> None:
    """With every trigger absent, the foreign key by itself binds the workspace."""
    declarations = [
        statement
        for migration in load_migrations()
        if migration.version in (7, MIGRATION_VERSION)
        for statement in split_sql_statements(migration.sql)
        if " ".join(statement.split()).startswith(("CREATE TABLE", "CREATE UNIQUE INDEX"))
    ]
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        for statement in declarations:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO omnivia_application_audit_events (audit_ref, workspace_id, "
            "principal_id, operation, purpose, request_id, correlation_id, trace_id, "
            "granted_authority_json, outcome_class, error_code, recorded_at_us) "
            "VALUES ('aud-elsewhere', ?, 'p', 'o', 'p', 'r', 'c', 't', '{}', "
            "'succeeded', NULL, ?)",
            (m2.OTHER_WORKSPACE_ID, BASE_US),
        )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
            m2.insert(connection, TABLE, _raw_row(audit_ref="aud-elsewhere"))
        m2.insert(
            connection,
            TABLE,
            _raw_row(audit_ref="aud-elsewhere", workspace_id=m2.OTHER_WORKSPACE_ID),
        )
        assert connection.execute(f"SELECT workspace_id FROM {TABLE}").fetchall() == [
            (m2.OTHER_WORKSPACE_ID,)
        ]
    finally:
        connection.close()


# --- the schema boundary: shape, vocabularies and evidence ---------------------------

MALFORMED: tuple[tuple[str, dict[str, object], str], ...] = (
    ("empty dataset id", {"dataset_id": ""}, CHECK),
    ("dataset id with a space", {"dataset_id": "dataset raw"}, CHECK),
    ("dataset id with a leading separator", {"dataset_id": "-dataset"}, CHECK),
    ("overlong dataset revision", {"dataset_revision": "r" * 129}, CHECK),
    (
        "missing dataset incarnation",
        {"dataset_incarnation": None},
        _not_null("dataset_incarnation"),
    ),
    ("manifest id without revision or digest", {"manifest_id": "manifest-1"}, CHECK),
    ("manifest digest without its identity", {"manifest_digest": MANIFEST_DIGEST}, CHECK),
    (
        "manifest digest of another algorithm",
        {"manifest_id": "m-1", "manifest_revision": "r-1", "manifest_digest": "sha512:" + "a" * 64},
        CHECK,
    ),
    ("authority epoch with a space", {"observed_authority_epoch": "epoch 7"}, CHECK),
    # The documents carry the scope digest too, so a malformed row scope disagrees with
    # them before the column's own CHECK is read.
    ("uppercase scope digest", {"scope_digest": "sha256:" + "A" * 64}, BOUND),
    ("short scope digest", {"scope_digest": "sha256:" + "a" * 63}, BOUND),
    ("unprefixed scope digest", {"scope_digest": "a" * 71}, BOUND),
    ("malformed coverage digest", {"coverage_digest": "sha256:not-hex"}, CHECK),
    (
        "missing source observation digest",
        {"source_observation_digest": None},
        _not_null("source_observation_digest"),
    ),
    ("missing coverage", {"coverage_json": None}, _not_null("coverage_json")),
    (
        "missing source observation",
        {"source_observation_json": None},
        _not_null("source_observation_json"),
    ),
    # The source observation names the instant the row was verified at, so a zero row
    # disagrees with it before the column's own CHECK is read.
    ("zero verification instant", {"verified_at_us": 0}, BOUND),
    ("fractional verification instant", {"verified_at_us": 1.5}, CHECK),
    ("textual verification instant", {"verified_at_us": "soon"}, CHECK),
    ("missing verification instant", {"verified_at_us": None}, _not_null("verified_at_us")),
    ("negative freshness deadline", {"freshness_deadline_at_us": -1}, CHECK),
    ("fractional freshness deadline", {"freshness_deadline_at_us": 2.5}, CHECK),
    ("recorded instant that is not its audit's", {"recorded_at_us": 0}, AUDIT),
)


@pytest.mark.parametrize(
    ("overrides", "refusal"),
    [pytest.param(overrides, refusal, id=name) for name, overrides, refusal in MALFORMED],
)
def test_the_schema_refuses_malformed_identity_digests_and_instants(
    refusing: m2.Owned, overrides: dict[str, object], refusal: str
) -> None:
    # The baseline row is admitted, so each refusal is about its overrides alone.
    _admitted_then_undone(refusing)
    _refused(refusing, refusal, **overrides)


def _without(document: dict[str, Any], key: str) -> dict[str, Any]:
    return {name: value for name, value in document.items() if name != key}


#: Each way a coverage document can leave its closed shape, as the document it makes.
COVERAGE_REFUSALS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("missing proof kind", _without(_coverage(), "proof_kind")),
    ("extra field", _coverage(note="free text")),
    ("count as text", _coverage(accepted_rows="12")),
    ("count as boolean", _coverage(rejected_rows=False)),
    ("count as fraction", _coverage(conflicting_rows=1.5)),
    ("count past int64", _coverage(accepted_rows=2**63)),
    ("negative count", _coverage(deduplicated_rows=-1)),
    ("null count", _coverage(accepted_rows=None)),
    ("negative expected source rows", _coverage(expected_source_rows=-5)),
    ("proof kind outside its vocabulary", _coverage(proof_kind="complete")),
    ("proof kind in another case", _coverage(proof_kind="Complete_Enumeration")),
    ("proof refs as text", _coverage(proof_refs="listing-2026-10-04")),
    ("proof refs past 64 entries", _coverage(proof_refs=[f"proof-{index}" for index in range(65)])),
    ("duplicate proof refs", _coverage(proof_refs=["proof-1", "proof-1"])),
    ("proof ref with a space", _coverage(proof_refs=["listing 1"])),
    ("proof ref past 128 characters", _coverage(proof_refs=["p" * 129])),
    ("proof ref that is an integer", _coverage(proof_refs=[1])),
    ("scope digest in capitals", _coverage(scope_digest="sha256:" + "A" * 64)),
    ("scope digest of another algorithm", _coverage(scope_digest="sha512:" + "5" * 64)),
)

#: Each way a source observation can leave its closed shape, as the document it makes.
SOURCE_REFUSALS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("missing evidence kind", _without(_source(), "evidence_kind")),
    ("extra field", _source(note="free text")),
    ("source reference missing its revision", _source(source_ref={"id": "source-erp"})),
    (
        "source reference id as an integer",
        _source(source_ref={"id": 7, "revision_id": "source-erp-r4"}),
    ),
    (
        "source reference with a space",
        _source(source_ref={"id": "source erp", "revision_id": "source-erp-r4"}),
    ),
    (
        "interval that runs backward",
        _source(
            observation_interval={
                "start_inclusive_at_us": BASE_US,
                "end_exclusive_at_us": BASE_US - 1,
            }
        ),
    ),
    (
        "interval that is empty",
        _source(
            observation_interval={"start_inclusive_at_us": BASE_US, "end_exclusive_at_us": BASE_US}
        ),
    ),
    ("interval as a list", _source(observation_interval=[BASE_US - 1, BASE_US])),
    (
        "interval with a third bound",
        _source(
            observation_interval={
                "start_inclusive_at_us": BASE_US - 1,
                "end_exclusive_at_us": BASE_US,
                "midpoint_at_us": BASE_US - 1,
            }
        ),
    ),
    (
        "zero interval start",
        _source(observation_interval={"start_inclusive_at_us": 0, "end_exclusive_at_us": BASE_US}),
    ),
    ("zero source cutoff", _source(source_cutoff_at_us=0)),
    ("negative verification instant", _source(verification_at_us=-1)),
    ("evidence kind outside its vocabulary", _source(evidence_kind="full_scan")),
    ("snapshot token shaped as a URL", _source(snapshot_token_ref="https://source.invalid/v1")),
    ("checkpoint as an integer", _source(applied_checkpoint_ref=42)),
    ("incarnation with a space", _source(source_incarnation="incarnation 1")),
    ("scope digest in capitals", _source(scope_digest="sha256:" + "A" * 64)),
    ("evidence refs past 64 entries", _source(evidence_refs=[f"evidence-{index}" for index in range(65)])),
    ("duplicate evidence refs", _source(evidence_refs=["evidence-1", "evidence-1"])),
)

#: The payload classes the closed shapes exist to refuse. Each carries something other
#: than the shape's own fields, so it is refused by its keys before any value is read.
PROHIBITED_PAYLOADS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("an endpoint's host and port", "source_observation", _source(host="db.internal", port=5432)),
    (
        "a credential-shaped token",
        "source_observation",
        _source(access_token="tok_0123456789abcdef"),
    ),
    (
        "a raw row of identifiers and integers",
        "coverage",
        _coverage(row={"customer_id": "cust-1", "amount_cents": 1234}),
    ),
    (
        "a tokenized SQL array",
        "source_observation",
        _source(tokens=["SELECT", "*", "FROM", "invoices"]),
    ),
    ("a business-value payload", "coverage", _coverage(balance_cents=123456)),
)

LISTING = '"listing-2026-10-04"'
#: Sixty-four identifiers of 128 characters: the largest list the grammar admits, which
#: the byte bound refuses.
_OVERSIZED_COVERAGE = _text(
    _coverage(proof_refs=[f"{index:03d}-" + "p" * 124 for index in range(64)])
)
#: The source interval with its bounds in canonical key order, for the out-of-order rows.
_ORDERED_INTERVAL = {
    "end_exclusive_at_us": BASE_US,
    "start_inclusive_at_us": BASE_US - 60_000_000,
}

#: Raw text the shapes do not admit, as (document, label, text, refusal). An escape is
#: decoded before the shape reads it, so an escape that decodes to a valid value is
#: refused by the no-escape CHECK. Whether a trailing NUL reaches the JSON parser or the
#: CHECK first depends on the SQLite build, so that row accepts either refusal.
RAW_EVIDENCE: tuple[tuple[str, str, str, str], ...] = (
    (
        "coverage",
        "a raw NUL inside a reference",
        COVERAGE_JSON.replace(LISTING, '"listing\x00-2026"'),
        JSON_FORM,
    ),
    ("coverage", "a raw NUL after the document", COVERAGE_JSON + "\x00", f"{JSON_FORM}|{CHECK}"),
    (
        "source_observation",
        "a raw NUL inside an identifier",
        SOURCE_JSON.replace('"source-erp"', '"source\x00erp"'),
        JSON_FORM,
    ),
    (
        "coverage",
        "a raw Unicode field name",
        COVERAGE_JSON.replace('"proof_kind"', '"proof_kİnd"'),
        COVERAGE_SHAPE,
    ),
    (
        "coverage",
        "a raw Unicode value",
        COVERAGE_JSON.replace("complete_enumeration", "complete_enumeración"),
        COVERAGE_SHAPE,
    ),
    (
        "source_observation",
        "a raw Unicode identifier",
        SOURCE_JSON.replace('"source-erp"', '"fuente-ñ"'),
        SOURCE_SHAPE,
    ),
    (
        "coverage",
        "an escaped field name that decodes to a valid one",
        COVERAGE_JSON.replace('"proof_kind"', '"\\u0070roof_kind"'),
        CHECK,
    ),
    (
        "coverage",
        "an escaped value that decodes to a valid one",
        COVERAGE_JSON.replace("complete_enumeration", "complete\\u005fenumeration"),
        CHECK,
    ),
    (
        "coverage",
        "an escaped field name outside the shape",
        COVERAGE_JSON.replace('"proof_kind"', '"\\u00e9"'),
        COVERAGE_SHAPE,
    ),
    (
        "source_observation",
        "an escaped value outside the vocabulary",
        SOURCE_JSON.replace('"snapshot"', '"\\u00e9napshot"'),
        SOURCE_SHAPE,
    ),
    (
        "coverage",
        "a field repeated under its own name",
        COVERAGE_JSON.replace('"rejected_rows":0', '"rejected_rows":0,"rejected_rows":0'),
        COVERAGE_SHAPE,
    ),
    (
        "coverage",
        "a negative zero",
        COVERAGE_JSON.replace('"rejected_rows":0', '"rejected_rows":-0'),
        CHECK,
    ),
    ("coverage", "fields out of canonical order", _unsorted(_coverage()), COVERAGE_SHAPE),
    (
        "source_observation",
        "nested fields out of canonical order",
        SOURCE_JSON.replace(
            '{"id":"source-erp","revision_id":"source-erp-r4"}',
            '{"revision_id":"source-erp-r4","id":"source-erp"}',
        ),
        SOURCE_SHAPE,
    ),
    (
        "source_observation",
        "top-level fields out of canonical order",
        _unsorted(_source(observation_interval=_ORDERED_INTERVAL)),
        SOURCE_SHAPE,
    ),
    (
        "source_observation",
        "interval fields out of canonical order",
        SOURCE_JSON.replace(to_canonical_json(_ORDERED_INTERVAL), _unsorted(_ORDERED_INTERVAL)),
        SOURCE_SHAPE,
    ),
    ("coverage", "a document that is not minified", COVERAGE_JSON.replace(",", ", "), CHECK),
    (
        "coverage",
        "a NaN count",
        COVERAGE_JSON.replace('"accepted_rows":12', '"accepted_rows":NaN'),
        JSON_FORM,
    ),
    ("coverage", "an array as the document", '["listing-2026-10-04"]', OBJECT_FORM),
    ("source_observation", "a string as the document", '"source-erp"', OBJECT_FORM),
    (
        "coverage",
        "a payload nested past the ceiling",
        '{"accepted_rows":' + "[" * 40 + "]" * 40 + "}",
        TOO_DEEP,
    ),
    (
        "source_observation",
        "a payload nested past the ceiling",
        '{"source_ref":' + "[" * 40 + "]" * 40 + "}",
        TOO_DEEP,
    ),
    ("coverage", "a document past the byte bound", _OVERSIZED_COVERAGE, CHECK),
)


@pytest.mark.parametrize(
    ("label", "document"),
    [pytest.param(label, document, id=label) for label, document in COVERAGE_REFUSALS],
)
def test_the_writer_refuses_a_coverage_document_outside_its_shape(
    refusing: m2.Owned, label: str, document: dict[str, Any]
) -> None:
    with pytest.raises(
        dataset_state.DatasetStateInvalid, match=r"invalid dataset state fields: coverage[.]"
    ):
        _observe(refusing, _observation(coverage=document), at_us=BASE_US)
    assert m2.count(refusing.connection, TABLE) == 0


@pytest.mark.parametrize(
    ("label", "document"),
    [pytest.param(label, document, id=label) for label, document in COVERAGE_REFUSALS],
)
def test_the_schema_refuses_a_coverage_document_outside_its_shape(
    refusing: m2.Owned, label: str, document: dict[str, Any]
) -> None:
    _admitted_then_undone(refusing)
    _refused(refusing, COVERAGE_SHAPE, coverage_json=_text(document))


@pytest.mark.parametrize(
    ("label", "document"),
    [pytest.param(label, document, id=label) for label, document in SOURCE_REFUSALS],
)
def test_the_writer_refuses_a_source_observation_outside_its_shape(
    refusing: m2.Owned, label: str, document: dict[str, Any]
) -> None:
    with pytest.raises(
        dataset_state.DatasetStateInvalid,
        match=r"invalid dataset state fields: source_observation[.]",
    ):
        _observe(refusing, _observation(source_observation=document), at_us=BASE_US)
    assert m2.count(refusing.connection, TABLE) == 0


@pytest.mark.parametrize(
    ("label", "document"),
    [pytest.param(label, document, id=label) for label, document in SOURCE_REFUSALS],
)
def test_the_schema_refuses_a_source_observation_outside_its_shape(
    refusing: m2.Owned, label: str, document: dict[str, Any]
) -> None:
    _admitted_then_undone(refusing)
    _refused(refusing, SOURCE_SHAPE, source_observation_json=_text(document))


@pytest.mark.parametrize(
    ("label", "field", "document"),
    [
        pytest.param(label, field, document, id=label)
        for label, field, document in PROHIBITED_PAYLOADS
    ],
)
def test_the_storage_boundary_refuses_each_prohibited_payload_class(
    refusing: m2.Owned, label: str, field: str, document: dict[str, Any]
) -> None:
    with pytest.raises(
        dataset_state.DatasetStateInvalid,
        match=rf"invalid dataset state fields: {field}[.]keys",
    ):
        _observe(refusing, _observation(**{field: document}), at_us=BASE_US)
    _admitted_then_undone(refusing)
    refusal = COVERAGE_SHAPE if field == "coverage" else SOURCE_SHAPE
    _refused(refusing, refusal, **{f"{field}_json": _text(document)})
    assert m2.count(refusing.connection, TABLE) == 0


@pytest.mark.parametrize(
    ("base", "label", "text", "refusal"),
    [
        pytest.param(base, label, text, refusal, id=label)
        for base, label, text, refusal in RAW_EVIDENCE
    ],
)
def test_the_schema_refuses_raw_evidence_text_the_shapes_do_not_admit(
    refusing: m2.Owned, base: str, label: str, text: str, refusal: str
) -> None:
    _admitted_then_undone(refusing)
    _refused(refusing, refusal, **{f"{base}_json": text})


def _nested(field: str, depth: int) -> str:
    """An object whose `field` holds empty arrays nested `depth` levels deep, the root being
    level 0."""
    return '{"' + field + '":' + "[" * depth + "]" * depth + "}"


@pytest.mark.parametrize(
    ("base", "field", "depth", "refusal"),
    [
        pytest.param(base, field, depth, refusal, id=f"{base}-{depth}")
        for base, field, shape in (
            ("coverage", "accepted_rows", COVERAGE_SHAPE),
            ("source_observation", "source_ref", SOURCE_SHAPE),
        )
        for depth, refusal in ((32, shape), (33, TOO_DEEP))
    ],
)
def test_the_depth_ceiling_is_exactly_32_levels(
    refusing: m2.Owned, base: str, field: str, depth: int, refusal: str
) -> None:
    """A document 32 levels deep passes the depth walk and is refused only by its shape; one
    33 levels deep is refused as too deep."""
    _admitted_then_undone(refusing)
    _refused(refusing, refusal, **{f"{base}_json": _nested(field, depth)})


def test_each_document_is_bound_to_the_row_it_describes(refusing: m2.Owned) -> None:
    _admitted_then_undone(refusing)
    other = "sha256:" + "8" * 64
    _refused(refusing, BOUND, coverage_json=_text(_coverage(scope_digest=other)))
    _refused(refusing, BOUND, source_observation_json=_text(_source(scope_digest=other)))
    _refused(
        refusing,
        BOUND,
        source_observation_json=_text(_source(verification_at_us=BASE_US + 1)),
    )
    _refused(refusing, BOUND, scope_digest=other)
    _refused(refusing, BOUND, verified_at_us=BASE_US + 1)
    for observation, match in (
        (_observation(coverage=_coverage(scope_digest=other)), r"coverage[.]scope_digest"),
        (
            _observation(source_observation=_source(scope_digest=other)),
            r"source_observation[.]scope_digest",
        ),
        (
            _observation(source_observation=_source(verification_at_us=BASE_US + 1)),
            r"source_observation[.]verification_at_us",
        ),
        (_observation(scope_digest=other), r"coverage[.]scope_digest"),
    ):
        with pytest.raises(dataset_state.DatasetStateInvalid, match=match):
            _observe(refusing, observation, at_us=BASE_US)
    assert m2.count(refusing.connection, TABLE) == 0


@pytest.mark.parametrize(
    ("label", "field", "document"),
    [
        pytest.param(label, field, document, id=label)
        for field, label, document in (
            ("coverage", "an array as the document", ["listing-2026-10-04"]),
            ("coverage", "bytes in a field", _coverage(note=b"raw")),
            ("coverage", "a count that is not finite", _coverage(accepted_rows=float("nan"))),
            ("source_observation", "a string as the document", "source-erp"),
        )
    ],
)
def test_the_writer_refuses_a_document_that_is_not_its_object_or_not_json(
    refusing: m2.Owned, label: str, field: str, document: object
) -> None:
    with pytest.raises(
        dataset_state.DatasetStateInvalid, match=rf"invalid dataset state fields: {field}"
    ):
        _observe(refusing, _observation(**{field: document}), at_us=BASE_US)
    assert m2.count(refusing.connection, TABLE) == 0


def test_the_largest_closed_documents_are_admitted_and_read_back_exactly(
    owned: m2.Owned,
) -> None:
    """Every bound at its edge that still fits the byte bound: 64 entries of 104
    characters, identifiers at 128, and every instant and count at int64's largest."""
    coverage = _coverage(
        accepted_rows=MAX_INSTANT,
        proof_kind="bounded_observation",
        expected_source_rows=None,
        proof_refs=[f"{index:03d}-" + "r" * 100 for index in range(64)],
    )
    source = _source(
        source_ref={"id": "s" * 128, "revision_id": "r" * 128},
        source_incarnation=None,
        observation_interval={"start_inclusive_at_us": 1, "end_exclusive_at_us": MAX_INSTANT},
        source_cutoff_at_us=MAX_INSTANT,
        verification_at_us=MAX_INSTANT,
        evidence_kind="none",
        snapshot_token_ref=None,
        applied_checkpoint_ref="c" * 128,
        evidence_refs=[f"{index:03d}-" + "e" * 100 for index in range(64)],
    )
    coverage_text = to_canonical_json(coverage)
    source_text = to_canonical_json(source)
    assert len(coverage_text.encode("utf-8")) <= dataset_state.EVIDENCE_MAX_BYTES
    assert len(source_text.encode("utf-8")) <= dataset_state.EVIDENCE_MAX_BYTES

    observation = _observation(
        coverage=coverage, source_observation=source, verified_at_us=MAX_INSTANT
    )
    assert _observe(owned, observation, at_us=MAX_INSTANT) == 1
    record = _current(owned.connection, "dataset-invoices")
    assert record.observation == observation
    assert (record.coverage_digest, record.source_observation_digest) == (
        content_digest(coverage_text),
        content_digest(source_text),
    )

    # The same documents, written raw, are admitted by the schema as well.
    _admitted_then_undone(
        owned,
        at_us=MAX_INSTANT,
        verified_at_us=MAX_INSTANT,
        **_stored("coverage", coverage_text),
        **_stored("source_observation", source_text),
    )


def test_the_module_vocabularies_are_the_accepted_ones() -> None:
    assert {
        "initial_readiness": dataset_state.INITIAL_READINESS,
        "completeness": dataset_state.COMPLETENESS,
        "continuity": dataset_state.CONTINUITY,
        "operational_health": dataset_state.OPERATIONAL_HEALTH,
        "schema_compatibility": dataset_state.SCHEMA_COMPATIBILITY,
        "evidence_availability": dataset_state.EVIDENCE_AVAILABILITY,
        "content_observation": dataset_state.CONTENT_OBSERVATION,
    } == {dimension: frozenset(values) for dimension, values in VOCABULARIES.items()}


def test_every_accepted_value_of_every_dimension_is_stored_as_stated(owned: m2.Owned) -> None:
    at_us = BASE_US
    for dimension, values in VOCABULARIES.items():
        for value in values:
            at_us += 1
            observation = _observation(dataset_id=f"dataset-{dimension}", **{dimension: value})
            _observe(owned, observation, at_us=at_us)
        history = _history(owned.connection, f"dataset-{dimension}")
        assert tuple(getattr(record.observation, dimension) for record in history) == values


def test_the_module_evidence_vocabularies_are_the_accepted_ones() -> None:
    assert {
        "proof_kind": dataset_state.PROOF_KINDS,
        "evidence_kind": dataset_state.EVIDENCE_KINDS,
    } == {dimension: frozenset(values) for dimension, values in EVIDENCE_VOCABULARIES.items()}


def test_every_accepted_proof_kind_and_evidence_kind_is_stored_as_stated(
    owned: m2.Owned,
) -> None:
    at_us = BASE_US
    for value in EVIDENCE_VOCABULARIES["proof_kind"]:
        at_us += 1
        _observe(
            owned,
            _observation(dataset_id=f"dataset-proof-{value}", coverage=_coverage(proof_kind=value)),
            at_us=at_us,
        )
    for value in EVIDENCE_VOCABULARIES["evidence_kind"]:
        at_us += 1
        _observe(
            owned,
            _observation(
                dataset_id=f"dataset-evidence-{value}",
                source_observation=_source(evidence_kind=value),
            ),
            at_us=at_us,
        )
    for value in EVIDENCE_VOCABULARIES["proof_kind"]:
        record = _current(owned.connection, f"dataset-proof-{value}")
        assert record.observation.coverage["proof_kind"] == value
    for value in EVIDENCE_VOCABULARIES["evidence_kind"]:
        record = _current(owned.connection, f"dataset-evidence-{value}")
        assert record.observation.source_observation["evidence_kind"] == value


@pytest.mark.parametrize("dimension", sorted(VOCABULARIES))
def test_every_value_outside_a_closed_vocabulary_is_refused(
    refusing: m2.Owned, dimension: str
) -> None:
    accepted = set(VOCABULARIES[dimension])
    other_dimensions = {value for values in VOCABULARIES.values() for value in values}
    outsiders: set[object] = (
        other_dimensions
        | {value.upper() for value in accepted}
        | {value.title() for value in accepted}
        | {f" {value}" for value in accepted}
        | {f"{value} " for value in accepted}
        | {value.replace("_", "-") for value in accepted}
        | {"", "initializing", "fresh", "stale", "current", "ok", "true", "null", "none"}
    ) - accepted
    _admitted_then_undone(refusing)
    for value in [*sorted(outsiders, key=str), None, 1, 0.5]:
        _refused(refusing, f"{CHECK}|{_not_null(dimension)}", **{dimension: value})
        with pytest.raises(dataset_state.DatasetStateInvalid, match=dimension):
            _observe(refusing, _observation(**{dimension: value}), at_us=BASE_US)
    assert m2.count(refusing.connection, TABLE) == 0


# --- the dimensions stay independent -------------------------------------------------


def test_empty_content_does_not_imply_complete_coverage(owned: m2.Owned) -> None:
    stated = [
        ("empty", "partial"),
        ("empty", "unknown"),
        ("empty", "complete"),
        ("nonempty", "unknown"),
    ]
    for position, (content, completeness) in enumerate(stated, start=1):
        _observe(
            owned,
            _observation(content_observation=content, completeness=completeness),
            at_us=BASE_US + position,
        )
    assert [
        (record.observation.content_observation, record.observation.completeness)
        for record in _history(owned.connection, "dataset-invoices")
    ] == stated


def test_source_and_health_availability_do_not_imply_freshness(owned: m2.Owned) -> None:
    available = {
        "initial_readiness": "ready",
        "operational_health": "healthy",
        "evidence_availability": "available",
    }
    stated = [
        _observation(**available, freshness_deadline_at_us=None),
        _observation(**available, verified_at_us=BASE_US, freshness_deadline_at_us=BASE_US - 1),
        _observation(
            operational_health="unavailable",
            evidence_availability="unavailable",
            freshness_deadline_at_us=BASE_US + 10**12,
        ),
    ]
    for position, observation in enumerate(stated, start=1):
        _observe(owned, observation, at_us=BASE_US + position)
    history = _history(owned.connection, "dataset-invoices")
    assert [record.observation for record in history] == stated

    # Nothing is derived: the projection is the observation column for column, and
    # the stored shape has no freshness, readiness or currentness verdict to read.
    assert _columns(owned.connection, VIEW) == _columns(owned.connection, TABLE)
    assert {field.name for field in dataclasses.fields(dataset_state.DatasetStateRecord)} == {
        "workspace_id",
        "state_generation",
        "observation",
        "coverage_digest",
        "source_observation_digest",
        "recorded_at_us",
        "audit_ref",
    }
    # A clock value alone is not freshness evidence: a deadline without its scope
    # digest or its source observation is not storable.
    _refused(
        owned,
        _not_null("source_observation_json"),
        freshness_deadline_at_us=BASE_US + 1,
        source_observation_json=None,
    )
    _refused(
        owned,
        _not_null("scope_digest"),
        freshness_deadline_at_us=BASE_US + 1,
        scope_digest=None,
    )


def test_every_pair_of_dimensions_combines_freely(owned: m2.Owned) -> None:
    """No constraint couples one dimension to another: every pair of values coexists."""
    dimensions = sorted(VOCABULARIES)
    combinations = [
        {first: left, second: right}
        for index, first in enumerate(dimensions)
        for second in dimensions[index + 1 :]
        for left in VOCABULARIES[first]
        for right in VOCABULARIES[second]
    ]
    with _fenced(owned) as fenced:
        settlement = _settlement(_audit(fenced, ref="aud-dss-pairs", at_us=BASE_US), BASE_US)
        for combination in combinations:
            dataset_state.record_observation(
                fenced,
                settlement,
                workspace_id=WORKSPACE_ID,
                observation=_observation(**combination),
            )
    history = _history(owned.connection, "dataset-invoices")
    assert [record.state_generation for record in history] == list(
        range(1, len(combinations) + 1)
    )
    for record, combination in zip(history, combinations, strict=True):
        assert {name: getattr(record.observation, name) for name in combination} == combination


# --- the current-state projection ----------------------------------------------------


def test_the_current_projection_is_each_datasets_highest_generation_and_the_log_replay(
    owned: m2.Owned,
) -> None:
    plan = [
        ("dataset-a", "not_started"),
        ("dataset-b", "initialising"),
        ("dataset-a", "initialising"),
        ("dataset-c", "blocked"),
        ("dataset-a", "catching_up"),
        ("dataset-c", "catching_up"),
        ("dataset-a", "ready"),
    ]
    for position, (dataset_id, readiness) in enumerate(plan, start=1):
        _observe(
            owned,
            _observation(dataset_id=dataset_id, initial_readiness=readiness),
            at_us=BASE_US + position,
        )
    connection = owned.connection
    for dataset_id in ("dataset-a", "dataset-b", "dataset-c"):
        history = _history(connection, dataset_id)
        stated = [readiness for name, readiness in plan if name == dataset_id]
        assert [record.state_generation for record in history] == list(range(1, len(stated) + 1))
        assert [record.observation.initial_readiness for record in history] == stated
        assert _current(connection, dataset_id) == history[-1]
    assert _current(connection, "dataset-never-observed") is None
    assert _projected(connection) == _replayed(connection)
    assert len(_projected(connection)) == 3


def test_the_log_and_its_projection_survive_backup_and_restore(
    owned: m2.Owned, tmp_path: Path
) -> None:
    for position, completeness in enumerate(("unknown", "partial", "complete"), start=1):
        _observe(owned, _observation(completeness=completeness), at_us=BASE_US + position)
    _observe(owned, _observation(dataset_id="dataset-b"), at_us=BASE_US + 4)
    history = _history(owned.connection, "dataset-invoices")
    projected = _projected(owned.connection)
    owned.connection.close()

    attempt = new_attempt_id()
    backup = backup_database(owned.path, tmp_path / "backups" / f"{attempt}.sqlite")
    assert verify_backup(owned.path, backup, attempt).verified
    restored = m2.take_ownership(restore_backup(backup, tmp_path / "restored" / "workspace.sqlite"))
    try:
        connection = restored.connection
        assert _history(connection, "dataset-invoices") == history
        assert _projected(connection) == projected == _replayed(connection)
        assert_guards_intact(connection)
        assert fingerprint_schema(connection).matches(canonical_schema_fingerprint())
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
        assert _observe(restored, _observation(), at_us=BASE_US + 5) == 4
    finally:
        restored.connection.close()


# --- the storage module --------------------------------------------------------------


def test_evidence_is_stored_as_canonical_json_bound_to_its_digest(owned: m2.Owned) -> None:
    observation = _observation(
        coverage=_coverage(
            proof_kind="contiguous_log",
            expected_source_rows=None,
            proof_refs=["listing-b", "listing-a"],
        ),
        source_observation=_source(evidence_refs=["evidence-2", "evidence-1"]),
    )
    _observe(owned, observation, at_us=BASE_US + 1)
    row = owned.connection.execute(
        f"SELECT coverage_json, coverage_digest, source_observation_json, "
        f"source_observation_digest, recorded_at_us, audit_ref FROM {TABLE}"
    ).fetchone()
    assert row[0] == to_canonical_json(observation.coverage)
    assert row[1] == content_digest(row[0])
    assert row[2] == to_canonical_json(observation.source_observation)
    assert row[3] == content_digest(row[2])
    assert row[4:] == (BASE_US + 1, f"aud-dss-{BASE_US + 1}")
    record = _current(owned.connection, "dataset-invoices")
    assert record.observation == observation
    assert (record.coverage_digest, record.source_observation_digest) == (row[1], row[3])


#: Stored rows the reader must refuse, each written past the writer and the trigger. A row
#: under its own digest is still refused when its shape or its bindings do not hold.
STORED_CONTRADICTIONS: tuple[tuple[str, dict[str, object], str], ...] = (
    ("a digest of other bytes", {"coverage_digest": content_digest('{"x":1}')}, "does not verify"),
    (
        "fields out of canonical order under their own digest",
        _stored("coverage", _unsorted(_coverage())),
        "does not verify",
    ),
    (
        "a field outside the shape under its own digest",
        _stored("coverage", _text(_coverage(note="x"))),
        r"coverage[.]keys",
    ),
    (
        "a count past int64 under its own digest",
        _stored("coverage", _text(_coverage(accepted_rows=2**63))),
        r"coverage[.]accepted_rows",
    ),
    (
        "a scope contradicting its row",
        _stored("coverage", _text(_coverage(scope_digest="sha256:" + "8" * 64))),
        r"coverage[.]scope_digest",
    ),
    (
        "a verification contradicting its row",
        _stored("source_observation", _text(_source(verification_at_us=BASE_US + 1))),
        r"source_observation[.]verification_at_us",
    ),
    (
        "a backward interval under its own digest",
        _stored(
            "source_observation",
            _text(
                _source(
                    observation_interval={
                        "start_inclusive_at_us": BASE_US,
                        "end_exclusive_at_us": BASE_US - 1,
                    }
                )
            ),
        ),
        r"source_observation[.]observation_interval",
    ),
)


@pytest.mark.parametrize(
    ("label", "overrides", "refusal"),
    [
        pytest.param(label, overrides, refusal, id=label)
        for label, overrides, refusal in STORED_CONTRADICTIONS
    ],
)
def test_a_stored_row_that_does_not_verify_or_contradicts_itself_is_refused_on_read(
    label: str, overrides: dict[str, object], refusal: str
) -> None:
    connection = _bare_table()
    try:
        m2.insert(connection, TABLE, _raw_row(**overrides))
        with pytest.raises(dataset_state.DatasetStateInvalid, match=refusal):
            _history(connection, "dataset-raw")
    finally:
        connection.close()


def _refused_by_every_read(connection: sqlite3.Connection, refusal: str) -> None:
    with pytest.raises(dataset_state.DatasetStateInvalid, match=refusal):
        _history(connection, "dataset-raw")
    with pytest.raises(dataset_state.DatasetStateInvalid, match=refusal):
        _current(connection, "dataset-raw")


def test_a_stored_document_past_the_byte_bound_is_refused_by_every_read() -> None:
    """Canonical, correctly digested and closed-shape, the document is stored only because the
    CHECKs are disabled here; the reader refuses it on its bytes, before any decoding."""
    assert len(_OVERSIZED_COVERAGE.encode("utf-8")) > dataset_state.EVIDENCE_MAX_BYTES
    connection = _bare_table()
    try:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        m2.insert(connection, TABLE, _raw_row(**_stored("coverage", _OVERSIZED_COVERAGE)))
        _refused_by_every_read(connection, STORED_BOUND)
    finally:
        connection.close()


def test_stored_evidence_that_is_not_text_is_refused_by_every_read() -> None:
    connection = _bare_table()
    try:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        m2.insert(connection, TABLE, _raw_row(coverage_json=COVERAGE_JSON.encode("utf-8")))
        _refused_by_every_read(connection, STORED_TEXT)
    finally:
        connection.close()


INVALID_OBSERVATIONS: tuple[tuple[str, dict[str, object]], ...] = (
    ("dataset_id", {"dataset_id": "dataset invoices"}),
    ("dataset_revision", {"dataset_revision": ""}),
    ("dataset_incarnation", {"dataset_incarnation": None}),
    ("manifest", {"manifest_digest": None}),
    ("manifest", {"manifest_id": None, "manifest_revision": None}),
    ("observed_authority_epoch", {"observed_authority_epoch": 7}),
    ("scope_digest", {"scope_digest": "sha256:" + "F" * 64}),
    ("verified_at_us", {"verified_at_us": 0}),
    ("verified_at_us", {"verified_at_us": True}),
    ("freshness_deadline_at_us", {"freshness_deadline_at_us": 1.5}),
    ("initial_readiness", {"initial_readiness": "initializing"}),
)

@pytest.mark.parametrize(
    ("field", "overrides"),
    [
        pytest.param(field, overrides, id=f"{field}-{index}")
        for index, (field, overrides) in enumerate(INVALID_OBSERVATIONS)
    ],
)
def test_the_writer_refuses_a_malformed_observation_and_writes_nothing(
    refusing: m2.Owned, field: str, overrides: dict[str, object]
) -> None:
    with pytest.raises(dataset_state.DatasetStateInvalid, match=field):
        _observe(refusing, _observation(**overrides), at_us=BASE_US)
    assert m2.count(refusing.connection, TABLE) == 0
