"""Gate B installation migrations 0002-0003: fresh head, safe upgrade, refused drift."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import test_installed_mcp_authority as authority
from omnivia_core_runtime.service.authorization import AuthenticatedSession
from omnivia_core_runtime.service.installed_mcp import InstalledMcpAuthority
from omnivia_core_runtime.service.mutation import INSTALLATION_ADMINISTRATOR_ROLE
from omnivia_core_runtime.storage import installation_migrations
from omnivia_core_runtime.storage.installation_migrations import (
    PINNED_INSTALLATION_MIGRATIONS,
    InstallationMigrationError,
    canonical_installation_schema_fingerprint,
    load_installation_migrations,
)
from omnivia_core_runtime.storage.installation_store import (
    InstallationStore,
    InstallationStoreError,
    McpGrant,
    McpGrantKind,
    McpHost,
    McpProfile,
    open_installation_store,
)

HEAD_VERSION = 3
MCP_TABLES = ("omnivia_installation_mcp_setups", "omnivia_installation_mcp_grants")

#: The grant kinds `0003` admits, which is `0002`'s four plus the one role kind
#: R004 section 9.1's workspace-contributor authority needs a row to live in.
GRANT_KINDS = ("operation", "scope", "purpose", "capability", "role")

INSTALLATION_ID = "inst-mcp-migration"

#: This file's catalogues are minted under their own installation id, so the
#: administration session has to name that one rather than the neighbouring
#: module's. The role is the same one `configure` has always required.
ADMINISTRATOR = AuthenticatedSession(
    principal_id="local-owner",
    roles=frozenset({INSTALLATION_ADMINISTRATOR_ROLE}),
    installations=frozenset({INSTALLATION_ID}),
)


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
        installation_id_factory=lambda: INSTALLATION_ID,
    )


def materialise_version(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    version: int,
    owner: str,
    seed: Callable[[InstallationStore], object] | None = None,
) -> None:
    """A catalogue at an earlier head, as that Core build left it.

    `seed` runs against the store while the chain is still pinned short, which is
    the only way to produce rows an older schema could hold and a newer one has to
    carry across. Reopening afterwards would already have migrated it.
    """
    head = load_installation_migrations()
    monkeypatch.setattr(
        installation_migrations, "load_installation_migrations", lambda: head[:version]
    )
    store = open_store(root, owner)
    try:
        if seed is not None:
            seed(store)
    finally:
        store.close()
    monkeypatch.undo()


def materialise_version_one(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """A clean catalogue at the pre-Gate-B head, as an installed Core left it."""
    materialise_version(monkeypatch, root, 1, "installer-v1")


def test_pinned_installation_chain_names_the_gate_b_migration() -> None:
    migrations = load_installation_migrations()
    assert [migration.name for migration in migrations] == [
        "0001_installation_authority.sql",
        "0002_mcp_principals_and_authoring_intent.sql",
        "0003_mcp_role_grants.sql",
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
        (3, 2, "installation-service"),
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
            "VALUES (4, '0004_future.sql', ?, 'inst-mcp-migration', 1, 'future', 1)",
            ("0" * 64,),
        )
        connection.execute(f"PRAGMA user_version = {HEAD_VERSION + 1}")

    assert "more migrations than this build" in refuses(root)


# --- 0003: the role grant kind, added without losing a row --------------------


def grant_rows(path: Path) -> list[tuple[object, ...]]:
    with raw(path) as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT grant_row_id, installation_id, setup_id, setup_generation, "
                "grant_kind, grant_value, grant_version, fencing_generation, "
                "granted_at_us FROM omnivia_installation_mcp_grants "
                "ORDER BY grant_row_id"
            ).fetchall()
        ]


def configure_restricted(store: InstallationStore) -> None:
    """One real restricted setup, written through the ordinary typed surface."""
    workspace_id = authority.register_workspace(store, "one")
    InstalledMcpAuthority(store).configure(
        ADMINISTRATOR,
        host=McpHost.CLAUDE_CODE,
        workspace_id=workspace_id,
        profile=McpProfile.RESTRICTED,
        authoring_intent=False,
    )


def version_two_with_grants(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """A version-two installation holding the pre-decision restricted policy.

    The restricted profile now carries `decision.evaluate`, whose mutation the
    coordinator serves under a role -- a right the version-two schema cannot
    record (that is what 0003 exists to fix). The fixture therefore pins the
    policy to the pre-decision six-read derivation, which is what a version-two
    installation actually held, and leaves the current policy to the tests
    below that run against the upgraded schema.
    """
    import omnivia_core_runtime.service.installed_mcp as installed_mcp

    pre_decision = installed_mcp._derive_policy(
        tuple(
            entry
            for entry in installed_mcp._RESTRICTED_OPERATIONS
            if not entry[0].startswith("decision.")
        )
    )
    monkeypatch.setattr(installed_mcp, "RESTRICTED_POLICY", pre_decision)
    monkeypatch.setitem(
        installed_mcp._POLICIES, McpProfile.RESTRICTED, pre_decision
    )
    materialise_version(monkeypatch, root, 2, "installer-v2", configure_restricted)


def test_the_version_two_schema_has_no_row_a_role_grant_could_live_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why 0003 exists: 0002's CHECK refuses the kind section 9.1 requires."""
    root = (tmp_path / "installation").resolve()
    version_two_with_grants(monkeypatch, root)

    with raw(catalogue(root)) as connection:
        connection.execute("DROP TRIGGER omnivia_guard_installation_mcp_grants_insert")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO omnivia_installation_mcp_grants "
                "(grant_row_id, installation_id, setup_id, setup_generation, "
                "grant_kind, grant_value, grant_version, fencing_generation, "
                "granted_at_us) VALUES ('g-role', ?, 'mcp-setup-x', 1, 'role', "
                "'workspace_contributor', NULL, 1, 1)",
                ("inst-mcp-migration",),
            )


def test_0003_carries_every_version_two_grant_row_across_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The grant table is evidence of what was granted; an upgrade loses none of it."""
    root = (tmp_path / "installation").resolve()
    version_two_with_grants(monkeypatch, root)
    before = grant_rows(catalogue(root))
    assert before, "the fixture must leave real grant rows to carry across"

    open_store(root, "installation-service").close()

    assert grant_rows(catalogue(root)) == before
    with raw(catalogue(root)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == HEAD_VERSION


def test_the_upgraded_grant_table_admits_a_role_and_still_refuses_a_wildcard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one line 0003 changed, and every line it did not.

    Exercised through the typed surface rather than by hand: `configure` is the
    only path that writes grants, so a reconfigure to `authoring` proves the
    widened CHECK, the recreated guards and the recreated indexes all still work
    together. The wildcard refusal beside it proves the rebuild kept the rest of
    `0002`'s constraints rather than relaxing them along with the one.
    """
    root = (tmp_path / "installation").resolve()
    version_two_with_grants(monkeypatch, root)

    store = open_store(root, "installation-service")
    try:
        mcp = InstalledMcpAuthority(store)
        provisioned = mcp.configure(
            ADMINISTRATOR,
            host=McpHost.CLAUDE_CODE,
            workspace_id="ws-one",
            profile=McpProfile.AUTHORING,
            authoring_intent=True,
        )
        setup = provisioned.setup
        stored = store.mcp_grants(setup.setup_id, setup.setup_generation)
        assert McpGrant(McpGrantKind.ROLE, "workspace_contributor") in stored
        assert {grant.kind.value for grant in stored} <= set(GRANT_KINDS)
        # The rebuilt table is still append-only and still exact.
        with pytest.raises(InstallationStoreError):
            store.configure_mcp_setup(
                store.authority,
                host=McpHost.CODEX,
                workspace_id="ws-one",
                profile=McpProfile.RESTRICTED,
                authoring_intent=False,
                grants=(McpGrant(McpGrantKind.ROLE, "workspace_*"),),
                identity_factory=lambda: pytest.fail("nothing should be minted"),
            )
    finally:
        store.close()


def test_canonical_prefix_fingerprint_is_bounded_by_the_pinned_chain() -> None:
    head = canonical_installation_schema_fingerprint()
    assert canonical_installation_schema_fingerprint(HEAD_VERSION) == head
    assert canonical_installation_schema_fingerprint(1) != head
    assert canonical_installation_schema_fingerprint(0).tables == 0
    with pytest.raises(InstallationMigrationError):
        canonical_installation_schema_fingerprint(HEAD_VERSION + 1)
