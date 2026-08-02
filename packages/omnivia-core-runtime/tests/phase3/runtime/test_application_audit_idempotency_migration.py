"""V06-1 M1 acceptance: the audit and idempotency durable migration (M1-01 … M1-38).

`0007_application_audit_and_idempotency.sql` adds three append-only tables and
nothing else. This file is the durable proof of what that migration is, and just as
importantly of what it is not.

What it is. A unique consecutive successor to the accepted `0006`, pinned by content
checksum, whose objects are exactly three tables, six named indexes and nine
statement triggers; a slice that applies to a pristine and to an exactly-adopted
Phase 0 workspace without disturbing one legacy row, column or value; and a schema
whose immutability is enforced rather than asserted -- every UPDATE and DELETE aborts
unconditionally, for the current fenced owner as much as for anyone else, and every
INSERT must satisfy the complete connection-authority, guard, workspace-state and
lease predicate plus the singleton workspace binding.

What it is not. There is no repository, handler, dispatcher, operation, pagination or
token state, ACL/policy schema, or legacy promotion here, and the tests below hold
the migration to that: it contains no DML at all, so it cannot bump a workspace
format, promote a legacy `approved` claim, or rewrite anything that already exists.
Equivalence is likewise not redefined -- the application's public
`idempotency_equivalence()` remains the only authority for what makes two requests
the same request, and M1-24 uses that function rather than a second opinion.

**Residual obligation, carried forward deliberately.** The atomicity proved here is
atomicity across the three M1 tables (M1-34): audit, claim and outcome commit
together or not at all. It is *not* a demonstration that a real domain mutation and
its idempotency record commit atomically, because this slice adds no domain mutation
to commit. That obligation belongs to the first consuming vertical and is not
discharged by anything in this file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from omnivia_core_runtime.ownership.fencing import (
    GUARDED_TABLES,
    SchemaDrift,
    StaleGeneration,
    assert_guards_intact,
    close_guard,
    fenced_transaction,
    open_guard,
    trigger_names,
    verify_fingerprint,
)
from omnivia_core_runtime.ownership.identity import (
    FakeClock,
    ProcessEvidence,
    ServiceInstanceIdentity,
)
from omnivia_core_runtime.ownership.lease import acquire_lease
from omnivia_core_runtime.storage import migrations as migrations_module
from omnivia_core_runtime.storage.backup import (
    InstallationLayout,
    create_verified_backup,
    new_attempt_id,
)
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    SchemaCreationRefused,
    StorageError,
    fingerprint_schema,
    foreign_key_check,
    integrity_check,
    open_database,
    split_sql_statements,
    table_names,
)
from omnivia_core_runtime.storage.inventory import (
    capture_inventory,
    compare_inventories,
)
from omnivia_core_runtime.storage.legacy import LegacyMigrationError, rollback_migration
from omnivia_core_runtime.storage.migrations import (
    BASELINE_ADOPTED,
    BASELINE_PRISTINE,
    GENERATION_ONE,
    PHASE0_BASELINE_FILE,
    Migration,
    applied_migrations,
    apply_pending_migrations,
    bootstrap_generation_one,
    canonical_schema_fingerprint,
    canonical_schema_tables,
    load_migrations,
    materialise_phase0_baseline,
    phase0_baseline_sql,
    read_workspace_state,
)

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    IDEMPOTENCY_CONFLICT,
    IDEMPOTENCY_DISTINCT,
    IDEMPOTENCY_EQUIVALENCE_EXCLUDED_INPUTS,
    IDEMPOTENCY_EQUIVALENCE_INCLUDED_INPUTS,
    IDEMPOTENCY_REPLAY,
    CapabilityRequirement,
    ClientIdentity,
    RequestMetadata,
    classify_idempotent_replay,
    idempotency_equivalence,
)

# --- the migration under test -------------------------------------------------

MIGRATION_VERSION = 7
MIGRATION_NAME = "0007_application_audit_and_idempotency.sql"
PREDECESSOR_NAME = "0006_protect_ownership_substrate.sql"

#: The accepted base's migration corpus, pinned by content. Every existing migration
#: is immutable once applied, so an edit to any of them is a defect this file must
#: report rather than something a later reader has to notice in a diff.
ACCEPTED_MIGRATION_CHECKSUMS = {
    "0000_phase0_baseline.sql": (
        "28b2e553cf461aeae6ec93ce9fd2170c59a7efd5d1b0b956ced5cdaec0090de1"
    ),
    "0001_ownership_substrate.sql": (
        "2ca7c190c8678994a2229a8ee9b2d1c12b259406de6dada108202cf51a8aa87c"
    ),
    "0002_mutation_guard.sql": (
        "10807d2ff70ba1e1c4c9b407b9cf1efd7fdb11767726ae10bcb0831de40b1d06"
    ),
    "0003_complete_mutation_guard.sql": (
        "bf1a837c16d3e012db0c4fa78071d7198c116f27b75dfd0308fce3f4ec5d3f0f"
    ),
    "0004_bind_guard_to_lease.sql": (
        "67048d8b1d9cf5dd565f7dff0c2bbd07529a3c18f3b8e05ae4add7d1521c1cde"
    ),
    "0005_require_connection_authority.sql": (
        "e6cc0d7f6d0ce59c60a35eeaed77780b47af93175bbcb3eea933cc98c7832e89"
    ),
    PREDECESSOR_NAME: (
        "c3c1501b0cf22ccaa3af16d1ea2c77e7c62dd3b94d4e26cf5cd0041caf1a471e"
    ),
}

M1_TABLES = (
    "omnivia_application_audit_events",
    "omnivia_idempotency_claims",
    "omnivia_idempotency_outcomes",
)

M1_INDEXES = (
    "omnivia_idx_application_audit_events_ref_workspace",
    "omnivia_idx_application_audit_events_workspace_time",
    "omnivia_idx_idempotency_claims_id_workspace",
    "omnivia_idx_idempotency_claims_scope",
    "omnivia_idx_idempotency_outcomes_claim",
    "omnivia_idx_idempotency_outcomes_id_workspace",
)

M1_TRIGGERS = tuple(
    f"omnivia_guard_{table.removeprefix('omnivia_')}_{statement}"
    for table in M1_TABLES
    for statement in ("insert", "update", "delete")
)

WORKSPACE_ID = "ws-m1-audit-0001"
OTHER_WORKSPACE_ID = "ws-m1-audit-0002"
SERVICE_INSTANCE = "svc-m1-one"
PRINCIPAL = "principal-m1"
OPERATION = "candidate.approve"
PURPOSE = "operations.read"
IDEMPOTENCY_KEY = "key-m1-0001"

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64

#: The largest signed 64-bit microsecond value, carried without narrowing.
MAX_SIGNED_64 = 9_223_372_036_854_775_807

#: Every way a write without current authority is refused, across all the layers that
#: can refuse one. Which layer answers depends on where the writer is standing, and no
#: single message is the right expectation for all of them: the connection authorizer
#: says `not authorized` and is reached first on a governed connection whose guard is
#: closed; the persisted triggers say `unguarded` or `append-only` and are what a
#: writer that never went through the authorizer meets; a stock SQLite process finds
#: `no such function` because the connection-authority function is connection-local;
#: and a read-only open is refused `from outside the runtime`.
REFUSED_EXTERNAL_WRITE = (
    "unguarded|no such function|from outside the runtime|append-only|not authorized"
)


def migration_under_test() -> Migration:
    found = [m for m in load_migrations() if m.version == MIGRATION_VERSION]
    assert len(found) == 1, [m.name for m in load_migrations()]
    return found[0]


MIGRATION = migration_under_test()
MIGRATION_STATEMENTS = tuple(split_sql_statements(MIGRATION.sql))


def ddl_only_statements() -> tuple[str, ...]:
    """The migration's table and index statements, without its triggers.

    Replaying only these gives a database where the composite foreign keys are the
    sole defence, which is how M1-20 proves they refuse a cross-workspace link on
    their own rather than being carried by the trigger layer in front of them.
    """
    return tuple(
        statement
        for statement in MIGRATION_STATEMENTS
        if not statement.lstrip().upper().startswith("CREATE TRIGGER")
    )


# --- fixtures and helpers -----------------------------------------------------


def make_identity(
    instance: str = SERVICE_INSTANCE, pid: int = 4242
) -> ServiceInstanceIdentity:
    return ServiceInstanceIdentity(
        service_instance_id=instance,
        installation_id="inst-m1",
        process=ProcessEvidence(
            pid=pid, start_time="100", boot_id="boot-m1", os_principal="me"
        ),
    )


def bootstrap_and_migrate(
    path: Path,
    *,
    workspace_id: str = WORKSPACE_ID,
    adopt_phase0: bool = True,
    service_instance_id: str = SERVICE_INSTANCE,
) -> None:
    """Take a database to the full canonical schema through the real migrator."""
    maintenance = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        state = bootstrap_generation_one(
            maintenance,
            workspace_id=workspace_id,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            expect_phase0_baseline=adopt_phase0,
            service_instance_id=service_instance_id,
        )
        apply_pending_migrations(
            maintenance,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            service_instance_id=service_instance_id,
            fencing_generation=state.fencing_generation,
            workspace_id=workspace_id,
        )
    finally:
        maintenance.close()


@dataclass(frozen=True)
class Owned:
    """A migrated workspace this service instance holds the lease and guard for."""

    connection: sqlite3.Connection
    identity: ServiceInstanceIdentity
    generation: int
    path: Path


def take_ownership(path: Path, *, workspace_id: str = WORKSPACE_ID) -> Owned:
    identity = make_identity()
    connection = open_database(path, OpenMode.SERVICE_OWNED)
    lease = acquire_lease(
        connection,
        identity,
        clock=FakeClock(),
        workspace_id=workspace_id,
        holds_storage_lock=True,
        lock_mechanism="flock",
    )
    open_guard(
        connection,
        identity,
        clock=FakeClock(),
        workspace_id=workspace_id,
        fencing_generation=lease.fencing_generation,
    )
    return Owned(
        connection=connection,
        identity=identity,
        generation=lease.fencing_generation,
        path=path,
    )


@pytest.fixture
def migrated(tmp_path: Path) -> Path:
    """A workspace adopted from the frozen Phase 0 artifact and fully migrated."""
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    bootstrap_and_migrate(path)
    return path


@pytest.fixture
def owned(migrated: Path) -> Iterator[Owned]:
    """A migrated workspace under current, valid write authority."""
    holder = take_ownership(migrated)
    yield holder
    holder.connection.close()


AUDIT_DEFAULTS: dict[str, object] = {
    "audit_ref": "aud-0001",
    "workspace_id": WORKSPACE_ID,
    "principal_id": PRINCIPAL,
    "operation": OPERATION,
    "purpose": PURPOSE,
    "request_id": "req-0001",
    "correlation_id": "cor-0001",
    "trace_id": "trc-0001",
    "granted_authority_json": '{"scopes":["memory.read"]}',
    "outcome_class": "succeeded",
    "error_code": None,
    "recorded_at_us": 1_700_000_000_000_000,
    "claim_id": None,
    "outcome_id": None,
}

CLAIM_DEFAULTS: dict[str, object] = {
    "claim_id": "clm-0001",
    "workspace_id": WORKSPACE_ID,
    "principal_id": PRINCIPAL,
    "operation": OPERATION,
    "idempotency_key": IDEMPOTENCY_KEY,
    "request_digest": DIGEST_A,
    "audit_ref": "aud-0001",
    "claimed_at_us": 1_700_000_000_000_001,
}

OUTCOME_DEFAULTS: dict[str, object] = {
    "outcome_id": "out-0001",
    "claim_id": "clm-0001",
    "workspace_id": WORKSPACE_ID,
    "outcome_branch": "success",
    "error_code": None,
    "outcome_json": '{"status":"ok"}',
    "outcome_reference": None,
    "outcome_digest": DIGEST_B,
    "audit_ref": "aud-0001",
    "settled_at_us": 1_700_000_000_000_002,
}

DEFAULTS_BY_TABLE = {
    "omnivia_application_audit_events": AUDIT_DEFAULTS,
    "omnivia_idempotency_claims": CLAIM_DEFAULTS,
    "omnivia_idempotency_outcomes": OUTCOME_DEFAULTS,
}


def row_for(table: str, **overrides: object) -> dict[str, object]:
    values = dict(DEFAULTS_BY_TABLE[table])
    values.update(overrides)
    return values


def insert(
    connection: sqlite3.Connection, table: str, values: dict[str, object]
) -> None:
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(values.values())
    )


def write(holder: Owned, table: str, **overrides: object) -> None:
    """Insert one row under current authority, in its own fenced transaction."""
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        insert(holder.connection, table, row_for(table, **overrides))


def seed_triad(holder: Owned) -> None:
    """The audit event, the claim it justifies and the outcome it settled on."""
    with fenced_transaction(
        holder.connection,
        holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
    ):
        insert(holder.connection, M1_TABLES[0], row_for(M1_TABLES[0]))
        insert(holder.connection, M1_TABLES[1], row_for(M1_TABLES[1]))
        insert(holder.connection, M1_TABLES[2], row_for(M1_TABLES[2]))


def count(connection: sqlite3.Connection, table: str) -> int:
    row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


def object_names(connection: sqlite3.Connection, kind: str) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = ? AND name NOT LIKE 'sqlite_%'",
            (kind,),
        ).fetchall()
    }


def legacy_table_names() -> frozenset[str]:
    """The fourteen frozen Phase 0 tables, derived from the artifact itself."""
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(phase0_baseline_sql())
        return frozenset(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        )
    finally:
        connection.close()


def populate_legacy_corpus(path: Path) -> None:
    """A small deterministic legacy corpus covering the value classes a copy loses.

    Deliberately independent of the Phase 2 preservation fixture: this file must be
    able to state on its own what "legacy values are unchanged" means, and importing
    another phase's conftest to say it would make the claim depend on a fixture this
    slice does not own.
    """
    awkward = (
        "",
        "plain ascii",
        "unicode ☃ \U0001f30d émoji",
        "quote ' double \" semi ; backslash \\",
        "x" * 4096,
    )
    now = "2026-08-01T00:00:00+00:00"
    connection = sqlite3.connect(str(path))
    try:
        with connection:
            connection.execute(
                "INSERT INTO workspaces (id, name, root_path, storage_path, "
                "description, index_status, settings_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, NULL, 'indexed', '{\"b\":2,\"a\":1}', ?, ?)",
                (WORKSPACE_ID, "M1 corpus", "/root", "/storage", now, now),
            )
            for index, content in enumerate(awkward):
                connection.execute(
                    "INSERT INTO memories (id, workspace_id, content, source_type, "
                    "source_reference, source_description, lifecycle_state, "
                    "memory_type, created_by, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'note', 'ref', ?, 'approved', 'general', "
                    "'me', ?, ?)",
                    (
                        f"mem-{index:03d}",
                        WORKSPACE_ID,
                        content,
                        None if index % 2 else "described",
                        now,
                        now,
                    ),
                )
            connection.execute(
                "INSERT INTO sources (id, workspace_id, file_path, extension, "
                "size_bytes, modified_time, file_type, content_hash, parse_status, "
                "error_message, created_at, updated_at) "
                "VALUES ('src-0', ?, '/doc.md', '.md', 1024, ?, 'markdown', "
                "'0000', 'parsed', NULL, ?, ?)",
                (WORKSPACE_ID, now, now, now),
            )
            for index, content in enumerate(awkward[:3]):
                connection.execute(
                    "INSERT INTO chunks (id, source_id, chunk_index, content, "
                    "start_offset, end_offset, content_hash) "
                    "VALUES (?, 'src-0', ?, ?, ?, ?, NULL)",
                    (f"chk-{index}", index, content, index * 100, index * 100 + 99),
                )
            connection.execute(
                "INSERT INTO graph_entities (id, name, entity_type, source_id, "
                "approval_status, created_at, updated_at) "
                "VALUES ('ent-0', ?, 'concept', 'src-0', 'approved', ?, ?)",
                (awkward[2], now, now),
            )
    finally:
        connection.close()


def legacy_inventory(path: Path) -> dict[str, tuple[tuple[str, ...], int, str]]:
    """Columns, row count and content digest for every legacy table."""
    legacy = legacy_table_names()
    connection = open_database(path, OpenMode.READ_ONLY)
    try:
        inventory = capture_inventory(connection)
    finally:
        connection.close()
    return {
        entry.name: (entry.columns, entry.row_count, entry.content_checksum)
        for entry in inventory.tables
        if entry.name in legacy
    }


def run_child(source: str, *args: str, drop_pythonpath: bool = False) -> dict[str, Any]:
    """Run a script in a real OS process and return its single structured line."""
    environment = dict(os.environ)
    if drop_pythonpath:
        # A child that must import no OmniVia code is given no way to find it.
        environment.pop("PYTHONPATH", None)
    else:
        repo = Path(__file__).resolve().parents[5]
        environment["PYTHONPATH"] = os.pathsep.join(
            str(repo / part)
            for part in (
                "src",
                "services/omnivia-memory/src",
                "packages/omnivia-core-runtime/src",
            )
        )
    completed = subprocess.run(
        [sys.executable, "-c", source, *args],
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
        check=False,
    )
    for line in reversed(completed.stdout.splitlines()):
        stripped = line.strip()
        if stripped.startswith("{"):
            decoded = json.loads(stripped)
            assert isinstance(decoded, dict)
            return decoded
    raise AssertionError(
        f"child emitted no result: {completed.stdout}{completed.stderr}"
    )


#: A stock SQLite client. It imports no OmniVia code and reports which OmniVia
#: modules it ended up with, so "imports none" is verified rather than assumed.
STOCK_SQLITE_CHILD = """
import json, sqlite3, sys

path, statement = sys.argv[1], sys.argv[2]
result = {"statement": statement}
try:
    connection = sqlite3.connect(path, timeout=0.5)
    try:
        connection.execute(statement)
        connection.commit()
        result["succeeded"] = True
    finally:
        connection.close()
except Exception as exc:
    result["succeeded"] = False
    result["error"] = str(exc)
result["omnivia_modules"] = sorted(n for n in sys.modules if n.startswith("omnivia"))
sys.stdout.write(json.dumps(result, sort_keys=True) + chr(10))
"""

#: A genuinely fresh process that resumes migration on a workspace an interrupted
#: run left behind.
RETRY_CHILD = """
import json, sys
from omnivia_core_runtime.storage.connection import (
    OpenMode, foreign_key_check, integrity_check, open_database,
)
from omnivia_core_runtime.storage.migrations import (
    applied_migrations, apply_pending_migrations, read_workspace_state,
)

path, workspace_id, service = sys.argv[1], sys.argv[2], sys.argv[3]
result = {}
connection = open_database(__import__("pathlib").Path(path), OpenMode.EXCLUSIVE_MAINTENANCE)
try:
    state = read_workspace_state(connection)
    applied = apply_pending_migrations(
        connection,
        mode=OpenMode.EXCLUSIVE_MAINTENANCE,
        service_instance_id=service,
        fencing_generation=state.fencing_generation,
        workspace_id=workspace_id,
    )
    result["applied"] = [migration.version for migration in applied]
    result["ledger"] = sorted(applied_migrations(connection))
    result["ledger_rows_for_7"] = connection.execute(
        "SELECT COUNT(*) FROM omnivia_schema_migrations WHERE version = 7"
    ).fetchone()[0]
    result["tables"] = sorted(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    )
    result["objects"] = sorted(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )
    )
    result["integrity"] = integrity_check(connection)
    result["foreign_keys"] = foreign_key_check(connection)
    result["ok"] = True
except Exception as exc:
    result["ok"] = False
    result["error"] = str(exc)
finally:
    connection.close()
sys.stdout.write(json.dumps(result, sort_keys=True) + chr(10))
"""


class MigrationInterrupted(RuntimeError):
    """Injected failure, distinguishable from any refusal the runtime itself raises."""


class FailAfterStatement:
    """Forwarding proxy that raises once the Nth statement of `0007` has executed.

    A proxy rather than a monkeypatch because `sqlite3.Connection.execute` is a
    read-only attribute, and everything except `execute` is delegated so the code
    under test runs against genuine SQLite semantics -- which is the whole point when
    the property being proved is transactional rollback.

    The injection is matched on statement *text* rather than on a call count. A count
    would be pinned to however many statements the migrator happens to run around the
    migration, so it would drift the moment that changed; matching text keeps
    "after the Nth statement of 0007" meaning exactly that.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        statements: tuple[str, ...],
        stop_after: int,
    ) -> None:
        self._connection = connection
        self._targets = {" ".join(statement.split()) for statement in statements}
        self._stop_after = stop_after
        self.executed = 0

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        cursor = self._connection.execute(sql, *args)
        if " ".join(sql.split()) in self._targets:
            self.executed += 1
            if self.executed == self._stop_after:
                raise MigrationInterrupted(
                    f"interrupted after 0007 statement {self._stop_after}"
                )
        return cursor

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


# --- M1-01 … M1-08: migration identity, ledger and fingerprint ----------------


def test_m1_01_0007_is_the_unique_consecutive_successor_to_0006() -> None:
    """M1-01: one file, one version, immediately after the accepted predecessor."""
    ordered = load_migrations()
    versions = [migration.version for migration in ordered]
    assert versions == list(range(1, MIGRATION_VERSION + 1)), versions

    sevens = [m for m in ordered if m.version == MIGRATION_VERSION]
    assert len(sevens) == 1, [m.name for m in sevens]
    assert sevens[0].name == MIGRATION_NAME
    assert ordered[-1] is sevens[0]
    assert ordered[-2].name == PREDECESSOR_NAME
    assert ordered[-2].version == MIGRATION_VERSION - 1


def test_m1_02_content_checksum_and_ledger_metadata_agree(migrated: Path) -> None:
    """M1-02: the ledger records *this* 0007, under the authority that applied it."""
    expected = hashlib.sha256(MIGRATION.sql.encode("utf-8")).hexdigest()
    assert MIGRATION.checksum == expected
    assert len(expected) == 64

    connection = open_database(migrated, OpenMode.READ_ONLY)
    try:
        row = connection.execute(
            "SELECT name, checksum, applied_by_service_instance, fencing_generation, "
            "applied_at FROM omnivia_schema_migrations WHERE version = ?",
            (MIGRATION_VERSION,),
        ).fetchone()
        assert row is not None
        assert row[0] == MIGRATION_NAME
        assert row[1] == expected
        assert row[2] == SERVICE_INSTANCE
        assert int(row[3]) == GENERATION_ONE
        assert row[4]

        outcomes = {
            str(item[0])
            for item in connection.execute(
                "SELECT outcome FROM omnivia_migration_attempts WHERE version = ?",
                (MIGRATION_VERSION,),
            )
        }
        assert outcomes == {"succeeded"}, outcomes
    finally:
        connection.close()


def test_m1_03_unchanged_reapply_is_a_no_op(migrated: Path) -> None:
    """M1-03: applying an already-applied 0007 does nothing at all."""
    connection = open_database(migrated, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        before_ledger = applied_migrations(connection)
        before_fingerprint = fingerprint_schema(connection)
        before_attempts = count(connection, "omnivia_migration_attempts")

        applied = apply_pending_migrations(
            connection,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            service_instance_id=SERVICE_INSTANCE,
            fencing_generation=GENERATION_ONE,
            workspace_id=WORKSPACE_ID,
        )

        assert applied == []
        assert applied_migrations(connection) == before_ledger
        assert fingerprint_schema(connection) == before_fingerprint
        assert count(connection, "omnivia_migration_attempts") == before_attempts
    finally:
        connection.close()


def test_m1_04_a_mismatched_recorded_checksum_is_refused(migrated: Path) -> None:
    """M1-04: "version 7 is applied" has to mean *this* version 7."""
    connection = open_database(migrated, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        connection.execute(
            "UPDATE omnivia_schema_migrations SET checksum = ? WHERE version = ?",
            ("0" * 64, MIGRATION_VERSION),
        )
        connection.commit()
        with pytest.raises(StorageError, match="has changed since it was applied"):
            apply_pending_migrations(
                connection,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=SERVICE_INSTANCE,
                fencing_generation=GENERATION_ONE,
                workspace_id=WORKSPACE_ID,
            )
    finally:
        connection.close()


def test_m1_05_canonical_schema_carries_every_m1_object(migrated: Path) -> None:
    """M1-05: three tables, six named indexes and nine triggers, and exactly those.

    The deltas are computed by replaying the artifacts with and without `0007`
    rather than by hard-coding totals, so this stays a statement about what the
    migration adds instead of about how large the schema happens to be.
    """
    assert set(M1_TABLES) <= canonical_schema_tables()

    without = sqlite3.connect(":memory:")
    with_seven = sqlite3.connect(":memory:")
    try:
        for connection in (without, with_seven):
            connection.executescript(phase0_baseline_sql())
        for migration in load_migrations():
            if migration.version != MIGRATION_VERSION:
                without.executescript(migration.sql)
            with_seven.executescript(migration.sql)

        before = fingerprint_schema(without)
        after = fingerprint_schema(with_seven)
        assert after.tables - before.tables == len(M1_TABLES)
        assert after.indexes - before.indexes == len(M1_INDEXES)
        assert after.triggers - before.triggers == len(M1_TRIGGERS)
        assert after.digest != before.digest
    finally:
        without.close()
        with_seven.close()

    canonical = canonical_schema_fingerprint()
    live = open_database(migrated, OpenMode.READ_ONLY)
    try:
        assert fingerprint_schema(live).matches(canonical)
        assert set(M1_TABLES) <= object_names(live, "table")
        assert set(M1_INDEXES) <= object_names(live, "index")
        assert set(M1_TRIGGERS) <= object_names(live, "trigger")
        # No view and no virtual table: the slice adds storage, not projections.
        assert object_names(live, "view") == set()
    finally:
        live.close()


@pytest.mark.parametrize(
    ("kind", "name"),
    [("table", M1_TABLES[0]), ("index", M1_INDEXES[3]), ("trigger", M1_TRIGGERS[0])],
)
def test_m1_06_live_drift_in_any_m1_object_fails(
    migrated: Path, kind: str, name: str
) -> None:
    """M1-06: dropping any one of them is detected before writable readiness."""
    connection = open_database(migrated, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        expected = canonical_schema_fingerprint()
        assert verify_fingerprint(connection, expected).matches(expected)
        assert_guards_intact(connection)

        connection.execute(f"DROP {kind.upper()} {name}")
        connection.commit()

        with pytest.raises(SchemaDrift, match="fingerprint differs"):
            verify_fingerprint(connection, expected)
        if kind == "trigger":
            with pytest.raises(SchemaDrift, match="guard triggers are missing"):
                assert_guards_intact(connection)
    finally:
        connection.close()


def test_m1_07_user_version_mirrors_seven_but_is_not_authority(migrated: Path) -> None:
    """M1-07: the ledger decides; the pragma is a diagnostic mirror of it."""
    connection = open_database(migrated, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        ledger = applied_migrations(connection)
        assert max(ledger) == MIGRATION_VERSION
        version = connection.execute("PRAGMA user_version").fetchone()
        assert version is not None and int(version[0]) == MIGRATION_VERSION

        connection.execute("PRAGMA user_version = 999")
        assert applied_migrations(connection) == ledger
        connection.execute("PRAGMA user_version = 0")
        assert applied_migrations(connection) == ledger
        assert max(applied_migrations(connection)) == MIGRATION_VERSION
    finally:
        connection.close()


def test_m1_08_every_existing_migration_is_byte_for_byte_unchanged() -> None:
    """M1-08: 0007 is additive; nothing before it moved."""
    package = migrations_module.resources.files(migrations_module.MIGRATION_PACKAGE)
    for name, expected in ACCEPTED_MIGRATION_CHECKSUMS.items():
        text = package.joinpath(name).read_text(encoding="utf-8")
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == expected, name

    on_disk = {
        entry.name
        for entry in package.iterdir()
        if entry.name.endswith(".sql")  # type: ignore[attr-defined]
    }
    assert on_disk == set(ACCEPTED_MIGRATION_CHECKSUMS) | {MIGRATION_NAME}
    assert PHASE0_BASELINE_FILE in ACCEPTED_MIGRATION_CHECKSUMS


# --- M1-09 … M1-14: bootstrap, adoption and legacy preservation ---------------


def test_m1_09_pristine_bootstrap_reaches_0007(tmp_path: Path) -> None:
    """M1-09: a workspace that never had a legacy database migrates cleanly."""
    path = tmp_path / "pristine.sqlite"
    connection = open_database(path, OpenMode.EPHEMERAL)
    try:
        state = bootstrap_generation_one(
            connection,
            workspace_id=WORKSPACE_ID,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            expect_phase0_baseline=False,
            service_instance_id=SERVICE_INSTANCE,
        )
        assert state.baseline_state == BASELINE_PRISTINE
        applied = apply_pending_migrations(
            connection,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            service_instance_id=SERVICE_INSTANCE,
            fencing_generation=state.fencing_generation,
            workspace_id=WORKSPACE_ID,
        )
        assert MIGRATION_VERSION in [migration.version for migration in applied]
        assert set(M1_TABLES) <= object_names(connection, "table")
        assert set(M1_INDEXES) <= object_names(connection, "index")
        assert set(M1_TRIGGERS) <= object_names(connection, "trigger")
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
    finally:
        connection.close()


def test_m1_10_exact_phase0_adoption_reaches_0007(tmp_path: Path) -> None:
    """M1-10: adoption is recorded as adoption, and 0007 lands on top of it."""
    path = tmp_path / "adopted.sqlite"
    materialise_phase0_baseline(path)
    populate_legacy_corpus(path)
    bootstrap_and_migrate(path)

    connection = open_database(path, OpenMode.READ_ONLY)
    try:
        state = read_workspace_state(connection)
        assert state is not None
        assert state.baseline_state == BASELINE_ADOPTED
        assert state.workspace_id == WORKSPACE_ID
        assert MIGRATION_VERSION in applied_migrations(connection)
        assert set(M1_TABLES) <= object_names(connection, "table")
    finally:
        connection.close()


def test_m1_11_the_full_legacy_inventory_is_unchanged_across_0007(
    tmp_path: Path,
) -> None:
    """M1-11: every legacy table, column, row count and value survives verbatim.

    Measured either side of `0007` alone rather than either side of the whole
    migration series, so this is evidence about *this* migration.
    """
    path = tmp_path / "adopted.sqlite"
    materialise_phase0_baseline(path)
    populate_legacy_corpus(path)

    before_all = _apply_through_predecessor(path)
    before = legacy_inventory(path)
    assert len(before) == 14, sorted(before)
    assert sum(entry[1] for entry in before.values()) > 8

    connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        applied = apply_pending_migrations(
            connection,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            service_instance_id=SERVICE_INSTANCE,
            fencing_generation=before_all,
            workspace_id=WORKSPACE_ID,
        )
    finally:
        connection.close()
    assert [migration.version for migration in applied] == [MIGRATION_VERSION]

    after = legacy_inventory(path)
    assert after == before
    # And the empty M1 tables did not smuggle a row in alongside them.
    published = open_database(path, OpenMode.READ_ONLY)
    try:
        for table in M1_TABLES:
            assert count(published, table) == 0
    finally:
        published.close()


def _apply_through_predecessor(path: Path) -> int:
    """Bootstrap and migrate up to the accepted predecessor only.

    `apply_pending_migrations` applies everything outstanding, so reaching the state
    that existed *before* this slice means narrowing what `load_migrations` offers.
    Narrowed at the module attribute the migrator actually reads, so both halves of
    the test still run through the real migrator rather than a hand-rolled stand-in.
    """
    original = migrations_module.load_migrations
    trimmed = tuple(m for m in original() if m.version < MIGRATION_VERSION)

    migrations_module.load_migrations = lambda: trimmed  # type: ignore[assignment]
    try:
        connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
        try:
            state = bootstrap_generation_one(
                connection,
                workspace_id=WORKSPACE_ID,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                expect_phase0_baseline=True,
                service_instance_id=SERVICE_INSTANCE,
            )
            apply_pending_migrations(
                connection,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=SERVICE_INSTANCE,
                fencing_generation=state.fencing_generation,
                workspace_id=WORKSPACE_ID,
            )
            assert max(applied_migrations(connection)) == MIGRATION_VERSION - 1
            return state.fencing_generation
        finally:
            connection.close()
    finally:
        migrations_module.load_migrations = original  # type: ignore[assignment]
        # The canonical oracles memoise what `load_migrations` returned; a value
        # computed while it was narrowed would describe a schema without 0007.
        canonical_schema_tables.cache_clear()
        canonical_schema_fingerprint.cache_clear()


def test_m1_12_populated_non_phase0_adoption_is_refused_with_no_m1_objects(
    tmp_path: Path,
) -> None:
    """M1-12: an unrecognised database is not adopted, and gains nothing."""
    path = tmp_path / "unknown.sqlite"
    materialise_phase0_baseline(path)
    stray = sqlite3.connect(str(path))
    try:
        stray.execute("CREATE TABLE interloper (id TEXT PRIMARY KEY)")
        stray.commit()
    finally:
        stray.close()

    connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        with pytest.raises(StorageError, match="not the frozen"):
            bootstrap_generation_one(
                connection,
                workspace_id=WORKSPACE_ID,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                expect_phase0_baseline=True,
                service_instance_id=SERVICE_INSTANCE,
            )
        with pytest.raises(SchemaCreationRefused, match="requires an empty database"):
            bootstrap_generation_one(
                connection,
                workspace_id=WORKSPACE_ID,
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                expect_phase0_baseline=False,
                service_instance_id=SERVICE_INSTANCE,
            )
        present = object_names(connection, "table")
        assert present.isdisjoint(M1_TABLES)
        assert object_names(connection, "index").isdisjoint(M1_INDEXES)
        assert object_names(connection, "trigger").isdisjoint(M1_TRIGGERS)
    finally:
        connection.close()


def test_m1_13_0007_carries_no_dml_so_it_bumps_no_version(migrated: Path) -> None:
    """M1-13: the workspace format and minimum Core behaviour are untouched.

    Proved structurally rather than by observation. `0007` is pure DDL over
    `omnivia_`-prefixed objects: with no INSERT, UPDATE, DELETE or ALTER anywhere in
    it, it *cannot* bump a workspace format version, rewrite a legacy row, or promote
    a legacy `approved` claim, whatever a later reader assumes about it.
    """
    prefixes = ("CREATE TABLE", "CREATE UNIQUE INDEX", "CREATE INDEX", "CREATE TRIGGER")
    for statement in MIGRATION_STATEMENTS:
        normalised = " ".join(statement.split()).upper()
        assert normalised.startswith(prefixes), normalised[:80]
    assert len(MIGRATION_STATEMENTS) == len(M1_TABLES) + len(M1_INDEXES) + len(
        M1_TRIGGERS
    )

    forbidden = (
        r"\bINSERT\s+INTO\b",
        r"\bUPDATE\s+\w+\s+SET\b",
        r"\bDELETE\s+FROM\b",
        r"\bALTER\s+TABLE\b",
        r"\bDROP\s+(TABLE|INDEX|TRIGGER|VIEW)\b",
        r"\bCREATE\s+VIEW\b",
        r"\bCREATE\s+VIRTUAL\s+TABLE\b",
        r"\bPRAGMA\b",
    )
    for pattern in forbidden:
        assert re.search(pattern, MIGRATION.sql, re.IGNORECASE) is None, pattern

    # It never names a legacy table, so it cannot alter or promote legacy knowledge.
    for legacy in legacy_table_names():
        assert re.search(rf"\b{re.escape(legacy)}\b", MIGRATION.sql) is None, legacy

    # Every object it creates is reserved-prefixed.
    created = re.findall(
        r"CREATE (?:UNIQUE )?(?:TABLE|INDEX|TRIGGER) IF NOT EXISTS (\w+)", MIGRATION.sql
    )
    assert len(created) == len(MIGRATION_STATEMENTS)
    assert all(name.startswith("omnivia_") for name in created), created

    connection = open_database(migrated, OpenMode.READ_ONLY)
    try:
        state = read_workspace_state(connection)
        assert state is not None
        assert state.workspace_format_version == "1"
        assert state.fencing_generation == GENERATION_ONE
    finally:
        connection.close()


def test_m1_14_integrity_and_foreign_keys_are_clean_after_migration(
    owned: Owned,
) -> None:
    """M1-14: a migrated workspace is healthy, and stays so once M1 rows exist."""
    assert integrity_check(owned.connection) == []
    assert foreign_key_check(owned.connection) == []
    seed_triad(owned)
    assert integrity_check(owned.connection) == []
    assert foreign_key_check(owned.connection) == []


# --- M1-15 … M1-24: scope, uniqueness, negatives and equivalence --------------


@pytest.mark.parametrize(
    ("case", "overrides"),
    [
        ("principal", {"principal_id": "principal-other"}),
        ("operation", {"operation": "candidate.reject"}),
        ("key", {"idempotency_key": "key-m1-0002"}),
    ],
)
def test_m1_15_scope_uniqueness_distinguishes_each_scope_member(
    owned: Owned, case: str, overrides: dict[str, object]
) -> None:
    """M1-15: principal, operation and key each make a different scope."""
    seed_triad(owned)
    write(owned, M1_TABLES[1], claim_id=f"clm-{case}", **overrides)
    assert count(owned.connection, M1_TABLES[1]) == 2


def test_m1_15b_the_workspace_member_of_the_scope_is_load_bearing(
    tmp_path: Path, owned: Owned
) -> None:
    """M1-15: and so does the workspace, which is why the tuple names it.

    A second workspace takes the identical (principal, operation, key) and claims it
    without conflict, while the same tuple repeated inside one workspace is refused.
    """
    seed_triad(owned)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        write(owned, M1_TABLES[1], claim_id="clm-duplicate-scope")

    other = tmp_path / "other-workspace.sqlite"
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
                M1_TABLES[0],
                row_for(M1_TABLES[0], workspace_id=OTHER_WORKSPACE_ID),
            )
            insert(
                holder.connection,
                M1_TABLES[1],
                row_for(M1_TABLES[1], workspace_id=OTHER_WORKSPACE_ID),
            )
        assert count(holder.connection, M1_TABLES[1]) == 1
    finally:
        holder.connection.close()


def test_m1_16_same_scope_and_digest_resolves_to_the_one_claim(owned: Owned) -> None:
    """M1-16: an honest replay finds the existing pair; it does not add a second."""
    seed_triad(owned)
    equivalence = _equivalence()
    first = (equivalence.scope, equivalence.fingerprint)

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        write(owned, M1_TABLES[1], claim_id="clm-replay")

    assert count(owned.connection, M1_TABLES[1]) == 1
    assert count(owned.connection, M1_TABLES[2]) == 1
    row = owned.connection.execute(
        "SELECT c.claim_id, c.request_digest, o.outcome_id FROM "
        "omnivia_idempotency_claims c JOIN omnivia_idempotency_outcomes o "
        "ON o.claim_id = c.claim_id"
    ).fetchone()
    assert row == ("clm-0001", DIGEST_A, "out-0001")

    # The replay decision itself belongs to the accepted contract function.
    repeat = _equivalence()
    assert (repeat.scope, repeat.fingerprint) == first
    assert classify_idempotent_replay(equivalence, repeat) == IDEMPOTENCY_REPLAY


def test_m1_17_same_scope_and_different_digest_conflicts_without_overwriting(
    owned: Owned,
) -> None:
    """M1-17: one key, two requests -- refused, with the original bytes intact."""
    seed_triad(owned)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        write(owned, M1_TABLES[1], claim_id="clm-conflict", request_digest=DIGEST_B)

    assert count(owned.connection, M1_TABLES[1]) == 1
    stored = owned.connection.execute(
        "SELECT request_digest FROM omnivia_idempotency_claims"
    ).fetchone()
    assert stored == (DIGEST_A,)

    first = _equivalence()
    second = _equivalence(operation_input={"changed": True})
    assert classify_idempotent_replay(first, second) == IDEMPOTENCY_CONFLICT


def test_m1_18_one_claim_cannot_have_two_terminal_outcomes(owned: Owned) -> None:
    """M1-18: terminal means terminal."""
    seed_triad(owned)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        write(owned, M1_TABLES[2], outcome_id="out-second")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        write(
            owned,
            M1_TABLES[2],
            outcome_id="out-third",
            outcome_branch="error",
            error_code="conflict",
            outcome_json=None,
            outcome_reference="ref://outcome",
        )
    assert count(owned.connection, M1_TABLES[2]) == 1


#: A second claim beside the seeded triad: distinct in primary key and in scope, and
#: with no outcome of its own yet. An orphan-*outcome* case needs it, because the
#: outcome table allows one outcome per claim, so an attempt that reused `clm-0001`
#: would be refused by that index before its foreign keys were ever consulted.
SPARE_CLAIM: dict[str, object] = {
    "claim_id": "clm-spare",
    "idempotency_key": "key-spare",
}


@pytest.mark.parametrize(
    ("case", "table", "orphan", "present_parent"),
    [
        (
            "claim_without_audit",
            M1_TABLES[1],
            {
                "claim_id": "clm-orphan",
                "idempotency_key": "key-orphan",
                "audit_ref": "aud-missing",
            },
            {"audit_ref": "aud-0001"},
        ),
        (
            "outcome_without_claim",
            M1_TABLES[2],
            {"outcome_id": "out-orphan", "claim_id": "clm-missing"},
            {"claim_id": "clm-spare"},
        ),
        (
            "outcome_without_audit",
            M1_TABLES[2],
            {
                "outcome_id": "out-orphan",
                "claim_id": "clm-spare",
                "audit_ref": "aud-missing",
            },
            {"audit_ref": "aud-0001"},
        ),
        (
            "audit_to_missing_claim",
            M1_TABLES[0],
            {"audit_ref": "aud-orphan", "claim_id": "clm-missing"},
            {"claim_id": "clm-spare"},
        ),
        (
            "audit_to_missing_outcome",
            M1_TABLES[0],
            {"audit_ref": "aud-orphan", "outcome_id": "out-missing"},
            {"outcome_id": "out-0001"},
        ),
    ],
)
def test_m1_19_orphan_links_are_refused(
    owned: Owned,
    case: str,
    table: str,
    orphan: dict[str, object],
    present_parent: dict[str, object],
) -> None:
    """M1-19: no row may name a parent that does not exist.

    Every attempted row is distinct from the rows already there -- distinct primary
    key, and for a claim a distinct idempotency scope -- so the foreign key under test
    is the only constraint left that can refuse it. An attempt that collided with an
    existing row would be refused by UNIQUE first, which says nothing about links.

    Each case then inserts the *same* row a second time with only the missing
    reference replaced by one that exists, and that one is accepted. That is what
    attributes the refusal to the missing parent rather than to something incidental:
    SQLite's message names no column, so a bare "FOREIGN KEY constraint failed" is on
    its own compatible with the wrong link having failed.
    """
    seed_triad(owned)
    write(owned, M1_TABLES[1], **SPARE_CLAIM)
    before = {name: count(owned.connection, name) for name in M1_TABLES}

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        write(owned, table, **orphan)
    assert {name: count(owned.connection, name) for name in M1_TABLES} == before, case

    write(owned, table, **{**orphan, **present_parent})
    assert count(owned.connection, table) == before[table] + 1, case


def test_m1_19b_a_dangling_link_is_refused_without_wedging_the_connection(
    owned: Owned,
) -> None:
    """M1-19: where a dangling link is caught decides whether a refusal is survivable.

    The links read as mutually recursive, but there is no cycle to break: the audit
    event's two links are optional, so the first event carries neither, and the triad
    commits in one transaction under immediate enforcement (M1-27, and M1-27b for the
    later event that does carry both). Enforcement is therefore free to stay at the
    statement, and the first half below is what that buys -- the offending statement
    fails, `fenced_transaction` rolls the transaction back on its way out, and the
    connection is ready for the next write.

    The second half is the cost of the alternative, made permanent rather than left as
    a claim in a comment. `PRAGMA defer_foreign_keys` moves exactly these checks to
    COMMIT without altering the schema, which is what `DEFERRABLE INITIALLY DEFERRED`
    would do durably. SQLite then leaves the transaction *open* when COMMIT fails,
    and `fenced_transaction` issues its COMMIT outside the block that rolls back --
    so the service's single authoritative write connection is left inside an open
    transaction, refusing every later write, until something rolls it back by hand.
    Nothing in the product does. That is why none of these foreign keys is deferred,
    and why M1-20b asserts that none of them ever becomes so.
    """
    seed_triad(owned)

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        write(
            owned,
            M1_TABLES[1],
            claim_id="clm-dangling",
            idempotency_key="key-dangling",
            audit_ref="aud-missing",
        )

    assert owned.connection.in_transaction is False
    assert count(owned.connection, M1_TABLES[1]) == 1

    write(owned, M1_TABLES[0], audit_ref="aud-after-refusal")
    assert count(owned.connection, M1_TABLES[0]) == 2

    # Deferred, on the same connection and through the same governed path: the
    # statement is accepted and COMMIT is what refuses.
    owned.connection.execute("PRAGMA defer_foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        write(
            owned,
            M1_TABLES[1],
            claim_id="clm-deferred",
            idempotency_key="key-deferred",
            audit_ref="aud-missing",
        )

    assert owned.connection.in_transaction is True
    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        write(owned, M1_TABLES[0], audit_ref="aud-never-reached")

    # Only an explicit rollback -- which no product path issues after a failed COMMIT
    # -- gets the connection back, and none of the refused rows is durable.
    owned.connection.execute("ROLLBACK")
    assert owned.connection.in_transaction is False
    write(owned, M1_TABLES[0], audit_ref="aud-after-rollback")
    assert count(owned.connection, M1_TABLES[0]) == 3
    assert count(owned.connection, M1_TABLES[1]) == 1
    assert count(owned.connection, M1_TABLES[2]) == 1


def test_m1_20_composite_foreign_keys_refuse_a_cross_workspace_link() -> None:
    """M1-20: the links cannot cross workspace scope, trigger layer or not.

    Replayed without the guard triggers on purpose. In a real workspace the INSERT
    trigger pins every row to the singleton workspace, so a cross-workspace row never
    reaches the foreign keys -- which would leave the claim that *they* forbid it
    untested. Here they are the only thing standing.
    """
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        for statement in ddl_only_statements():
            connection.execute(statement)

        insert(connection, M1_TABLES[0], row_for(M1_TABLES[0]))
        insert(connection, M1_TABLES[1], row_for(M1_TABLES[1]))

        # An outcome in another workspace cannot attach to this workspace's claim.
        with pytest.raises(
            sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"
        ):
            insert(
                connection,
                M1_TABLES[2],
                row_for(M1_TABLES[2], workspace_id=OTHER_WORKSPACE_ID),
            )
        # Nor can a claim reach an audit event recorded in another workspace.
        with pytest.raises(
            sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"
        ):
            insert(
                connection,
                M1_TABLES[1],
                row_for(
                    M1_TABLES[1],
                    claim_id="clm-cross",
                    workspace_id=OTHER_WORKSPACE_ID,
                    idempotency_key="key-cross",
                ),
            )
        # Nor can an audit event in another workspace link to this claim.
        with pytest.raises(
            sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"
        ):
            insert(
                connection,
                M1_TABLES[0],
                row_for(
                    M1_TABLES[0],
                    audit_ref="aud-cross",
                    workspace_id=OTHER_WORKSPACE_ID,
                    claim_id="clm-0001",
                ),
            )
        assert count(connection, M1_TABLES[2]) == 0
    finally:
        connection.close()


def test_m1_20b_every_declared_foreign_key_names_the_workspace_column(
    migrated: Path,
) -> None:
    """M1-20: and each one is composite, so scope cannot be dropped from a link."""
    connection = open_database(migrated, OpenMode.READ_ONLY)
    try:
        seen = 0
        for table in M1_TABLES:
            keys: dict[int, list[tuple[str, str]]] = {}
            for row in connection.execute(f"PRAGMA foreign_key_list({table})"):
                keys.setdefault(int(row[0]), []).append((str(row[3]), str(row[4])))
            for columns in keys.values():
                seen += 1
                assert "workspace_id" in [child for child, _ in columns], (
                    table,
                    columns,
                )
                assert len(columns) == 2, (table, columns)
        assert seen == 5, seen

        # And none of them is deferred: see M1-19b for what that would cost.
        for table in M1_TABLES:
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            assert row is not None
            assert "DEFERRABLE" not in str(row[0]).upper(), table
    finally:
        connection.close()


NEGATIVE_CASES: list[tuple[str, str, dict[str, object], str]] = [
    # Required values may be neither absent nor empty.
    ("audit_ref_null", M1_TABLES[0], {"audit_ref": None}, "NOT NULL|PRIMARY KEY"),
    ("audit_ref_empty", M1_TABLES[0], {"audit_ref": ""}, "CHECK constraint failed"),
    ("audit_workspace_empty", M1_TABLES[0], {"workspace_id": ""}, "unguarded INSERT"),
    ("audit_principal_null", M1_TABLES[0], {"principal_id": None}, "NOT NULL"),
    (
        "audit_principal_empty",
        M1_TABLES[0],
        {"principal_id": ""},
        "CHECK constraint failed",
    ),
    ("audit_operation_null", M1_TABLES[0], {"operation": None}, "NOT NULL"),
    (
        "audit_operation_empty",
        M1_TABLES[0],
        {"operation": ""},
        "CHECK constraint failed",
    ),
    ("audit_purpose_null", M1_TABLES[0], {"purpose": None}, "NOT NULL"),
    ("audit_purpose_empty", M1_TABLES[0], {"purpose": ""}, "CHECK constraint failed"),
    ("audit_request_id_null", M1_TABLES[0], {"request_id": None}, "NOT NULL"),
    (
        "audit_correlation_empty",
        M1_TABLES[0],
        {"correlation_id": ""},
        "CHECK constraint failed",
    ),
    ("audit_trace_null", M1_TABLES[0], {"trace_id": None}, "NOT NULL"),
    (
        "audit_authority_null",
        M1_TABLES[0],
        {"granted_authority_json": None},
        "NOT NULL",
    ),
    (
        "audit_authority_empty",
        M1_TABLES[0],
        {"granted_authority_json": ""},
        "CHECK constraint failed",
    ),
    # Bounds: no column is a place unbounded content goes.
    (
        "audit_ref_too_long",
        M1_TABLES[0],
        {"audit_ref": "a" * 129},
        "CHECK constraint failed",
    ),
    (
        "audit_operation_too_long",
        M1_TABLES[0],
        {"operation": "o" * 129},
        "CHECK constraint failed",
    ),
    (
        "audit_authority_too_long",
        M1_TABLES[0],
        {"granted_authority_json": "j" * 4097},
        "CHECK constraint failed",
    ),
    # Enums are closed, and the code column agrees with the branch it belongs to.
    (
        "audit_outcome_unknown",
        M1_TABLES[0],
        {"outcome_class": "maybe"},
        "CHECK constraint failed",
    ),
    (
        "audit_outcome_empty",
        M1_TABLES[0],
        {"outcome_class": ""},
        "CHECK constraint failed",
    ),
    (
        "audit_success_with_code",
        M1_TABLES[0],
        {"outcome_class": "succeeded", "error_code": "conflict"},
        "CHECK constraint failed",
    ),
    (
        "audit_failure_without_code",
        M1_TABLES[0],
        {"outcome_class": "failed", "error_code": None},
        "CHECK constraint failed",
    ),
    (
        "audit_refusal_without_code",
        M1_TABLES[0],
        {"outcome_class": "refused", "error_code": None},
        "CHECK constraint failed",
    ),
    (
        "audit_refusal_empty_code",
        M1_TABLES[0],
        {"outcome_class": "refused", "error_code": ""},
        "CHECK constraint failed",
    ),
    # Timestamps are exact microseconds, never rounded into shape.
    ("audit_time_null", M1_TABLES[0], {"recorded_at_us": None}, "NOT NULL"),
    (
        "audit_time_fractional",
        M1_TABLES[0],
        {"recorded_at_us": 1_700_000_000_000_000.5},
        "CHECK constraint failed",
    ),
    ("audit_time_zero", M1_TABLES[0], {"recorded_at_us": 0}, "CHECK constraint failed"),
    (
        "audit_time_negative",
        M1_TABLES[0],
        {"recorded_at_us": -1},
        "CHECK constraint failed",
    ),
    ("audit_claim_empty", M1_TABLES[0], {"claim_id": ""}, "CHECK constraint failed"),
    # Claims.
    ("claim_id_null", M1_TABLES[1], {"claim_id": None}, "NOT NULL|PRIMARY KEY"),
    ("claim_id_empty", M1_TABLES[1], {"claim_id": ""}, "CHECK constraint failed"),
    ("claim_principal_null", M1_TABLES[1], {"principal_id": None}, "NOT NULL"),
    (
        "claim_principal_empty",
        M1_TABLES[1],
        {"principal_id": ""},
        "CHECK constraint failed",
    ),
    (
        "claim_operation_empty",
        M1_TABLES[1],
        {"operation": ""},
        "CHECK constraint failed",
    ),
    ("claim_key_null", M1_TABLES[1], {"idempotency_key": None}, "NOT NULL"),
    (
        "claim_key_empty",
        M1_TABLES[1],
        {"idempotency_key": ""},
        "CHECK constraint failed",
    ),
    (
        "claim_key_too_long",
        M1_TABLES[1],
        {"idempotency_key": "k" * 129},
        "CHECK constraint failed",
    ),
    ("claim_audit_null", M1_TABLES[1], {"audit_ref": None}, "NOT NULL"),
    ("claim_digest_null", M1_TABLES[1], {"request_digest": None}, "NOT NULL"),
    (
        "claim_digest_empty",
        M1_TABLES[1],
        {"request_digest": ""},
        "CHECK constraint failed",
    ),
    (
        "claim_digest_bare_hex",
        M1_TABLES[1],
        {"request_digest": "a" * 64},
        "CHECK constraint failed",
    ),
    (
        "claim_digest_wrong_algorithm",
        M1_TABLES[1],
        {"request_digest": "sha512:" + "a" * 64},
        "CHECK constraint failed",
    ),
    (
        "claim_digest_uppercase",
        M1_TABLES[1],
        {"request_digest": "sha256:" + "A" * 64},
        "CHECK constraint failed",
    ),
    (
        "claim_digest_non_hex",
        M1_TABLES[1],
        {"request_digest": "sha256:" + "g" * 64},
        "CHECK constraint failed",
    ),
    (
        "claim_digest_short",
        M1_TABLES[1],
        {"request_digest": "sha256:" + "a" * 63},
        "CHECK constraint failed",
    ),
    (
        "claim_digest_long",
        M1_TABLES[1],
        {"request_digest": "sha256:" + "a" * 65},
        "CHECK constraint failed",
    ),
    ("claim_time_null", M1_TABLES[1], {"claimed_at_us": None}, "NOT NULL"),
    (
        "claim_time_text",
        M1_TABLES[1],
        {"claimed_at_us": 1.25},
        "CHECK constraint failed",
    ),
    # Outcomes.
    ("outcome_id_null", M1_TABLES[2], {"outcome_id": None}, "NOT NULL|PRIMARY KEY"),
    ("outcome_id_empty", M1_TABLES[2], {"outcome_id": ""}, "CHECK constraint failed"),
    ("outcome_claim_null", M1_TABLES[2], {"claim_id": None}, "NOT NULL"),
    ("outcome_branch_null", M1_TABLES[2], {"outcome_branch": None}, "NOT NULL"),
    (
        "outcome_branch_unknown",
        M1_TABLES[2],
        {"outcome_branch": "partial"},
        "CHECK constraint failed",
    ),
    (
        "outcome_success_with_code",
        M1_TABLES[2],
        {"error_code": "conflict"},
        "CHECK constraint failed",
    ),
    (
        "outcome_error_without_code",
        M1_TABLES[2],
        {"outcome_branch": "error"},
        "CHECK constraint failed",
    ),
    (
        "outcome_both_representations",
        M1_TABLES[2],
        {"outcome_reference": "ref://outcome"},
        "CHECK constraint failed",
    ),
    (
        "outcome_no_representation",
        M1_TABLES[2],
        {"outcome_json": None},
        "CHECK constraint failed",
    ),
    (
        "outcome_json_too_long",
        M1_TABLES[2],
        {"outcome_json": "j" * 8193},
        "CHECK constraint failed",
    ),
    (
        "outcome_reference_too_long",
        M1_TABLES[2],
        {"outcome_json": None, "outcome_reference": "r" * 257},
        "CHECK constraint failed",
    ),
    ("outcome_digest_null", M1_TABLES[2], {"outcome_digest": None}, "NOT NULL"),
    (
        "outcome_digest_uppercase",
        M1_TABLES[2],
        {"outcome_digest": "SHA256:" + "b" * 64},
        "CHECK constraint failed",
    ),
    ("outcome_audit_null", M1_TABLES[2], {"audit_ref": None}, "NOT NULL"),
    ("outcome_time_null", M1_TABLES[2], {"settled_at_us": None}, "NOT NULL"),
    (
        "outcome_time_zero",
        M1_TABLES[2],
        {"settled_at_us": 0},
        "CHECK constraint failed",
    ),
]


@pytest.mark.parametrize(
    ("table", "overrides", "expected"),
    [
        (table, overrides, expected)
        for _case, table, overrides, expected in NEGATIVE_CASES
    ],
    ids=[case for case, _table, _overrides, _expected in NEGATIVE_CASES],
)
def test_m1_21_m1_22_m1_23_every_null_empty_enum_bound_and_digest_negative(
    owned: Owned, table: str, overrides: dict[str, object], expected: str
) -> None:
    """M1-21/22/23: refused for the legitimate owner, which is the strong form.

    Each case runs under a current lease and an open guard, so the insert clears the
    trigger and it is the declared constraint -- not the fencing layer -- that says
    no. A negative that only ever fired because no one had authority would prove
    nothing about the schema.
    """
    seed_triad(owned)
    before = {name: count(owned.connection, name) for name in M1_TABLES}
    with pytest.raises((sqlite3.IntegrityError, sqlite3.DatabaseError), match=expected):
        write(owned, table, **overrides)
    assert {name: count(owned.connection, name) for name in M1_TABLES} == before


def test_m1_23b_the_full_signed_64_bit_microsecond_domain_is_carried(
    owned: Owned,
) -> None:
    """M1-23: the largest signed 64-bit microsecond survives without narrowing."""
    write(owned, M1_TABLES[0], audit_ref="aud-max", recorded_at_us=MAX_SIGNED_64)
    stored = owned.connection.execute(
        "SELECT recorded_at_us, typeof(recorded_at_us) FROM "
        "omnivia_application_audit_events WHERE audit_ref = 'aud-max'"
    ).fetchone()
    assert stored == (MAX_SIGNED_64, "integer")


def _metadata(**overrides: object) -> RequestMetadata:
    fields: dict[str, Any] = {
        "request_id": "req-0001",
        "correlation_id": "cor-0001",
        "trace_id": "trc-0001",
        "api_version": CONTRACT_VERSION,
        "client": ClientIdentity(id="test-client", version="0.1.0"),
        "scopes": ("memory.read", "memory.write"),
        "purpose": PURPOSE,
        "required_capabilities": (
            CapabilityRequirement(
                id="core.memory", minimum_version="1.0.0", required=True
            ),
        ),
        "workspace_id": WORKSPACE_ID,
        "deadline_ms": 30_000,
        "idempotency_key": IDEMPOTENCY_KEY,
    }
    fields.update(overrides)
    return RequestMetadata(**fields)


def _equivalence(
    *, operation_input: object | None = None, **metadata_overrides: object
) -> Any:
    return idempotency_equivalence(
        OPERATION,
        _metadata(**metadata_overrides),
        {"candidate_id": "cand-1"} if operation_input is None else operation_input,
        principal_id=PRINCIPAL,
        workspace_id=WORKSPACE_ID,
    )


def test_m1_24_the_stored_digest_is_the_accepted_equivalence_fingerprint(
    owned: Owned,
) -> None:
    """M1-24: the schema stores what the contract function computed, verbatim."""
    equivalence = _equivalence()
    assert equivalence.fingerprint.startswith("sha256:")
    assert len(equivalence.fingerprint) == 71

    write(owned, M1_TABLES[0])
    write(owned, M1_TABLES[1], request_digest=equivalence.fingerprint)
    stored = owned.connection.execute(
        "SELECT workspace_id, principal_id, operation, idempotency_key, request_digest "
        "FROM omnivia_idempotency_claims"
    ).fetchone()
    assert stored == (
        equivalence.scope.workspace_id,
        equivalence.scope.principal_id,
        equivalence.scope.operation,
        equivalence.scope.idempotency_key,
        equivalence.fingerprint,
    )


@pytest.mark.parametrize("excluded", IDEMPOTENCY_EQUIVALENCE_EXCLUDED_INPUTS)
def test_m1_24b_an_excluded_retry_fact_keeps_the_stored_digest_identical(
    excluded: str,
) -> None:
    """M1-24: a genuine retry changes these, and stays the same claim."""
    changed: dict[str, object] = {
        "request_id": "req-retry",
        "correlation_id": "cor-retry",
        "trace_id": "trc-retry",
        "deadline_ms": 90_000,
        "client": ClientIdentity(id="test-client", version="9.9.9"),
    }
    first = _equivalence()
    second = _equivalence(**{excluded: changed[excluded]})
    assert second.fingerprint == first.fingerprint
    assert classify_idempotent_replay(first, second) == IDEMPOTENCY_REPLAY


@pytest.mark.parametrize("included", IDEMPOTENCY_EQUIVALENCE_INCLUDED_INPUTS)
def test_m1_24c_an_included_request_fact_changes_the_stored_digest(
    included: str,
) -> None:
    """M1-24: and a different request is a different digest, hence a conflict."""
    first = _equivalence()
    if included == "operation_input":
        second = _equivalence(operation_input={"candidate_id": "cand-2"})
    elif included == "purpose":
        second = _equivalence(purpose="operations.write")
    elif included == "scopes":
        second = _equivalence(scopes=("memory.read",))
    else:
        second = _equivalence(
            required_capabilities=(
                CapabilityRequirement(
                    id="core.memory", minimum_version="1.0.0", required=False
                ),
            )
        )
    assert second.fingerprint != first.fingerprint
    assert classify_idempotent_replay(first, second) == IDEMPOTENCY_CONFLICT


def test_m1_24d_canonically_equivalent_requests_share_one_digest() -> None:
    """M1-24: reordering an unordered set is the same request, not a new one."""
    first = _equivalence(scopes=("memory.read", "memory.write"))
    second = _equivalence(scopes=("memory.write", "memory.read", "memory.read"))
    assert second.fingerprint == first.fingerprint
    assert classify_idempotent_replay(first, second) == IDEMPOTENCY_REPLAY

    # A different scope is a different scope, never merged into this one.
    elsewhere = idempotency_equivalence(
        OPERATION,
        _metadata(),
        {"candidate_id": "cand-1"},
        principal_id="principal-other",
        workspace_id=WORKSPACE_ID,
    )
    assert classify_idempotent_replay(first, elsewhere) == IDEMPOTENCY_DISTINCT


# --- M1-25 … M1-31: triggers, fencing and the stock-SQLite boundary -----------


def test_m1_25_all_nine_statement_triggers_are_introspectable(migrated: Path) -> None:
    """M1-25: three tables, three statement classes each, by exact name."""
    connection = open_database(migrated, OpenMode.READ_ONLY)
    try:
        present = object_names(connection, "trigger")
        assert set(M1_TRIGGERS) <= present
        assert set(M1_TRIGGERS) <= set(trigger_names(connection))

        covered: dict[str, set[str]] = {}
        for name in M1_TRIGGERS:
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                (name,),
            ).fetchone()
            assert row is not None, name
            found = re.search(
                r"BEFORE\s+(INSERT|UPDATE|DELETE)\s+ON\s+(\w+)",
                str(row[0]),
                re.IGNORECASE,
            )
            assert found is not None, name
            covered.setdefault(found.group(2), set()).add(found.group(1).upper())

        assert covered == {table: {"INSERT", "UPDATE", "DELETE"} for table in M1_TABLES}
        # And the derived guard inventory already treats them as required.
        assert set(M1_TABLES) <= set(GUARDED_TABLES)
        assert_guards_intact(connection)
    finally:
        connection.close()


@pytest.mark.parametrize("table", M1_TABLES)
@pytest.mark.parametrize("statement", ["INSERT", "UPDATE", "DELETE"])
def test_m1_26_no_open_guard_means_no_write_at_all(
    owned: Owned, table: str, statement: str
) -> None:
    """M1-26: with the guard closed, all three statement classes are refused."""
    seed_triad(owned)
    close_guard(owned.connection)

    statements = {
        "UPDATE": f"UPDATE {table} SET workspace_id = 'x'",
        "DELETE": f"DELETE FROM {table}",
    }
    with pytest.raises(sqlite3.DatabaseError, match=REFUSED_EXTERNAL_WRITE):
        if statement == "INSERT":
            with owned.connection:
                insert(owned.connection, table, row_for(table, **_unique_ids(table)))
        else:
            with owned.connection:
                owned.connection.execute(statements[statement])


def _unique_ids(table: str) -> dict[str, object]:
    return {
        M1_TABLES[0]: {"audit_ref": "aud-unguarded"},
        M1_TABLES[1]: {"claim_id": "clm-unguarded", "idempotency_key": "key-unguarded"},
        M1_TABLES[2]: {"outcome_id": "out-unguarded", "claim_id": "clm-0001"},
    }[table]


def test_m1_27_the_current_owner_writes_the_triad_in_one_transaction(
    owned: Owned,
) -> None:
    """M1-27: claim, its audit event and its outcome commit together."""
    seed_triad(owned)
    for table in M1_TABLES:
        assert count(owned.connection, table) == 1
    linked = owned.connection.execute(
        "SELECT a.audit_ref, c.claim_id, o.outcome_id FROM "
        "omnivia_application_audit_events a "
        "JOIN omnivia_idempotency_claims c ON c.audit_ref = a.audit_ref "
        "JOIN omnivia_idempotency_outcomes o ON o.claim_id = c.claim_id"
    ).fetchall()
    assert linked == [("aud-0001", "clm-0001", "out-0001")]


def test_m1_27b_a_later_audit_event_links_the_claim_and_outcome_it_decided(
    owned: Owned,
) -> None:
    """M1-27: the optional links are usable, in one transaction, with no UPDATE.

    An event recorded before an outcome exists cannot name it, so the link belongs to
    a *later* event rather than to a backfill of the first -- which is exactly why
    refusing UPDATE forever costs the schema nothing.
    """
    with fenced_transaction(
        owned.connection,
        owned.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=owned.generation,
    ):
        insert(owned.connection, M1_TABLES[0], row_for(M1_TABLES[0]))
        insert(owned.connection, M1_TABLES[1], row_for(M1_TABLES[1]))
        insert(owned.connection, M1_TABLES[2], row_for(M1_TABLES[2]))
        insert(
            owned.connection,
            M1_TABLES[0],
            row_for(
                M1_TABLES[0],
                audit_ref="aud-0002",
                claim_id="clm-0001",
                outcome_id="out-0001",
                recorded_at_us=1_700_000_000_000_003,
            ),
        )

    linked = owned.connection.execute(
        "SELECT audit_ref, claim_id, outcome_id FROM omnivia_application_audit_events "
        "ORDER BY recorded_at_us"
    ).fetchall()
    assert linked == [("aud-0001", None, None), ("aud-0002", "clm-0001", "out-0001")]
    assert foreign_key_check(owned.connection) == []


@pytest.mark.parametrize("table", M1_TABLES)
@pytest.mark.parametrize("statement", ["UPDATE", "DELETE"])
def test_m1_28_updates_and_deletes_always_fail_including_for_the_owner(
    owned: Owned, table: str, statement: str
) -> None:
    """M1-28: append-only means the fenced owner cannot rewrite history either."""
    seed_triad(owned)
    sql = {
        "UPDATE": f"UPDATE {table} SET workspace_id = workspace_id",
        "DELETE": f"DELETE FROM {table}",
    }[statement]

    with (
        pytest.raises(sqlite3.DatabaseError, match="append-only"),
        fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ),
    ):
        owned.connection.execute(sql)
    assert count(owned.connection, table) == 1


@pytest.mark.parametrize("table", M1_TABLES)
def test_m1_29_a_row_bound_to_another_workspace_is_refused(
    owned: Owned, table: str
) -> None:
    """M1-29: every row binds the singleton workspace this database *is*."""
    seed_triad(owned)
    overrides = dict(_unique_ids(table))
    overrides["workspace_id"] = OTHER_WORKSPACE_ID
    with pytest.raises(sqlite3.DatabaseError, match="unguarded INSERT"):
        write(owned, table, **overrides)
    assert count(owned.connection, table) == 1


def test_m1_29b_wrong_service_instance_or_generation_cannot_write(owned: Owned) -> None:
    """M1-29: a correct-looking tuple from the wrong holder is still refused."""
    impostor = make_identity(instance="svc-m1-impostor", pid=9999)
    with (
        pytest.raises(StaleGeneration, match="lease belongs to"),
        fenced_transaction(
            owned.connection,
            impostor,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ),
    ):
        insert(owned.connection, M1_TABLES[0], row_for(M1_TABLES[0]))

    with (
        pytest.raises(StaleGeneration),
        fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation + 1,
        ),
    ):
        insert(owned.connection, M1_TABLES[0], row_for(M1_TABLES[0]))

    with (
        pytest.raises(StaleGeneration),
        fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=OTHER_WORKSPACE_ID,
            fencing_generation=owned.generation,
        ),
    ):
        insert(owned.connection, M1_TABLES[0], row_for(M1_TABLES[0]))

    assert count(owned.connection, M1_TABLES[0]) == 0


def test_m1_30_a_takeover_predecessor_fails_before_commit(owned: Owned) -> None:
    """M1-30: the check that matters is the one immediately before COMMIT.

    The write is accepted on entry and the row is inserted; the generation then moves
    under it, exactly as a takeover would. Nothing may become durable after that.
    """
    with (
        pytest.raises(StaleGeneration, match="before commit"),
        fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ),
    ):
        insert(owned.connection, M1_TABLES[0], row_for(M1_TABLES[0]))
        owned.connection.execute(
            "UPDATE omnivia_workspace_state SET fencing_generation = ? "
            "WHERE singleton = 1",
            (owned.generation + 1,),
        )
    assert count(owned.connection, M1_TABLES[0]) == 0


def test_m1_30b_a_superseded_owner_cannot_write_after_a_real_takeover(
    owned: Owned,
) -> None:
    """M1-30: and a real successor's takeover closes the predecessor out entirely."""
    successor = make_identity(instance="svc-m1-two", pid=5151)
    successor_lease = acquire_lease(
        owned.connection,
        successor,
        clock=FakeClock(),
        workspace_id=WORKSPACE_ID,
        holds_storage_lock=True,
        lock_mechanism="flock",
        predecessor=owned.identity.service_instance_id,
    )
    assert successor_lease.fencing_generation > owned.generation

    with (
        pytest.raises(StaleGeneration),
        fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ),
    ):
        insert(owned.connection, M1_TABLES[0], row_for(M1_TABLES[0]))
    assert count(owned.connection, M1_TABLES[0]) == 0

    # The predecessor's guard row is stale too, so even the new generation's number
    # does not let the old instance through.
    with (
        pytest.raises((StaleGeneration, sqlite3.DatabaseError)),
        fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=successor_lease.fencing_generation,
        ),
    ):
        insert(owned.connection, M1_TABLES[0], row_for(M1_TABLES[0]))
    assert count(owned.connection, M1_TABLES[0]) == 0


@pytest.mark.parametrize(
    ("case", "statement"),
    [
        (
            "insert",
            "INSERT INTO omnivia_application_audit_events (audit_ref, workspace_id, "
            "principal_id, operation, purpose, request_id, correlation_id, trace_id, "
            "granted_authority_json, outcome_class, recorded_at_us) VALUES "
            "('aud-stock', '" + WORKSPACE_ID + "', 'p', 'o.p', 'a.b', 'r', 'c', 't', "
            "'{}', 'succeeded', 1)",
        ),
        ("update_claim", "UPDATE omnivia_idempotency_claims SET request_digest = 'x'"),
        ("delete_audit", "DELETE FROM omnivia_application_audit_events"),
        ("delete_outcome", "DELETE FROM omnivia_idempotency_outcomes"),
        (
            "update_outcome",
            "UPDATE omnivia_idempotency_outcomes SET outcome_branch = 'error'",
        ),
        ("delete_claim", "DELETE FROM omnivia_idempotency_claims"),
    ],
)
def test_m1_31_a_stock_sqlite_process_cannot_write_even_with_valid_looking_rows(
    migrated: Path, case: str, statement: str
) -> None:
    """M1-31: a real OS process, the stock VFS, and no OmniVia import anywhere.

    The runtime connection is closed first *on purpose*: the guard row and the held
    lease it leaves behind are exactly the pair a crashed service leaves, and they
    agree with each other. That is the state in which an ordinary `sqlite3` client
    used to be able to walk in and write.
    """
    holder = take_ownership(migrated)
    try:
        seed_triad(holder)
        before = {table: count(holder.connection, table) for table in M1_TABLES}
    finally:
        holder.connection.close()

    persisted = open_database(migrated, OpenMode.READ_ONLY)
    try:
        guard = persisted.execute(
            "SELECT COUNT(*) FROM omnivia_mutation_guard g "
            "JOIN omnivia_workspace_state s ON s.singleton = 1 "
            "JOIN omnivia_workspace_lease l ON l.singleton = 1 "
            "WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation "
            "AND l.service_instance_id = g.service_instance_id "
            "AND l.lifecycle = 'held'"
        ).fetchone()
        assert guard is not None and guard[0] == 1, "the persisted rows must look valid"
    finally:
        persisted.close()

    report = run_child(
        STOCK_SQLITE_CHILD, str(migrated), statement, drop_pythonpath=True
    )
    assert report["omnivia_modules"] == [], report
    assert report["succeeded"] is False, report
    assert re.search(REFUSED_EXTERNAL_WRITE, str(report["error"]), re.IGNORECASE), (
        report
    )

    after = open_database(migrated, OpenMode.READ_ONLY)
    try:
        assert {table: count(after, table) for table in M1_TABLES} == before
    finally:
        after.close()


# --- M1-32 … M1-37: interruption, retry, rollback and backup ------------------


@pytest.mark.parametrize("stop_after", list(range(1, len(MIGRATION_STATEMENTS) + 1)))
def test_m1_32_m1_33_failure_after_every_statement_converges_exactly_once(
    tmp_path: Path, stop_after: int
) -> None:
    """M1-32/33: interrupt after each of 0007's statements; retry lands it once.

    Three properties per run. The database exposes the whole migration or none of it
    -- never a half-built set of tables without their triggers. The failed attempt
    survives as durable evidence carrying the *injected* error, rather than being
    overwritten by whatever the recovery path itself hit. And a genuinely fresh
    process, not just a fresh connection, applies `0007` exactly once afterwards.
    """
    path = tmp_path / "interrupted.sqlite"
    materialise_phase0_baseline(path)
    _apply_through_predecessor(path)

    before = legacy_inventory(path)
    connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        crashing = FailAfterStatement(connection, MIGRATION_STATEMENTS, stop_after)
        with pytest.raises(MigrationInterrupted, match=f"statement {stop_after}$"):
            apply_pending_migrations(
                cast("sqlite3.Connection", crashing),
                mode=OpenMode.EXCLUSIVE_MAINTENANCE,
                service_instance_id=SERVICE_INSTANCE,
                fencing_generation=GENERATION_ONE,
                workspace_id=WORKSPACE_ID,
            )
        assert crashing.executed == stop_after
    finally:
        connection.close()

    interrupted = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        ledger = applied_migrations(interrupted)
        tables = object_names(interrupted, "table")
        objects = (
            object_names(interrupted, "table")
            | object_names(interrupted, "index")
            | object_names(interrupted, "trigger")
        )
        complete = set(M1_TABLES) | set(M1_INDEXES) | set(M1_TRIGGERS)
        if MIGRATION_VERSION in ledger:
            assert complete <= objects, "a recorded 0007 must be complete"
        else:
            assert objects.isdisjoint(complete), sorted(objects & complete)
            assert tables.isdisjoint(M1_TABLES)

        failed = interrupted.execute(
            "SELECT outcome, detail FROM omnivia_migration_attempts "
            "WHERE version = ? AND outcome = 'failed'",
            (MIGRATION_VERSION,),
        ).fetchall()
        assert len(failed) == 1, failed
        assert f"statement {stop_after}" in str(failed[0][1]), failed
        assert integrity_check(interrupted) == []
    finally:
        interrupted.close()

    report = run_child(RETRY_CHILD, str(path), WORKSPACE_ID, SERVICE_INSTANCE)
    assert report["ok"] is True, report
    assert report["ledger"] == list(range(1, MIGRATION_VERSION + 1)), report
    assert report["ledger_rows_for_7"] == 1, report
    assert set(M1_TABLES) <= set(report["tables"]), report
    assert (set(M1_INDEXES) | set(M1_TRIGGERS)) <= set(report["objects"]), report
    assert report["integrity"] == [], report
    assert report["foreign_keys"] == [], report
    assert legacy_inventory(path) == before

    settled = open_database(path, OpenMode.READ_ONLY)
    try:
        assert fingerprint_schema(settled).matches(canonical_schema_fingerprint())
    finally:
        settled.close()


def test_m1_34_a_failed_triad_transaction_rolls_all_three_back(owned: Owned) -> None:
    """M1-34: audit, claim and outcome are one unit of work.

    Note the boundary this does *not* cross: there is no domain mutation in this
    slice to commit alongside them, so real domain-mutation atomicity remains an
    explicit obligation on the first consuming vertical.
    """

    class Rejected(RuntimeError):
        pass

    with (
        pytest.raises(Rejected),
        fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ),
    ):
        insert(owned.connection, M1_TABLES[0], row_for(M1_TABLES[0]))
        insert(owned.connection, M1_TABLES[1], row_for(M1_TABLES[1]))
        insert(owned.connection, M1_TABLES[2], row_for(M1_TABLES[2]))
        raise Rejected("the operation failed after recording its intent")

    for table in M1_TABLES:
        assert count(owned.connection, table) == 0

    # And a constraint failure on the last insert takes the first two with it.
    with (
        pytest.raises(sqlite3.IntegrityError),
        fenced_transaction(
            owned.connection,
            owned.identity,
            workspace_id=WORKSPACE_ID,
            fencing_generation=owned.generation,
        ),
    ):
        insert(owned.connection, M1_TABLES[0], row_for(M1_TABLES[0]))
        insert(owned.connection, M1_TABLES[1], row_for(M1_TABLES[1]))
        insert(
            owned.connection,
            M1_TABLES[2],
            row_for(M1_TABLES[2], outcome_digest="not-a-digest"),
        )
    for table in M1_TABLES:
        assert count(owned.connection, table) == 0


@pytest.fixture
def adopted_with_backup(tmp_path: Path) -> tuple[Path, Any, dict[str, Any]]:
    """An adopted workspace, a verified pre-0007 backup, and then 0007 applied."""
    path = tmp_path / "adopted.sqlite"
    materialise_phase0_baseline(path)
    populate_legacy_corpus(path)
    generation = _apply_through_predecessor(path)

    installation = InstallationLayout(root=tmp_path / "installation-state")
    installation.create(WORKSPACE_ID)
    backup = create_verified_backup(
        path, installation, workspace_id=WORKSPACE_ID, attempt_id=new_attempt_id()
    )
    assert backup.verified

    before = open_database(path, OpenMode.READ_ONLY)
    try:
        pre = {
            "fingerprint": fingerprint_schema(before),
            "tables": table_names(before),
            "ledger": applied_migrations(before),
            "state": read_workspace_state(before),
            "legacy": legacy_inventory(path),
        }
    finally:
        before.close()

    connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        applied = apply_pending_migrations(
            connection,
            mode=OpenMode.EXCLUSIVE_MAINTENANCE,
            service_instance_id=SERVICE_INSTANCE,
            fencing_generation=generation,
            workspace_id=WORKSPACE_ID,
        )
        assert [migration.version for migration in applied] == [MIGRATION_VERSION]
    finally:
        connection.close()
    return path, backup, pre


def test_m1_35_rollback_restores_the_exact_pre_0007_workspace(
    adopted_with_backup: tuple[Path, Any, dict[str, Any]],
) -> None:
    """M1-35: schema, counts, values and workspace identity, all exactly as before."""
    path, backup, pre = adopted_with_backup

    migrated_state = open_database(path, OpenMode.READ_ONLY)
    try:
        assert set(M1_TABLES) <= set(table_names(migrated_state))
    finally:
        migrated_state.close()

    restored = rollback_migration(backup, path)
    connection = open_database(restored, OpenMode.READ_ONLY)
    try:
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
        assert fingerprint_schema(connection).matches(pre["fingerprint"])
        assert table_names(connection) == pre["tables"]
        assert set(table_names(connection)).isdisjoint(M1_TABLES)
        assert applied_migrations(connection) == pre["ledger"]
        assert MIGRATION_VERSION not in applied_migrations(connection)
        assert read_workspace_state(connection) == pre["state"]
    finally:
        connection.close()
    assert legacy_inventory(restored) == pre["legacy"]
    assert compare_inventories(backup.source_inventory, _inventory_of(restored)) == []


def _inventory_of(path: Path) -> Any:
    connection = open_database(path, OpenMode.READ_ONLY)
    try:
        return capture_inventory(connection)
    finally:
        connection.close()


#: Bytes deliberately left in the replaced database's sidecars. Not a valid WAL
#: header, so if a restore ever did adopt them the next open would fail loudly rather
#: than quietly -- and either way they must not survive into the restored state.
STALE_SIDECAR_BYTES = b"stale frames that must never be replayed"


def assert_no_stale_frames(sidecars: list[Path]) -> None:
    """No sidecar beside the restored database carries a byte of the stale ones."""
    for sidecar in sidecars:
        if sidecar.exists():
            assert STALE_SIDECAR_BYTES not in sidecar.read_bytes(), sidecar


def test_m1_36_stale_wal_and_shm_cannot_replay_over_a_restore(
    adopted_with_backup: tuple[Path, Any, dict[str, Any]],
) -> None:
    """M1-36: sidecars belonging to the replaced database are discarded, not reused.

    The durable claim is about state, not about which files are on disk. A restore
    drops the replaced database's `-wal` and `-shm` before putting the verified backup
    in their place, and SQLite then recreates an empty pair the moment the restored
    file is opened -- including by the read-only verification `rollback_migration`
    does before it returns. Asserting the paths are absent afterwards would therefore
    be asserting when SQLite happens to create a sidecar, not that the stale frames
    were refused. What must hold is that nothing of them survives and that the
    restored database is exactly the state the backup was verified as.
    """
    path, backup, pre = adopted_with_backup
    sidecars = [path.with_name(path.name + suffix) for suffix in ("-wal", "-shm")]
    for sidecar in sidecars:
        sidecar.write_bytes(STALE_SIDECAR_BYTES)

    restored = rollback_migration(backup, path)
    assert_no_stale_frames(sidecars)

    connection = open_database(restored, OpenMode.READ_ONLY)
    try:
        assert integrity_check(connection) == []
        assert foreign_key_check(connection) == []
        assert fingerprint_schema(connection).matches(pre["fingerprint"])
        assert table_names(connection) == pre["tables"]
        assert set(table_names(connection)).isdisjoint(M1_TABLES)
        assert applied_migrations(connection) == pre["ledger"]
        assert read_workspace_state(connection) == pre["state"]
    finally:
        connection.close()
    assert legacy_inventory(restored) == pre["legacy"]
    assert compare_inventories(backup.source_inventory, _inventory_of(restored)) == []
    # And opening it repeatedly never replays anything into it either.
    assert_no_stale_frames(sidecars)


#: A legacy value the corpus writes exactly once, in an unindexed column, and the
#: same-length replacement put in its place. Same length so the page it lives on keeps
#: its layout, unindexed so no second copy of it exists to disagree with -- which is
#: what makes the damaged file still a structurally valid database carrying one value
#: that is no longer the one the backup was verified as.
TAMPERED_VALUE = b"M1 corpus"
TAMPERED_REPLACEMENT = b"M1 corpuz"


@pytest.mark.parametrize("damage", ["tampered", "incomplete"])
def test_m1_37_a_tampered_or_incomplete_backup_is_refused(
    adopted_with_backup: tuple[Path, Any, dict[str, Any]], damage: str
) -> None:
    """M1-37: a backup damaged after verification is not accepted as one.

    Both damages are applied to the file's bytes rather than through SQL. A backup of
    a workspace is a workspace: it carries the same guard triggers, so a stock SQLite
    client cannot delete a row from it at all, and a test that tried to would be
    proving the guard a second time instead of proving that a damaged backup is
    refused. Byte damage also models what actually happens to a backup after it was
    verified -- nobody edits it through the product.

    The expectations are drawn from what the existing implementation emits, and are
    kept wider than one exact type and message: a torn file can be caught by the
    restore itself, by the integrity check, or by the inventory comparison, and which
    one answers first is not part of the accepted behaviour. That it is refused, and
    that the target is not left standing as a completed rollback, is.
    """
    path, backup, _pre = adopted_with_backup
    target = path.with_name("restore-target.sqlite")

    expected: tuple[type[BaseException], ...]
    if damage == "tampered":
        raw = backup.path.read_bytes()
        at = raw.find(TAMPERED_VALUE)
        assert at > 0, "the legacy value being tampered with is not in the backup"
        assert raw.count(TAMPERED_VALUE) == 1, "the tampered value must be unique"
        backup.path.write_bytes(
            raw[:at] + TAMPERED_REPLACEMENT + raw[at + len(TAMPERED_VALUE) :]
        )
        expected = (LegacyMigrationError,)
        match = "did not restore the original"
    else:
        content = backup.path.read_bytes()
        backup.path.write_bytes(content[: len(content) // 3])
        expected = (StorageError, sqlite3.DatabaseError)
        match = "malformed|not a database|integrity|did not restore"

    with pytest.raises(expected, match=match):
        rollback_migration(backup, target)

    # Nothing may read the target as a successful restore: either it was never
    # produced, or what was produced is not the state the backup was verified as.
    if target.exists():
        assert compare_inventories(backup.source_inventory, _inventory_of(target)) != []


# --- M1-38: this module adds no skip and no xfail -----------------------------


def test_m1_38_this_module_declares_no_skip_or_xfail() -> None:
    """M1-38: acceptance stays green without anything being excused from running."""
    source = Path(__file__).read_text(encoding="utf-8")
    for pattern in (
        r"@pytest\.mark\.skip",
        r"@pytest\.mark\.xfail",
        r"pytest\.skip\(",
        r"pytest\.xfail\(",
        r"pytest\.importorskip\(",
    ):
        assert re.search(pattern, source) is None, pattern
