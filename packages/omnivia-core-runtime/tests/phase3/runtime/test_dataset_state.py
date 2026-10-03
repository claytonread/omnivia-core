"""DatasetState storage component (SPEC-CORE-DATA-001 WP07 PR 1; migration 0062).

Proves the additive migration, its three guards, the current-state projection and
`storage.dataset_state`. Rows are written the way a mutation settles them -- the
application audit event, then the observation, inside one caller-owned
`fenced_transaction` -- or as raw SQL where a test has to show the schema refusing
on its own. Nothing here wires an operation: `analysis.start` stays the milestone-1
refusal that `test_analysis_start_refusal.py` pins.

Refusal-only cases share one module-scoped workspace. Every refused write rolls its
whole fence back, and each case asserts that it left nothing behind.
"""

from __future__ import annotations

import dataclasses
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

SCOPE_DIGEST = "sha256:" + "5" * 64
MANIFEST_DIGEST = "sha256:" + "6" * 64
SOURCE_VERSION_DIGEST = "sha256:" + "7" * 64
COVERAGE_JSON = '{"partitions_covered":12}'
SOURCE_JSON = '{"source_version_digest":"' + SOURCE_VERSION_DIGEST + '"}'

CHECK = "CHECK constraint failed"
PROFILE = "holds only identifiers, integers, booleans and nulls"
AUDIT = "exact successful audit"
GENERATION = "advance by exactly one"
UNGUARDED = f"unguarded INSERT on {TABLE}"


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
        "coverage": {"cutoff_at_us": BASE_US, "partitions_covered": 12},
        "source_observation": {"source_version_digest": SOURCE_VERSION_DIGEST},
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
    ("uppercase scope digest", {"scope_digest": "sha256:" + "A" * 64}, CHECK),
    ("short scope digest", {"scope_digest": "sha256:" + "a" * 63}, CHECK),
    ("unprefixed scope digest", {"scope_digest": "a" * 71}, CHECK),
    ("malformed coverage digest", {"coverage_digest": "sha256:not-hex"}, CHECK),
    (
        "missing source observation digest",
        {"source_observation_digest": None},
        _not_null("source_observation_digest"),
    ),
    ("coverage that is not JSON", {"coverage_json": "not json"}, "malformed JSON"),
    ("coverage that is an array", {"coverage_json": "[]"}, CHECK),
    ("coverage that is not minified", {"coverage_json": '{"partitions_covered": 12}'}, CHECK),
    (
        "coverage beyond its byte bound",
        {"coverage_json": '{"n":[' + ",".join(["1"] * 5000) + "]}"},
        CHECK,
    ),
    (
        "missing source observation",
        {"source_observation_json": None},
        _not_null("source_observation_json"),
    ),
    (
        "source observation carrying SQL",
        {"source_observation_json": '{"query":"SELECT * FROM invoices"}'},
        PROFILE,
    ),
    (
        "source observation carrying a URL",
        {"source_observation_json": '{"endpoint":"https://source.invalid/v1"}'},
        PROFILE,
    ),
    ("coverage carrying a fractional amount", {"coverage_json": '{"balance":1234.56}'}, PROFILE),
    (
        "coverage carrying an oversized integer",
        {"coverage_json": '{"n":99999999999999999999}'},
        PROFILE,
    ),
    ("coverage keyed by free text", {"coverage_json": '{"overdue balance":1}'}, PROFILE),
    # Which layer refuses an escaped NUL depends on SQLite: 3.45 and later decode it and
    # the evidence profile refuses; earlier versions cut the string short there, and the
    # no-escape CHECK refuses. An escape that decodes to an identifier meets the CHECK.
    (
        "coverage hiding text behind an escaped NUL",
        {"coverage_json": '{"q":"x\\u0000SELECT * FROM invoices"}'},
        f"{PROFILE}|{CHECK}",
    ),
    (
        "source observation key hiding text behind an escaped NUL",
        {"source_observation_json": '{"x\\u0000https://source.invalid/v1":1}'},
        f"{PROFILE}|{CHECK}",
    ),
    ("coverage spelled with an escape", {"coverage_json": '{"q":"\\u0041BC"}'}, CHECK),
    (
        "source observation keyed with an escape",
        {"source_observation_json": '{"s\\u0041":1}'},
        CHECK,
    ),
    ("zero verification instant", {"verified_at_us": 0}, CHECK),
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
def test_the_schema_refuses_malformed_identity_digests_evidence_and_instants(
    refusing: m2.Owned, overrides: dict[str, object], refusal: str
) -> None:
    # The baseline row is admitted, so each refusal is about its overrides alone.
    _admitted_then_undone(refusing)
    _refused(refusing, refusal, **overrides)


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
        coverage={"partitions": ["2026-09", "2026-10"], "cutoff_at_us": BASE_US, "gaps": []},
        source_observation={
            "watermark": {"sequence": 41, "digest": SOURCE_VERSION_DIGEST},
            "rows_observed": 0,
            "complete_listing": False,
            "previous": None,
        },
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


def test_stored_evidence_that_does_not_verify_is_refused_when_read(owned: m2.Owned) -> None:
    unordered = '{"b":1,"a":2}'
    with _fenced(owned) as fenced:
        mismatched = content_digest('{"partitions_covered":13}')
        _write_raw(fenced, at_us=BASE_US, coverage_digest=mismatched)
        _write_raw(
            fenced,
            at_us=BASE_US + 1,
            dataset_id="dataset-unordered",
            coverage_json=unordered,
            coverage_digest=content_digest(unordered),
        )
    for dataset_id in ("dataset-raw", "dataset-unordered"):
        with pytest.raises(dataset_state.DatasetStateInvalid, match="does not verify"):
            _history(owned.connection, dataset_id)
        with pytest.raises(dataset_state.DatasetStateInvalid, match="does not verify"):
            _current(owned.connection, dataset_id)


def test_stored_evidence_too_deep_to_decode_is_refused_not_raised(owned: m2.Owned) -> None:
    """The schema admits nesting this module never writes. Reading it back either
    decodes exactly or refuses in the module's own terms -- never `RecursionError`."""
    deep = '{"n":' + "[" * 999 + "]" * 999 + "}"
    with _fenced(owned) as fenced:
        _write_raw(fenced, at_us=BASE_US, coverage_json=deep, coverage_digest=content_digest(deep))
    try:
        record = _current(owned.connection, "dataset-raw")
    except dataset_state.DatasetStateInvalid:
        pass
    else:
        assert record.coverage_digest == content_digest(deep)


def test_the_evidence_profile_is_a_shape_not_a_judgement_of_content(owned: m2.Owned) -> None:
    """A single token or an integer fits the profile whatever it means. Pinned so the
    ceiling is reviewed rather than assumed: keeping source values out of evidence is
    the producer's obligation until each document has a fixed schema."""
    token_shaped = {"host_port": "db.internal:5432", "amount_cents": 123456}
    _observe(owned, _observation(source_observation=token_shaped), at_us=BASE_US)
    current = _current(owned.connection, "dataset-invoices")
    assert current.observation.source_observation == token_shaped


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

DEEP: dict[str, Any] = {"leaf": 1}
for _ in range(40):
    DEEP = {"level": DEEP}

INVALID_EVIDENCE: tuple[tuple[str, object], ...] = (
    ("SQL", {"query": "SELECT * FROM invoices"}),
    ("a URL", {"endpoint": "https://source.invalid/v1"}),
    ("a user:password@host string", {"connection": "user:secret@db.invalid"}),
    ("a fractional amount", {"balance": 1234.56}),
    ("a row of free text", {"row": {"customer": "Acme Pty Ltd", "amount": 10}}),
    ("an oversized integer", {"count": 2**63}),
    ("a free-text key", {"overdue balance": 1}),
    ("a non-string key", {1: "x"}),
    ("bytes", {"payload": b"raw"}),
    ("excessive nesting", DEEP),
    ("more than its byte bound", {"partitions": ["p"] * 3000}),
    ("an array in place of an object", ["partitions"]),
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


@pytest.mark.parametrize(
    "evidence",
    [pytest.param(evidence, id=name) for name, evidence in INVALID_EVIDENCE],
)
@pytest.mark.parametrize("document", ["coverage", "source_observation"])
def test_the_writer_refuses_evidence_that_is_more_than_names_counts_and_digests(
    refusing: m2.Owned, document: str, evidence: object
) -> None:
    with pytest.raises(dataset_state.DatasetStateInvalid, match="evidence"):
        _observe(refusing, _observation(**{document: evidence}), at_us=BASE_US)
    assert m2.count(refusing.connection, TABLE) == 0
