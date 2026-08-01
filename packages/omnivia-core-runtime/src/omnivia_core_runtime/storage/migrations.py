"""Ordered, checksum-pinned migrations and Generation-1 bootstrap (T-0629B).

The migration ledger is authoritative. `PRAGMA user_version` mirrors its highest
applied version for diagnostics only, because a single integer cannot record
checksums, attempts or the authority a migration ran under.

Bootstrap resolves a circular-authority problem. The normal fenced migration path
validates the current `(workspace_id, fencing_generation, service_instance_id)`
tuple inside every write transaction, but a pristine or frozen Phase 0 database
contains no table to read that tuple from. Bootstrap is therefore the one
operation that creates ownership state without first proving ownership through it,
and it earns that right a different way: it runs only while holding the lifetime
storage lock and the sole exclusive SQLite connection, only against a database
whose schema fingerprint exactly matches the frozen Phase 0 artifact, and only
inside one atomic transaction. Every later migration runs under the tuple this
establishes.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from importlib import resources
from pathlib import Path

from omnivia_core_runtime.storage.connection import (
    OpenMode,
    SchemaCreationRefused,
    SchemaFingerprint,
    StorageError,
    authorised,
    execute_script,
    fingerprint_schema,
    fingerprint_sql_script,
    immediate_transaction,
)

MIGRATION_PACKAGE = "omnivia_core_runtime.storage.migration_files"

#: The frozen Phase 0 legacy schema. Extracted once and checked in; never
#: regenerated from live runtime code.
PHASE0_BASELINE_FILE = "0000_phase0_baseline.sql"

#: Baseline states recorded in `omnivia_workspace_state.baseline_state`.
BASELINE_ADOPTED = "baseline_adopted"
BASELINE_PRISTINE = "pristine"

GENERATION_ONE = 1


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Migration:
    """One ordered, checksum-pinned migration."""

    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        """SHA-256 of the migration text.

        Pinning the checksum is what makes "migration 3 has been applied" mean
        "*this* migration 3", so an edited migration is detected rather than
        silently accepted as already done.
        """
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def _read_migration_file(name: str) -> str:
    return resources.files(MIGRATION_PACKAGE).joinpath(name).read_text(encoding="utf-8")


def phase0_baseline_sql() -> str:
    """The frozen Phase 0 schema DDL."""
    return _read_migration_file(PHASE0_BASELINE_FILE)


def phase0_fingerprint() -> str:
    """Schema fingerprint of the frozen Phase 0 artifact.

    Derived from the checked-in `.sql` file, not from the legacy runtime. This is
    the independence T-0629B requires: a change to the legacy `_init_schema` can no
    longer move the oracle that is supposed to police it.
    """
    return fingerprint_sql_script(phase0_baseline_sql())


@lru_cache(maxsize=1)
def canonical_schema_tables() -> frozenset[str]:
    """Every table the frozen artifacts define, from the artifacts themselves."""
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(phase0_baseline_sql())
        for migration in load_migrations():
            connection.executescript(migration.sql)
        return frozenset(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        )
    finally:
        connection.close()


@lru_cache(maxsize=1)
def canonical_schema_fingerprint() -> SchemaFingerprint:
    """The schema the frozen artifacts describe, built without touching any workspace.

    Readiness needs an expectation that a drifted database cannot influence. Taking
    it from the database under test -- `verify_fingerprint(conn, fingerprint_schema(conn))`
    -- compares a value with itself and passes by construction, so an object added
    while no service held the workspace is accepted as canonical.

    Rebuilding the baseline and every migration in memory keeps the oracle where the
    reviewed artifacts are. The cache is safe because those artifacts are packaged
    files: they cannot change within a process lifetime.
    """
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(phase0_baseline_sql())
        for migration in load_migrations():
            connection.executescript(migration.sql)
        return fingerprint_schema(connection)
    finally:
        connection.close()


GUARD_MIGRATION_FILE = "0002_mutation_guard.sql"


def guard_migration_sql() -> str:
    """The mutation-guard migration text, so its trigger names can be derived."""
    return _read_migration_file(GUARD_MIGRATION_FILE)


def load_migrations() -> tuple[Migration, ...]:
    """Every ordered migration, excluding the Phase 0 baseline artifact.

    The baseline is evidence of where migration starts, not a migration: applying
    it would recreate the legacy schema rather than advance from it.
    """
    package = resources.files(MIGRATION_PACKAGE)
    found: list[Migration] = []
    for entry in package.iterdir():
        name = entry.name
        if not name.endswith(".sql") or name == PHASE0_BASELINE_FILE:
            continue
        prefix = name.split("_", 1)[0]
        if not prefix.isdigit():
            raise StorageError(f"migration file has no ordered prefix: {name}")
        found.append(
            Migration(
                version=int(prefix),
                name=name,
                sql=entry.read_text(encoding="utf-8"),
            )
        )

    ordered = tuple(sorted(found, key=lambda m: m.version))
    versions = [m.version for m in ordered]
    if len(set(versions)) != len(versions):
        raise StorageError(f"duplicate migration versions: {versions}")
    return ordered


def substrate_present(connection: sqlite3.Connection) -> bool:
    """Whether the ownership substrate exists."""
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'omnivia_workspace_state'"
    ).fetchone()
    return row is not None


@dataclass(frozen=True)
class WorkspaceState:
    """The authoritative workspace state row."""

    workspace_id: str
    fencing_generation: int
    workspace_format_version: str
    baseline_state: str


def read_workspace_state(connection: sqlite3.Connection) -> WorkspaceState | None:
    """Read the single workspace-state row, or `None` before bootstrap."""
    if not substrate_present(connection):
        return None
    row = connection.execute(
        "SELECT workspace_id, fencing_generation, workspace_format_version, "
        "baseline_state FROM omnivia_workspace_state WHERE singleton = 1"
    ).fetchone()
    if row is None:
        return None
    return WorkspaceState(
        workspace_id=str(row[0]),
        fencing_generation=int(row[1]),
        workspace_format_version=str(row[2]),
        baseline_state=str(row[3]),
    )


def applied_migrations(connection: sqlite3.Connection) -> dict[int, str]:
    """Applied version to recorded checksum."""
    if not substrate_present(connection):
        return {}
    return {
        int(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT version, checksum FROM omnivia_schema_migrations ORDER BY version"
        ).fetchall()
    }


def bootstrap_generation_one(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    workspace_format_version: str = "1",
    mode: OpenMode,
    expect_phase0_baseline: bool,
    service_instance_id: str | None = None,
) -> WorkspaceState:
    """Create the ownership substrate and assign fencing generation 1.

    Crash safety comes from doing all of it in one `BEGIN IMMEDIATE` transaction:
    a crash at any statement leaves either the unchanged prior state or the
    complete substrate, never a partial one. Because the substrate creation is
    `IF NOT EXISTS` and the state row is guarded by a `singleton` primary key,
    a restart either finds the work done or redoes it safely.

    `expect_phase0_baseline` distinguishes adopting a frozen legacy database from
    initialising a pristine one. When true the schema must match the frozen Phase 0
    fingerprint exactly, so an unknown database cannot be adopted as if its shape
    were understood.
    """
    if not mode.exclusive:
        raise StorageError(
            f"bootstrap requires an exclusive connection, got mode={mode.value}"
        )

    existing = read_workspace_state(connection)
    if existing is not None:
        # Not an error: a restart after a crash between commit and the caller
        # observing it must not re-bootstrap. Generation 1 is created exactly once.
        return existing

    if expect_phase0_baseline:
        actual = fingerprint_schema(connection).digest
        expected = phase0_fingerprint()
        if actual != expected:
            raise StorageError(
                "refusing to adopt a database whose schema is not the frozen "
                f"Phase 0 baseline (expected {expected[:12]}…, got {actual[:12]}…)"
            )
        baseline_state = BASELINE_ADOPTED
    else:
        if fingerprint_schema(connection).tables:
            raise SchemaCreationRefused(
                "pristine bootstrap requires an empty database; this one has tables"
            )
        baseline_state = BASELINE_PRISTINE

    substrate = _substrate_migration()
    timestamp = _now()
    attempt_id = f"bootstrap-{uuid.uuid4()}"

    with immediate_transaction(connection), authorised(
        connection, mutations=True, ddl=True
    ):
        execute_script(connection, substrate.sql)
        connection.execute(
            "INSERT INTO omnivia_migration_attempts "
            "(attempt_id, version, outcome, started_at, finished_at, detail) "
            "VALUES (?, ?, 'succeeded', ?, ?, 'generation-1 bootstrap')",
            (attempt_id, substrate.version, timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO omnivia_workspace_state "
            "(singleton, workspace_id, fencing_generation, workspace_format_version, "
            "baseline_state, created_at, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?)",
            (
                workspace_id,
                GENERATION_ONE,
                workspace_format_version,
                baseline_state,
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            "INSERT INTO omnivia_schema_migrations "
            "(version, name, checksum, applied_at, applied_by_service_instance, "
            "fencing_generation) VALUES (?, ?, ?, ?, ?, ?)",
            (
                substrate.version,
                substrate.name,
                substrate.checksum,
                timestamp,
                service_instance_id,
                GENERATION_ONE,
            ),
        )
        connection.execute(f"PRAGMA user_version = {substrate.version}")

    state = read_workspace_state(connection)
    if state is None:  # pragma: no cover - the transaction committed
        raise StorageError("bootstrap committed but workspace state is unreadable")
    return state


def _substrate_migration() -> Migration:
    migrations = load_migrations()
    if not migrations:
        raise StorageError("no migrations are available; the substrate is missing")
    return migrations[0]


def apply_pending_migrations(
    connection: sqlite3.Connection,
    *,
    mode: OpenMode,
    service_instance_id: str,
    fencing_generation: int,
    workspace_id: str,
) -> list[Migration]:
    """Apply every unapplied migration under the current authority tuple.

    Each migration is its own transaction with its own durable attempt record, so
    a failure part-way through a series leaves earlier migrations applied and
    recorded, and the failed one recorded as failed rather than invisible.
    """
    if not mode.writable:
        raise StorageError(f"migrations require a writable mode, got {mode.value}")

    state = read_workspace_state(connection)
    if state is None:
        raise StorageError("cannot migrate before the ownership substrate exists")
    if state.workspace_id != workspace_id:
        raise StorageError(
            f"workspace mismatch: database holds {state.workspace_id!r}, "
            f"caller claims {workspace_id!r}"
        )
    if state.fencing_generation != fencing_generation:
        raise StorageError(
            f"stale fencing generation: database is at {state.fencing_generation}, "
            f"caller holds {fencing_generation}"
        )

    recorded = applied_migrations(connection)
    applied: list[Migration] = []

    for migration in load_migrations():
        known = recorded.get(migration.version)
        if known is not None:
            if known != migration.checksum:
                raise StorageError(
                    f"migration {migration.version} ({migration.name}) has changed "
                    f"since it was applied: recorded {known[:12]}…, "
                    f"found {migration.checksum[:12]}…"
                )
            continue

        attempt_id = f"migrate-{uuid.uuid4()}"
        started = _now()
        with authorised(connection, mutations=True):
            connection.execute(
                "INSERT INTO omnivia_migration_attempts "
                "(attempt_id, version, outcome, started_at) VALUES (?, ?, 'started', ?)",
                (attempt_id, migration.version, started),
            )
        try:
            with immediate_transaction(connection), authorised(
                connection, mutations=True, ddl=True
            ):
                _assert_authority(
                    connection, workspace_id, fencing_generation
                )
                execute_script(connection, migration.sql)
                connection.execute(
                    "INSERT INTO omnivia_schema_migrations "
                    "(version, name, checksum, applied_at, "
                    "applied_by_service_instance, fencing_generation) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        migration.version,
                        migration.name,
                        migration.checksum,
                        _now(),
                        service_instance_id,
                        fencing_generation,
                    ),
                )
                connection.execute(f"PRAGMA user_version = {migration.version}")
                _assert_authority(connection, workspace_id, fencing_generation)
        except Exception as exc:
            # Elevated too. Un-elevated, a governed connection failed *here* while
            # recording that it had failed there, which replaced a recorded failure
            # with an unrecorded one and hid the original exception behind
            # `not authorized`.
            with authorised(connection, mutations=True):
                connection.execute(
                    "UPDATE omnivia_migration_attempts SET outcome = 'failed', "
                    "finished_at = ?, detail = ? WHERE attempt_id = ?",
                    (_now(), str(exc)[:500], attempt_id),
                )
            raise
        with authorised(connection, mutations=True):
            connection.execute(
                "UPDATE omnivia_migration_attempts SET outcome = 'succeeded', "
                "finished_at = ? WHERE attempt_id = ?",
                (_now(), attempt_id),
            )
        applied.append(migration)

    return applied


def _assert_authority(
    connection: sqlite3.Connection, workspace_id: str, fencing_generation: int
) -> None:
    """Validate the authority tuple from inside a write transaction.

    Read inside the transaction on purpose. A check performed before `BEGIN` proves
    nothing, because a takeover could have incremented the generation in between —
    which is exactly the failure ADR-037 invariant 6 names.
    """
    row = connection.execute(
        "SELECT workspace_id, fencing_generation FROM omnivia_workspace_state "
        "WHERE singleton = 1"
    ).fetchone()
    if row is None:
        raise StorageError("workspace state disappeared inside a write transaction")
    if str(row[0]) != workspace_id or int(row[1]) != fencing_generation:
        raise StorageError(
            f"authority changed inside the transaction: database holds "
            f"({row[0]!r}, {row[1]}), caller holds ({workspace_id!r}, "
            f"{fencing_generation})"
        )


def record_open_event(
    connection: sqlite3.Connection,
    *,
    open_mode: OpenMode,
    service_instance_id: str | None = None,
    fencing_generation: int | None = None,
) -> str:
    """Record a workspace open for takeover diagnostics."""
    event_id = str(uuid.uuid4())
    with immediate_transaction(connection), authorised(connection, mutations=True):
        connection.execute(
            "INSERT INTO omnivia_workspace_open_events "
            "(event_id, opened_at, open_mode, service_instance_id, fencing_generation) "
            "VALUES (?, ?, ?, ?, ?)",
            (event_id, _now(), open_mode.value, service_instance_id, fencing_generation),
        )
    return event_id


def materialise_phase0_baseline(path: Path) -> None:
    """Create a database carrying exactly the frozen Phase 0 schema.

    Test and migration-staging helper. Uses the checked-in artifact, so a fixture
    built this way is independent of the legacy runtime.
    """
    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(phase0_baseline_sql())
        connection.commit()
    finally:
        connection.close()


__all__ = [
    "BASELINE_ADOPTED",
    "BASELINE_PRISTINE",
    "GENERATION_ONE",
    "GUARD_MIGRATION_FILE",
    "MIGRATION_PACKAGE",
    "PHASE0_BASELINE_FILE",
    "Migration",
    "WorkspaceState",
    "applied_migrations",
    "apply_pending_migrations",
    "bootstrap_generation_one",
    "guard_migration_sql",
    "load_migrations",
    "materialise_phase0_baseline",
    "phase0_baseline_sql",
    "phase0_fingerprint",
    "read_workspace_state",
    "record_open_event",
    "substrate_present",
]
