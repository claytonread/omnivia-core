"""Fenced write transactions (T-0629F, ADR-037).

The mutation token is `(workspace_id, fencing_generation, service_instance_id)`.

Checking the integer generation before a transaction is not sufficient, and this is
the crux of the whole design. Between a pre-flight check and a commit, a takeover
can increment the generation; the writer would then commit under authority it no
longer holds. So the token is validated *inside* the write transaction, enforced by
persisted triggers that fire for any writer, and validated *again* immediately
before commit. SQLite's writer lock prevents a valid takeover increment from
interleaving with a live guarded transaction.

Three layers, because each covers what the others cannot:

1. **In-transaction validation** — catches a generation that changed before this
   transaction began.
2. **Persisted triggers** — fire for connections this runtime did not create, which
   an authorizer cannot reach.
3. **Connection authorizer** — denies DDL and unguarded DML on runtime-created
   connections, including statements that never touch a guarded table.

None of the three is a security boundary against arbitrary code running as the
workspace-owning OS principal. ADR-037 states that explicitly: that principal can
terminate the service, alter triggers offline or rewrite bytes. Drift is *detected*
before writable readiness, not prevented.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

from omnivia_core_runtime.ownership.identity import Clock, ServiceInstanceIdentity
from omnivia_core_runtime.storage.connection import (
    SchemaFingerprint,
    StorageError,
    authorised,
    clear_authorizer,
    fingerprint_schema,
    install_authorizer,
)

#: The ownership substrate: the mechanism, never its subject.
#:
#: A trigger on `omnivia_mutation_guard` would make opening a guard a mutation that
#: requires an open guard, and the ledger and lease tables are written by the very
#: operations that establish authority. Listed explicitly because this is a real
#: exemption that has to be justified, not an oversight.
UNGUARDED_SUBSTRATE_TABLES = frozenset(
    {
        "omnivia_mutation_guard",
        "omnivia_workspace_state",
        "omnivia_workspace_lease",
        "omnivia_schema_migrations",
        "omnivia_migration_attempts",
        "omnivia_workspace_open_events",
    }
)


@lru_cache(maxsize=1)
def guarded_tables() -> tuple[str, ...]:
    """Every mutable table in the canonical schema, minus the substrate.

    Derived, because the hand-written list was wrong in two directions at once: it
    named nine of sixteen mutable tables, and four of those nine were missing one of
    their three statement triggers, so a table could be "guarded" while its UPDATEs
    committed unchecked.

    A list cannot notice a table that was added after it was written. Deriving from
    the canonical schema inverts the default -- a new table is guarded unless it is
    explicitly declared substrate -- so the failure mode of forgetting becomes a
    refusal rather than an unguarded write.

    Cached so that reading `GUARDED_TABLES` returns the same object every time.
    Rebuilding the tuple per read made a name that reads like a constant a different
    object on each access, which is a difference nothing should have to think about.
    """
    from omnivia_core_runtime.storage.migrations import canonical_schema_tables

    return tuple(
        sorted(name for name in canonical_schema_tables() if name not in UNGUARDED_SUBSTRATE_TABLES)
    )


if TYPE_CHECKING:
    #: Every mutable table in the canonical schema, minus the ownership substrate.
    #: Declared for static tools only -- `__getattr__` below is what resolves it, so
    #: without this the name is invisible to anything that does not run the module.
    GUARDED_TABLES: tuple[str, ...]


def __getattr__(name: str) -> object:
    """Resolve `GUARDED_TABLES` on first read rather than at import.

    Deriving it eagerly meant `import fencing` built an in-memory database and
    replayed every migration -- work no importer necessarily needs, and it made this
    module's import depend on `migrations` at import time, one edge away from a cycle
    with `connection`. PEP 562 keeps `from ... import GUARDED_TABLES` working while
    the cost moves to whoever actually reads it; `guarded_tables()` caches through
    `canonical_schema_tables`, so repeated reads are free.
    """
    if name == "GUARDED_TABLES":
        return guarded_tables()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class FencingError(StorageError):
    """A mutation was attempted without current authority."""


class StaleGeneration(FencingError):
    """The authority tuple changed; the transaction must not commit."""


class SchemaDrift(FencingError):
    """The schema or triggers differ from the expected fingerprint."""


@dataclass(frozen=True)
class MutationGuard:
    """The authority tuple currently permitted to mutate."""

    workspace_id: str
    service_instance_id: str
    fencing_generation: int


def guard_present(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'omnivia_mutation_guard'"
    ).fetchone()
    return row is not None


def read_guard(connection: sqlite3.Connection) -> MutationGuard | None:
    if not guard_present(connection):
        return None
    row = connection.execute(
        "SELECT workspace_id, service_instance_id, fencing_generation "
        "FROM omnivia_mutation_guard WHERE singleton = 1"
    ).fetchone()
    if row is None:
        return None
    return MutationGuard(
        workspace_id=str(row[0]),
        service_instance_id=str(row[1]),
        fencing_generation=int(row[2]),
    )


def open_guard(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    clock: Clock,
    workspace_id: str,
    fencing_generation: int,
) -> MutationGuard:
    """Install the guard row for this service instance.

    Called once when writable ownership is taken, not per transaction: the triggers
    read this row, and rewriting it per statement would make the guard itself a
    mutation that needs guarding.
    """
    if not guard_present(connection):
        raise FencingError("mutation guard table is absent; migrations are incomplete")

    connection.execute("BEGIN IMMEDIATE")
    try:
        # Against the lease, not just the generation. The generation is a number the
        # caller supplies and any process can read; the lease is the record of who
        # actually holds the workspace. Checking only the generation let a service
        # identity that never acquired the lease install itself as the guard owner
        # and write under the real owner's authority.
        #
        # Inside the transaction on purpose: a check that passes and then commits
        # under a lease that moved in between proves nothing.
        _assert_lease_owner(connection, identity, workspace_id, fencing_generation)
        # Widened only now, with the lease proven, and only for these two
        # statements. The guard table is not writable outside this block.
        with authorised(connection, mutations=True):
            connection.execute("DELETE FROM omnivia_mutation_guard WHERE singleton = 1")
            connection.execute(
                "INSERT INTO omnivia_mutation_guard (singleton, workspace_id, "
                "service_instance_id, fencing_generation, opened_at) "
                "VALUES (1, ?, ?, ?, ?)",
                (
                    workspace_id,
                    identity.service_instance_id,
                    fencing_generation,
                    clock.wall_time().isoformat(),
                ),
            )
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")

    guard = read_guard(connection)
    if guard is None:  # pragma: no cover - the transaction committed
        raise FencingError("guard committed but is unreadable")
    return guard


def close_guard(connection: sqlite3.Connection) -> None:
    """Remove the guard row, so no mutation is permitted until it is reopened."""
    if not guard_present(connection):
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        # Closing is always permitted: it removes authority, never grants it.
        with authorised(connection, mutations=True):
            connection.execute("DELETE FROM omnivia_mutation_guard WHERE singleton = 1")
    except BaseException:  # pragma: no cover - defensive
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


def _assert_lease_owner(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    workspace_id: str,
    fencing_generation: int,
) -> None:
    """Prove the exact tuple against the authoritative lease.

    Imported here rather than at module scope: `lease` reaches into `storage`, and a
    top-level import would close a cycle through it.
    """
    from omnivia_core_runtime.ownership.lease import (
        LeaseError,
        assert_current_authority,
    )

    try:
        assert_current_authority(connection, identity, workspace_id, fencing_generation)
    except LeaseError as error:
        raise StaleGeneration(str(error)) from error


def _validate(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    workspace_id: str,
    fencing_generation: int,
    when: str,
) -> None:
    # The lease is the authority; workspace state alone is not. A guard row is an
    # ordinary row that the caller being validated may have written, so validating a
    # transaction against the guard and the generation compares the claim with a
    # copy of itself.
    try:
        _assert_lease_owner(connection, identity, workspace_id, fencing_generation)
    except StaleGeneration as error:
        raise StaleGeneration(f"authority is not current {when}: {error}") from error

    guard = read_guard(connection)
    if guard is None:
        raise StaleGeneration(f"no mutation guard is open ({when})")
    if (
        guard.workspace_id != workspace_id
        or guard.fencing_generation != fencing_generation
        or guard.service_instance_id != identity.service_instance_id
    ):
        raise StaleGeneration(
            f"mutation guard does not match this instance ({when}): guard holds "
            f"({guard.workspace_id!r}, {guard.fencing_generation}, "
            f"{guard.service_instance_id!r})"
        )


@contextmanager
def fenced_transaction(
    connection: sqlite3.Connection,
    identity: ServiceInstanceIdentity,
    *,
    workspace_id: str,
    fencing_generation: int,
) -> Iterator[sqlite3.Connection]:
    """A write transaction that validates authority on entry and before commit.

    The second validation is the one that matters. A pre-flight check proves the
    generation was current when the transaction *started*; only a check immediately
    before COMMIT proves it is still current when the write becomes durable.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        _validate(connection, identity, workspace_id, fencing_generation, "on entry")
        # The one place guarded-table DML is permitted on a service connection, and
        # only between the two validations.
        with authorised(connection, mutations=True):
            yield connection
        _validate(connection, identity, workspace_id, fencing_generation, "before commit")
    except BaseException:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.OperationalError:  # pragma: no cover - no active transaction
            pass
        raise
    connection.execute("COMMIT")


def trigger_names(connection: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "AND name LIKE 'omnivia_guard_%' ORDER BY name"
        ).fetchall()
    )


def verify_fingerprint(
    connection: sqlite3.Connection, expected: SchemaFingerprint
) -> SchemaFingerprint:
    """Verify schema and trigger identity before writable readiness.

    ADR-037 requires canonical schema and trigger drift to be detected before
    writable readiness and to fail closed. This is detection, not prevention: the
    file-owning principal can alter triggers while the service is offline, and the
    honest guarantee is that such a change is noticed on the next open.
    """
    actual = fingerprint_schema(connection)
    if not actual.matches(expected):
        raise SchemaDrift(
            "schema or trigger fingerprint differs from the expected value "
            f"(expected {expected.digest[:12]}… with {expected.triggers} triggers, "
            f"found {actual.digest[:12]}… with {actual.triggers} triggers)"
        )
    return actual


def expected_trigger_names() -> tuple[str, ...]:
    """Guard trigger names declared by the migration, read from the migration itself.

    Derived rather than hard-coded so the expectation cannot drift from the SQL that
    creates them: adding a guard to a migration automatically makes its absence a
    detected defect.

    Read from *every* migration, not just the one that introduced the guard. Scanning
    only `0002` meant a guard added later was never checked for -- the same shape of
    defect as the hand-written table list, one level up.
    """
    from omnivia_core_runtime.storage.migrations import load_migrations

    names: set[str] = set()
    for migration in load_migrations():
        names.update(
            re.findall(r"CREATE TRIGGER IF NOT EXISTS\s+(omnivia_guard_\w+)", migration.sql)
        )
    return tuple(sorted(names))


def assert_guards_intact(connection: sqlite3.Connection) -> None:
    """Every declared guard trigger must be present, by exact name.

    Exact-set comparison rather than "at least one trigger per table". A per-table
    check would pass while a DELETE guard was missing and its INSERT sibling
    survived, which is precisely the drift that would let unguarded deletes through.
    """
    present = set(trigger_names(connection))
    expected = set(expected_trigger_names())
    missing = sorted(expected - present)
    unexpected = sorted(present - expected)
    if missing:
        raise SchemaDrift(f"guard triggers are missing: {missing}")
    if unexpected:
        raise SchemaDrift(f"unrecognised guard triggers are present: {unexpected}")


__all__ = [
    "GUARDED_TABLES",
    "FencingError",
    "MutationGuard",
    "SchemaDrift",
    "StaleGeneration",
    "assert_guards_intact",
    "clear_authorizer",
    "close_guard",
    "expected_trigger_names",
    "fenced_transaction",
    "guard_present",
    "install_authorizer",
    "open_guard",
    "read_guard",
    "trigger_names",
    "verify_fingerprint",
]
