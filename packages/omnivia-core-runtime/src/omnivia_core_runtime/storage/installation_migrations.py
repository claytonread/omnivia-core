"""Checksum-pinned migrations for the machine-local installation catalogue.

The installation catalogue is deliberately independent of every portable workspace
database.  It has its own migration package, ledger, schema fingerprint and version
oracle; adding an installation table to the workspace migration chain would make a
machine identity portable with whichever workspace happened to be copied.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from importlib import resources

from omnivia_core_runtime.storage.connection import (
    SchemaFingerprint,
    StorageError,
    execute_script,
    fingerprint_schema,
    foreign_key_check,
    integrity_check,
)

INSTALLATION_MIGRATION_PACKAGE = (
    "omnivia_core_runtime.storage.installation_migration_files"
)
INSTALLATION_FORMAT_VERSION = "1"
INSTALLATION_WRITER_FUNCTION = "omnivia_installation_writer"

# Reviewed artifact identities. Discovery remains deterministic, but a new file is not
# silently trusted merely because it was copied into the package: this map must move in
# the same reviewed change.
PINNED_INSTALLATION_MIGRATIONS: dict[str, str] = {
    "0001_installation_authority.sql": (
        "d7a7bf5c79d5e52aceae2e319668b6d799012845f2bb1fc4867643e914650d07"
    ),
    "0002_mcp_principals_and_authoring_intent.sql": (
        "3cb8651338ff5f978ec49a503f3fc625993cf9acff5278cb98eaecc9e107c64f"
    ),
}


class InstallationMigrationError(StorageError):
    """The installation migration history or schema is not the reviewed article."""


@dataclass(frozen=True)
class InstallationMigration:
    """One ordered installation-owned migration artifact."""

    version: int
    name: str
    sql: str
    checksum: str


def load_installation_migrations() -> tuple[InstallationMigration, ...]:
    """Load the exact pinned installation migration set in version order."""
    package = resources.files(INSTALLATION_MIGRATION_PACKAGE)
    names = sorted(
        entry.name for entry in package.iterdir() if entry.name.endswith(".sql")
    )
    expected_names = sorted(PINNED_INSTALLATION_MIGRATIONS)
    if names != expected_names:
        raise InstallationMigrationError(
            "installation migration set differs from the pinned manifest "
            f"(expected={expected_names!r}, actual={names!r})"
        )

    migrations: list[InstallationMigration] = []
    for name in names:
        prefix = name.split("_", 1)[0]
        if not prefix.isdigit():
            raise InstallationMigrationError(
                f"installation migration has no numeric prefix: {name}"
            )
        sql = package.joinpath(name).read_text(encoding="utf-8")
        actual = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        expected = PINNED_INSTALLATION_MIGRATIONS[name]
        if actual != expected:
            raise InstallationMigrationError(
                f"installation migration checksum mismatch for {name} "
                f"(expected={expected}, actual={actual})"
            )
        migrations.append(
            InstallationMigration(
                version=int(prefix), name=name, sql=sql, checksum=actual
            )
        )

    versions = [migration.version for migration in migrations]
    expected_versions = list(range(1, len(migrations) + 1))
    if versions != expected_versions:
        raise InstallationMigrationError(
            "installation migration versions must be contiguous from one "
            f"(expected={expected_versions!r}, actual={versions!r})"
        )
    return tuple(migrations)


def canonical_installation_schema_fingerprint(
    applied_versions: int | None = None,
) -> SchemaFingerprint:
    """Build the expected schema solely from the pinned installation artifacts.

    `applied_versions` names how much of the chain to replay, and exists so a
    catalogue that is legitimately behind the head can still be held to an exact
    schema rather than to no schema at all. The default is the whole chain.

    A partially or manually applied migration is what this makes detectable. Every
    statement in these artifacts is `IF NOT EXISTS`, so replaying one over a
    catalogue where somebody has already created its tables by hand succeeds
    silently and the result fingerprints as a clean head -- drift accepted rather
    than refused. Comparing the *pre-migration* fingerprint against the prefix the
    ledger claims closes that: an object that exists without a ledger row is a
    mismatch before anything is applied.
    """
    migrations = load_installation_migrations()
    if applied_versions is not None:
        if not 0 <= applied_versions <= len(migrations):
            raise InstallationMigrationError(
                "installation schema prefix is outside the pinned chain "
                f"(pinned={len(migrations)}, requested={applied_versions})"
            )
        migrations = migrations[:applied_versions]
    connection = sqlite3.connect(":memory:")
    try:
        for migration in migrations:
            execute_script(connection, migration.sql)
        return fingerprint_schema(connection)
    finally:
        connection.close()


def installation_schema_present(connection: sqlite3.Connection) -> bool:
    """Whether this database already contains the installation singleton table."""
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'omnivia_installation_state'"
        ).fetchone()
        is not None
    )


def apply_initial_installation_schema(
    connection: sqlite3.Connection,
    *,
    installation_id: str,
    owner_instance_id: str,
    now_us: int,
) -> None:
    """Materialise generation one and its ledger inside the caller's transaction.

    The *whole* pinned chain, and a ledger row for every artifact in it. A fresh
    catalogue reaches the head in one transaction rather than being created at
    version one and then immediately migrated, so there is no window in which a new
    installation exists at a version this build no longer serves.
    """
    if fingerprint_schema(connection).tables:
        raise InstallationMigrationError(
            "installation bootstrap requires an empty catalogue database"
        )
    migrations = load_installation_migrations()
    for migration in migrations:
        execute_script(connection, migration.sql)

    connection.execute(
        "INSERT INTO omnivia_installation_state "
        "(singleton, installation_id, installation_format_version, "
        "fencing_generation, owner_instance_id, owner_acquired_at_us, "
        "created_at_us, updated_at_us) VALUES (1, ?, ?, 1, ?, ?, ?, ?)",
        (
            installation_id,
            INSTALLATION_FORMAT_VERSION,
            owner_instance_id,
            now_us,
            now_us,
            now_us,
        ),
    )
    for migration in migrations:
        connection.execute(
            "INSERT INTO omnivia_installation_schema_migrations "
            "(version, name, checksum, installation_id, fencing_generation, "
            "applied_by_owner, applied_at_us) VALUES (?, ?, ?, ?, 1, ?, ?)",
            (
                migration.version,
                migration.name,
                migration.checksum,
                installation_id,
                owner_instance_id,
                now_us,
            ),
        )
    connection.execute(f"PRAGMA user_version = {len(migrations)}")


def _applied_installation_prefix(
    connection: sqlite3.Connection, migrations: tuple[InstallationMigration, ...]
) -> int:
    """How much of the pinned chain this catalogue has already applied, or a refusal.

    "How much" is deliberately a prefix length rather than a set of versions. A
    ledger that skipped a version, reordered two, recorded a name or checksum that
    is not the pinned one, or recorded a version this build does not have at all is
    not a catalogue that is merely behind -- it is one whose history this build
    cannot account for, and the only safe answer is to refuse rather than to work
    out which of its rows to trust.

    `user_version` is checked against the same prefix, because the two are
    independent statements of the same fact and a disagreement means one of them
    was written by something other than this migrator.
    """
    ledger = connection.execute(
        "SELECT version, name, checksum FROM omnivia_installation_schema_migrations "
        "ORDER BY version"
    ).fetchall()
    if len(ledger) > len(migrations):
        raise InstallationMigrationError(
            "installation migration ledger records more migrations than this build "
            f"pins (pinned={len(migrations)}, recorded={len(ledger)})"
        )
    expected = [
        (migration.version, migration.name, migration.checksum)
        for migration in migrations[: len(ledger)]
    ]
    if [tuple(row) for row in ledger] != expected:
        raise InstallationMigrationError(
            "installation migration ledger differs from pinned artifacts"
        )

    user_version = connection.execute("PRAGMA user_version").fetchone()
    actual_version = 0 if user_version is None else int(user_version[0])
    if actual_version != len(ledger):
        raise InstallationMigrationError(
            "installation schema version disagrees with its migration ledger "
            f"(ledger={len(ledger)}, user_version={actual_version})"
        )
    return len(ledger)


def apply_pending_installation_migrations(
    connection: sqlite3.Connection,
    *,
    installation_id: str,
    owner_instance_id: str,
    fencing_generation: int,
    now_us: int,
) -> tuple[InstallationMigration, ...]:
    """Advance an existing catalogue to the pinned head, or refuse to touch it.

    One transaction for the whole remaining chain, not one per migration. The
    installation catalogue is a single small file opened by a single owner, so
    there is no partial-progress state worth preserving and every reason to make
    the advance atomic: a crash leaves a catalogue at the version it was already
    serving, with its ledger, its `user_version` and its schema still agreeing.

    Nothing is applied until the catalogue proves it is exactly the article the
    recorded prefix describes -- ledger, `user_version` and schema fingerprint all
    checked first, so a hand-applied or half-applied migration is refused rather
    than absorbed by the `IF NOT EXISTS` in every artifact. Each applied artifact
    records its own ledger row under the caller's current owner and fencing
    generation, which the schema's own INSERT trigger then re-proves against the
    state row inside this same transaction.
    """
    migrations = load_installation_migrations()
    applied = _applied_installation_prefix(connection, migrations)

    actual_fingerprint = fingerprint_schema(connection)
    expected_fingerprint = canonical_installation_schema_fingerprint(applied)
    if not actual_fingerprint.matches(expected_fingerprint):
        raise InstallationMigrationError(
            "installation schema does not match the migrations it records as applied "
            f"(expected={expected_fingerprint.digest}, "
            f"actual={actual_fingerprint.digest})"
        )

    pending = migrations[applied:]
    if not pending:
        return ()

    connection.execute("BEGIN IMMEDIATE")
    try:
        for migration in pending:
            execute_script(connection, migration.sql)
            connection.execute(
                "INSERT INTO omnivia_installation_schema_migrations "
                "(version, name, checksum, installation_id, fencing_generation, "
                "applied_by_owner, applied_at_us) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    migration.version,
                    migration.name,
                    migration.checksum,
                    installation_id,
                    fencing_generation,
                    owner_instance_id,
                    now_us,
                ),
            )
        connection.execute(f"PRAGMA user_version = {len(migrations)}")
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    return pending


def verify_installation_schema(connection: sqlite3.Connection) -> None:
    """Refuse drift in migration history, schema, integrity or foreign keys."""
    migrations = load_installation_migrations()
    expected_fingerprint = canonical_installation_schema_fingerprint()
    actual_fingerprint = fingerprint_schema(connection)
    if not actual_fingerprint.matches(expected_fingerprint):
        raise InstallationMigrationError(
            "installation schema fingerprint mismatch "
            f"(expected={expected_fingerprint.digest}, "
            f"actual={actual_fingerprint.digest})"
        )

    ledger = connection.execute(
        "SELECT version, name, checksum FROM omnivia_installation_schema_migrations "
        "ORDER BY version"
    ).fetchall()
    expected_ledger = [
        (migration.version, migration.name, migration.checksum)
        for migration in migrations
    ]
    if ledger != expected_ledger:
        raise InstallationMigrationError(
            "installation migration ledger differs from pinned artifacts"
        )

    user_version = connection.execute("PRAGMA user_version").fetchone()
    actual_version = 0 if user_version is None else int(user_version[0])
    if actual_version != len(migrations):
        raise InstallationMigrationError(
            "installation schema version mismatch "
            f"(expected={len(migrations)}, actual={actual_version})"
        )

    integrity_problems = integrity_check(connection)
    if integrity_problems:
        raise InstallationMigrationError(
            "installation catalogue integrity check failed: "
            + "; ".join(integrity_problems)
        )
    foreign_key_problems = foreign_key_check(connection)
    if foreign_key_problems:
        raise InstallationMigrationError(
            "installation catalogue foreign-key check failed: "
            + "; ".join(foreign_key_problems)
        )


__all__ = [
    "INSTALLATION_FORMAT_VERSION",
    "INSTALLATION_MIGRATION_PACKAGE",
    "INSTALLATION_WRITER_FUNCTION",
    "PINNED_INSTALLATION_MIGRATIONS",
    "InstallationMigration",
    "InstallationMigrationError",
    "apply_initial_installation_schema",
    "apply_pending_installation_migrations",
    "canonical_installation_schema_fingerprint",
    "installation_schema_present",
    "load_installation_migrations",
    "verify_installation_schema",
]
