"""Gate B installation migration 0002: fresh head, safe upgrade, refused drift."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from omnivia_core_runtime.storage import installation_migrations
from omnivia_core_runtime.storage.installation_migrations import (
    PINNED_INSTALLATION_MIGRATIONS,
    InstallationMigrationError,
    canonical_installation_schema_fingerprint,
    load_installation_migrations,
)
from omnivia_core_runtime.storage.installation_store import (
    InstallationStore,
    open_installation_store,
)

HEAD_VERSION = 2
MCP_TABLES = ("omnivia_installation_mcp_setups", "omnivia_installation_mcp_grants")


class TickClock:
    def __init__(self, start: int = 1_800_000_000_000_000) -> None:
        self.value = start

    def __call__(self) -> int:
        self.value += 1
        return self.value


def catalogue(root: Path) -> Path:
    return root / "catalogue" / "installation.sqlite"


@contextmanager
def raw(path: Path) -> Iterator[sqlite3.Connection]:
    """A plain client connection, exactly as an attacker or an operator has one."""
    connection = sqlite3.connect(path)
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def open_store(root: Path, owner: str) -> InstallationStore:
    return open_installation_store(
        root,
        owner_instance_id=owner,
        clock_us=TickClock(),
        installation_id_factory=lambda: "inst-mcp-migration",
    )


def materialise_version_one(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """A clean catalogue at the pre-Gate-B head, as an installed Core left it."""
    head = load_installation_migrations()
    monkeypatch.setattr(
        installation_migrations, "load_installation_migrations", lambda: head[:1]
    )
    open_store(root, "installer-v1").close()
    monkeypatch.undo()


def test_pinned_installation_chain_names_the_gate_b_migration() -> None:
    migrations = load_installation_migrations()
    assert [migration.name for migration in migrations] == [
        "0001_installation_authority.sql",
        "0002_mcp_principals_and_authoring_intent.sql",
    ]
    for migration in migrations:
        assert PINNED_INSTALLATION_MIGRATIONS[migration.name] == migration.checksum


def test_fresh_catalogue_materialises_the_whole_pinned_chain(tmp_path: Path) -> None:
    root = (tmp_path / "installation").resolve()
    store = open_store(root, "installation-service")
    store.close()

    with raw(catalogue(root)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == HEAD_VERSION
        ledger = connection.execute(
            "SELECT version, name, checksum, fencing_generation "
            "FROM omnivia_installation_schema_migrations ORDER BY version"
        ).fetchall()
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert [(row[0], row[1], row[2]) for row in ledger] == [
        (migration.version, migration.name, migration.checksum)
        for migration in load_installation_migrations()
    ]
    # Generation one materialises the head, so every ledger row belongs to the
    # generation that created the installation rather than to a later migration.
    assert {int(row[3]) for row in ledger} == {1}
    assert set(MCP_TABLES) <= tables


def test_clean_version_one_catalogue_upgrades_to_the_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = (tmp_path / "installation").resolve()
    materialise_version_one(monkeypatch, root)

    with raw(catalogue(root)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1

    store = open_store(root, "installation-service")
    try:
        # The upgraded catalogue is usable through the ordinary typed surface, not
        # merely present: an upgrade that left the tables unreadable would pass a
        # schema check and fail the first configure.
        assert store.mcp_setups() == ()
        assert store.authority.fencing_generation == 2
    finally:
        store.close()

    with raw(catalogue(root)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == HEAD_VERSION
        ledger = connection.execute(
            "SELECT version, fencing_generation, applied_by_owner "
            "FROM omnivia_installation_schema_migrations ORDER BY version"
        ).fetchall()
    # The new row is recorded under the owner and generation that applied it, which
    # is what the schema's own INSERT trigger proved against the state row.
    assert [tuple(row) for row in ledger] == [
        (1, 1, "installer-v1"),
        (2, 2, "installation-service"),
    ]


def test_upgrade_is_idempotent_across_reopens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = (tmp_path / "installation").resolve()
    materialise_version_one(monkeypatch, root)
    open_store(root, "owner-a").close()
    open_store(root, "owner-b").close()

    with raw(catalogue(root)) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM omnivia_installation_schema_migrations"
            ).fetchone()[0]
            == HEAD_VERSION
        )


def refuses(root: Path, owner: str = "installation-service") -> str:
    with pytest.raises(InstallationMigrationError) as refusal:
        open_store(root, owner).close()
    return str(refusal.value)


@pytest.mark.parametrize(
    ("name", "tamper"),
    [
        (
            "partially applied 0002",
            lambda connection: connection.executescript(
                "CREATE TABLE omnivia_installation_mcp_grants (x INTEGER);"
            ),
        ),
        (
            "manually applied 0002 without its ledger row",
            lambda connection: connection.executescript(
                Path(installation_migrations.__file__)
                .parent.joinpath(
                    "installation_migration_files",
                    "0002_mcp_principals_and_authoring_intent.sql",
                )
                .read_text(encoding="utf-8")
            ),
        ),
        (
            "unexpected user_version",
            lambda connection: connection.execute("PRAGMA user_version = 9"),
        ),
        (
            "rewritten ledger checksum",
            lambda connection: connection.executescript(
                "DROP TRIGGER omnivia_guard_installation_schema_migrations_update;"
                "UPDATE omnivia_installation_schema_migrations SET checksum = "
                f"'{'0' * 64}' WHERE version = 1;"
            ),
        ),
        (
            "emptied ledger",
            lambda connection: connection.executescript(
                "DROP TRIGGER omnivia_guard_installation_schema_migrations_delete;"
                "DELETE FROM omnivia_installation_schema_migrations;"
            ),
        ),
    ],
)
def test_drifted_version_one_catalogue_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    tamper: Callable[[sqlite3.Connection], object],
) -> None:
    root = (tmp_path / "installation").resolve()
    materialise_version_one(monkeypatch, root)
    with raw(catalogue(root)) as connection:
        tamper(connection)

    message = refuses(root)
    assert message, name
    # Nothing was applied on the way to refusing: a catalogue this build will not
    # migrate is also a catalogue it will not half-migrate.
    with raw(catalogue(root)) as connection:
        applied = connection.execute(
            "SELECT COUNT(*) FROM omnivia_installation_schema_migrations"
        ).fetchone()[0]
    assert applied <= 1


def test_ledger_ahead_of_this_build_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A catalogue from a newer Core is not silently downgraded or re-migrated."""
    root = (tmp_path / "installation").resolve()
    open_store(root, "installation-service").close()
    with raw(catalogue(root)) as connection:
        # The guard trigger already refuses this insert to a stock client, which is
        # the schema doing its job; dropped here so the *migrator's* own refusal is
        # what this test is about.
        connection.execute(
            "DROP TRIGGER omnivia_guard_installation_schema_migrations_insert"
        )
        connection.execute(
            "INSERT INTO omnivia_installation_schema_migrations "
            "(version, name, checksum, installation_id, fencing_generation, "
            "applied_by_owner, applied_at_us) "
            "VALUES (3, '0003_future.sql', ?, 'inst-mcp-migration', 1, 'future', 1)",
            ("0" * 64,),
        )
        connection.execute("PRAGMA user_version = 3")

    assert "more migrations than this build" in refuses(root)


def test_canonical_prefix_fingerprint_is_bounded_by_the_pinned_chain() -> None:
    head = canonical_installation_schema_fingerprint()
    assert canonical_installation_schema_fingerprint(HEAD_VERSION) == head
    assert canonical_installation_schema_fingerprint(1) != head
    assert canonical_installation_schema_fingerprint(0).tables == 0
    with pytest.raises(InstallationMigrationError):
        canonical_installation_schema_fingerprint(HEAD_VERSION + 1)
