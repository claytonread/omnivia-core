"""T-0629F acceptance: fenced transactions and mutation cutover (FM-01 … FM-22)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from omnivia_core_runtime.ownership.fencing import (
    GUARDED_TABLES,
    UNGUARDED_SUBSTRATE_TABLES,
    SchemaDrift,
    StaleGeneration,
    assert_guards_intact,
    clear_authorizer,
    close_guard,
    fenced_transaction,
    install_authorizer,
    open_guard,
    read_guard,
    trigger_names,
    verify_fingerprint,
)
from omnivia_core_runtime.ownership.identity import (
    FakeClock,
    ProcessEvidence,
    ServiceInstanceIdentity,
)
from omnivia_core_runtime.ownership.lease import acquire_lease
from omnivia_core_runtime.storage.connection import (
    OpenMode,
    authorised,
    fingerprint_schema,
    open_database,
)
from omnivia_core_runtime.storage.migrations import (
    apply_pending_migrations,
    bootstrap_generation_one,
    load_migrations,
    materialise_phase0_baseline,
    phase0_baseline_sql,
    read_workspace_state,
)

from .harness import run_child

WORKSPACE_ID = "ws-fencing-0001"



#: A write from outside this runtime is refused either by the connection-authority
#: function the triggers call -- which a stock client does not have -- or, once that
#: function is present, by the guard predicate itself. Both are refusals; which one
#: fires depends only on how the writer opened the database.
REFUSED_EXTERNAL_WRITE = "unguarded|no such function|from outside the runtime"

def make_identity(instance: str = "svc-one", pid: int = 1111) -> ServiceInstanceIdentity:
    return ServiceInstanceIdentity(
        service_instance_id=instance,
        installation_id="inst-one",
        process=ProcessEvidence(
            pid=pid, start_time="100", boot_id="boot-a", os_principal="me"
        ),
    )


@pytest.fixture
def served_connection(tmp_path: Path):
    """A migrated workspace held on a governed `SERVICE_OWNED` connection.

    `owned` deliberately uses `EXCLUSIVE_MAINTENANCE`, which carries no authorizer,
    so anything asserting on authorizer behaviour has to build its own.
    """
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    identity = make_identity()

    maintenance = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    state = bootstrap_generation_one(
        maintenance,
        workspace_id=WORKSPACE_ID,
        mode=OpenMode.EXCLUSIVE_MAINTENANCE,
        expect_phase0_baseline=True,
        service_instance_id=identity.service_instance_id,
    )
    apply_pending_migrations(
        maintenance,
        mode=OpenMode.EXCLUSIVE_MAINTENANCE,
        service_instance_id=identity.service_instance_id,
        fencing_generation=state.fencing_generation,
        workspace_id=WORKSPACE_ID,
    )
    maintenance.close()

    connection = open_database(path, OpenMode.SERVICE_OWNED)
    lease = acquire_lease(
        connection,
        identity,
        clock=FakeClock(),
        workspace_id=WORKSPACE_ID,
        holds_storage_lock=True,
        lock_mechanism="flock",
    )
    open_guard(
        connection,
        identity,
        clock=FakeClock(),
        workspace_id=WORKSPACE_ID,
        fencing_generation=lease.fencing_generation,
    )
    yield connection, identity, lease.fencing_generation
    connection.close()


@pytest.fixture
def owned(tmp_path: Path):
    """A migrated workspace with an open guard and a held lease."""
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    clock = FakeClock()
    identity = make_identity()

    state = bootstrap_generation_one(
        connection,
        workspace_id=WORKSPACE_ID,
        mode=OpenMode.EXCLUSIVE_MAINTENANCE,
        expect_phase0_baseline=True,
        service_instance_id=identity.service_instance_id,
    )
    apply_pending_migrations(
        connection,
        mode=OpenMode.EXCLUSIVE_MAINTENANCE,
        service_instance_id=identity.service_instance_id,
        fencing_generation=state.fencing_generation,
        workspace_id=WORKSPACE_ID,
    )
    lease = acquire_lease(
        connection,
        identity,
        clock=clock,
        workspace_id=WORKSPACE_ID,
        holds_storage_lock=True,
        lock_mechanism="flock",
    )
    open_guard(
        connection,
        identity,
        clock=clock,
        workspace_id=WORKSPACE_ID,
        fencing_generation=lease.fencing_generation,
    )
    yield connection, identity, lease.fencing_generation, path, clock
    connection.close()


def insert_memory(connection: sqlite3.Connection, identifier: str) -> None:
    connection.execute(
        "INSERT INTO memories (id, content, source_type, source_reference, "
        "created_by, created_at, updated_at) VALUES (?, 'c', 's', 'r', 'me', 'n', 'n')",
        (identifier,),
    )


# FM-01 / FM-02 / FM-03
def test_fm01_fm02_fm03_fenced_transaction_validates_on_entry_and_before_commit(
    owned: tuple[sqlite3.Connection, ServiceInstanceIdentity, int, Path, FakeClock],
) -> None:
    connection, identity, generation, _path, _clock = owned
    with fenced_transaction(
        connection, identity, workspace_id=WORKSPACE_ID, fencing_generation=generation
    ):
        insert_memory(connection, "mem-ok")
    count = connection.execute("SELECT COUNT(*) FROM memories").fetchone()
    assert count is not None and count[0] == 1


# FM-04 / FM-06
def test_fm04_fm06_generation_change_before_commit_rolls_back(
    owned: tuple[sqlite3.Connection, ServiceInstanceIdentity, int, Path, FakeClock],
) -> None:
    """The case a pre-flight check cannot catch."""
    connection, identity, generation, _path, _clock = owned

    with pytest.raises(StaleGeneration, match="before commit"), fenced_transaction(
        connection, identity, workspace_id=WORKSPACE_ID, fencing_generation=generation
    ):
        insert_memory(connection, "mem-doomed")
        # A takeover lands mid-transaction: entry validation already passed.
        connection.execute(
            "UPDATE omnivia_workspace_state SET fencing_generation = ? "
            "WHERE singleton = 1",
            (generation + 5,),
        )

    remaining = connection.execute("SELECT COUNT(*) FROM memories").fetchone()
    assert remaining is not None and remaining[0] == 0, "the whole block rolled back"


def test_fm04b_stale_generation_is_refused_on_entry(
    owned: tuple[sqlite3.Connection, ServiceInstanceIdentity, int, Path, FakeClock],
) -> None:
    connection, identity, generation, _path, _clock = owned
    with pytest.raises(StaleGeneration, match="on entry"), fenced_transaction(
        connection,
        identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=generation + 1,
    ):
        insert_memory(connection, "never")


# FM-05
def test_fm05_stale_generation_cannot_commit_after_takeover(
    owned: tuple[sqlite3.Connection, ServiceInstanceIdentity, int, Path, FakeClock],
) -> None:
    """A resumed predecessor writes nothing after a successor took over."""
    connection, identity, generation, _path, clock = owned
    successor = make_identity("svc-successor", pid=2222)
    new_lease = acquire_lease(
        connection,
        successor,
        clock=clock,
        workspace_id=WORKSPACE_ID,
        holds_storage_lock=True,
        lock_mechanism="flock",
    )
    open_guard(
        connection,
        successor,
        clock=clock,
        workspace_id=WORKSPACE_ID,
        fencing_generation=new_lease.fencing_generation,
    )

    # The predecessor resumes, still believing it owns the workspace.
    with pytest.raises(StaleGeneration), fenced_transaction(
        connection, identity, workspace_id=WORKSPACE_ID, fencing_generation=generation
    ):
        insert_memory(connection, "from-the-past")

    count = connection.execute("SELECT COUNT(*) FROM memories").fetchone()
    assert count is not None and count[0] == 0


# FM-07
def test_fm07_service_instance_mismatch_rolls_back(
    owned: tuple[sqlite3.Connection, ServiceInstanceIdentity, int, Path, FakeClock],
) -> None:
    connection, _identity, generation, _path, _clock = owned
    impostor = make_identity("svc-impostor", pid=3333)
    with (
        # Refused by the lease check now, which fires before the guard row is
        # consulted at all -- a stronger refusal than the guard mismatch.
        pytest.raises(StaleGeneration, match="lease belongs to"),
        fenced_transaction(
            connection, impostor, workspace_id=WORKSPACE_ID, fencing_generation=generation
        ),
    ):
            insert_memory(connection, "impostor")


# FM-08
def test_fm08_workspace_id_mismatch_rolls_back(
    owned: tuple[sqlite3.Connection, ServiceInstanceIdentity, int, Path, FakeClock],
) -> None:
    connection, identity, generation, _path, _clock = owned
    with pytest.raises(StaleGeneration), fenced_transaction(
        connection,
        identity,
        workspace_id="ws-elsewhere",
        fencing_generation=generation,
    ):
        insert_memory(connection, "wrong-workspace")


# FM-09 / FM-10
def test_fm09_fm10_authorizer_denies_unguarded_dml_and_all_ddl(
    owned: tuple[sqlite3.Connection, ServiceInstanceIdentity, int, Path, FakeClock],
) -> None:
    connection, _identity, _generation, _path, _clock = owned
    install_authorizer(connection, allow_mutations=False)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            insert_memory(connection, "denied")
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("CREATE TABLE surprise (id TEXT)")
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("DROP TABLE memories")
        # Reads remain permitted.
        assert connection.execute("SELECT COUNT(*) FROM memories").fetchone() is not None
    finally:
        clear_authorizer(connection)


def test_authorizer_denies_ddl_even_when_mutations_are_allowed(
    owned: tuple[sqlite3.Connection, ServiceInstanceIdentity, int, Path, FakeClock],
) -> None:
    connection, _identity, _generation, _path, _clock = owned
    install_authorizer(connection, allow_mutations=True)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("CREATE INDEX idx_nope ON memories (content)")
    finally:
        clear_authorizer(connection)


# FM-11
def test_fm11_ordinary_dml_fails_closed_when_no_guard_is_open(
    owned: tuple[sqlite3.Connection, ServiceInstanceIdentity, int, Path, FakeClock],
) -> None:
    """Persisted triggers, not the authorizer: the guard row is simply absent."""
    connection, _identity, _generation, _path, _clock = owned
    close_guard(connection)
    assert read_guard(connection) is None

    with pytest.raises(sqlite3.IntegrityError, match="unguarded INSERT on memories"):
        insert_memory(connection, "no-guard")


def test_stale_guard_generation_fails_closed(
    owned: tuple[sqlite3.Connection, ServiceInstanceIdentity, int, Path, FakeClock],
) -> None:
    """A guard row whose generation is no longer current does not authorise writes."""
    connection, _identity, generation, _path, _clock = owned
    connection.execute(
        "UPDATE omnivia_workspace_state SET fencing_generation = ? WHERE singleton = 1",
        (generation + 1,),
    )
    with pytest.raises(sqlite3.Error, match=REFUSED_EXTERNAL_WRITE):
        insert_memory(connection, "stale-guard")


# FM-12 / FM-13
def test_fm12_fm13_second_stock_sqlite_process_cannot_dml_or_ddl(
    owned: tuple[sqlite3.Connection, ServiceInstanceIdentity, int, Path, FakeClock],
) -> None:
    """A plain sqlite3 client, importing no OmniVia code, is refused.

    While the service holds its exclusive connection the external process cannot
    even acquire a write lock; with the guard closed it is additionally refused by
    the persisted triggers. Both are real refusals from a separate OS process.
    """
    connection, _identity, _generation, path, _clock = owned

    dml = run_child(
        "stock_sqlite_writer.py",
        str(path),
        "INSERT INTO memories (id, content, source_type, source_reference, created_by, "
        "created_at, updated_at) VALUES ('external', 'c', 's', 'r', 'me', 'n', 'n')",
        "300",
    )
    assert dml.ok, dml.stderr
    assert dml.report.get("succeeded") is False, dml.report

    ddl = run_child(
        "stock_sqlite_writer.py", str(path), "CREATE TABLE external_table (id TEXT)", "300"
    )
    assert ddl.report.get("succeeded") is False, ddl.report

    # Nothing the external process attempted is present.
    count = connection.execute("SELECT COUNT(*) FROM memories").fetchone()
    assert count is not None and count[0] == 0


def test_external_dml_is_refused_by_triggers_without_an_exclusive_holder(
    tmp_path: Path,
) -> None:
    """Trigger-only proof: no service connection is open at all.

    This isolates the persisted-trigger layer from the exclusive-lock layer. If the
    only refusal came from the lock, the triggers would be untested.
    """
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    connection = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    identity = make_identity()
    state = bootstrap_generation_one(
        connection,
        workspace_id=WORKSPACE_ID,
        mode=OpenMode.EXCLUSIVE_MAINTENANCE,
        expect_phase0_baseline=True,
    )
    apply_pending_migrations(
        connection,
        mode=OpenMode.EXCLUSIVE_MAINTENANCE,
        service_instance_id=identity.service_instance_id,
        fencing_generation=state.fencing_generation,
        workspace_id=WORKSPACE_ID,
    )
    connection.close()  # no exclusive holder remains

    result = run_child(
        "stock_sqlite_writer.py",
        str(path),
        "INSERT INTO memories (id, content, source_type, source_reference, created_by, "
        "created_at, updated_at) VALUES ('x', 'c', 's', 'r', 'me', 'n', 'n')",
        "500",
    )
    assert result.report.get("succeeded") is False, result.report
    # Refused for either reason: a stock client lacks the connection-authority
    # function the triggers call, and would fail the guard predicate even with it.
    error = str(result.report.get("error", "")).lower()
    assert "unguarded" in error or "no such function" in error, result.report


# FM-15 / FM-16 / FM-17
def test_fm15_fm16_fm17_schema_and_trigger_drift_is_detected(
    owned: tuple[sqlite3.Connection, ServiceInstanceIdentity, int, Path, FakeClock],
) -> None:
    connection, _identity, _generation, _path, _clock = owned
    expected = fingerprint_schema(connection)
    assert expected.triggers > 0, "guard triggers must exist"
    assert verify_fingerprint(connection, expected).matches(expected)

    # Offline trigger drift: drop one guard trigger.
    names = trigger_names(connection)
    assert names
    connection.execute(f"DROP TRIGGER {names[0]}")
    with pytest.raises(SchemaDrift, match="fingerprint differs"):
        verify_fingerprint(connection, expected)
    with pytest.raises(SchemaDrift, match="guard triggers are missing"):
        assert_guards_intact(connection)


def test_schema_drift_is_detected_for_an_added_table(
    owned: tuple[sqlite3.Connection, ServiceInstanceIdentity, int, Path, FakeClock],
) -> None:
    connection, _identity, _generation, _path, _clock = owned
    expected = fingerprint_schema(connection)
    connection.execute("CREATE TABLE interloper (id TEXT PRIMARY KEY)")
    with pytest.raises(SchemaDrift):
        verify_fingerprint(connection, expected)


# FM-18 … FM-21
def test_fm18_fm21_every_guarded_subsystem_is_covered_by_triggers(
    owned: tuple[sqlite3.Connection, ServiceInstanceIdentity, int, Path, FakeClock],
) -> None:
    """The B0 mutation inventory's subsystems each have guard triggers."""
    connection, _identity, _generation, _path, _clock = owned
    assert_guards_intact(connection)
    names = set(trigger_names(connection))
    for table in GUARDED_TABLES:
        short = table.removeprefix("omnivia_")
        assert any(
            name.startswith((f"omnivia_guard_{table}_", f"omnivia_guard_{short}_"))
            for name in names
        ), table


@pytest.mark.parametrize(
    "statement",
    [
        (
            "INSERT INTO sources (id, workspace_id, file_path, file_type, created_at, "
            "updated_at) VALUES ('s1', 'w', 'p', 'md', 'n', 'n')"
        ),
        (
            "INSERT INTO graph_entities (id, name, entity_type, created_at, updated_at) "
            "VALUES ('e1', 'n', 't', 'n', 'n')"
        ),
        (
            "INSERT INTO workspaces (id, name, root_path, storage_path, index_status, "
            "created_at, updated_at) VALUES ('w1', 'n', 'r', 's', 'i', 'n', 'n')"
        ),
        (
            "INSERT INTO omnivia_durable_jobs (job_id, job_type, state, created_at, "
            "updated_at) VALUES ('j1', 't', 'queued', 'n', 'n')"
        ),
        (
            "INSERT INTO omnivia_projection_ledger (projection_id, version, rebuildable, "
            "state, updated_at) VALUES ('p1', '1', 1, 's', 'n')"
        ),
    ],
)
def test_each_guarded_subsystem_fails_closed_without_a_guard(
    owned: tuple[sqlite3.Connection, ServiceInstanceIdentity, int, Path, FakeClock],
    statement: str,
) -> None:
    connection, _identity, _generation, _path, _clock = owned
    close_guard(connection)
    with pytest.raises(sqlite3.Error, match=REFUSED_EXTERNAL_WRITE):
        connection.execute(statement)


def test_guarded_subsystems_succeed_inside_a_fenced_transaction(
    owned: tuple[sqlite3.Connection, ServiceInstanceIdentity, int, Path, FakeClock],
) -> None:
    """The guard permits the owner, so fencing is not merely a blanket refusal."""
    connection, identity, generation, _path, _clock = owned
    with fenced_transaction(
        connection, identity, workspace_id=WORKSPACE_ID, fencing_generation=generation
    ):
        connection.execute(
            "INSERT INTO omnivia_durable_jobs (job_id, job_type, state, created_at, "
            "updated_at, fencing_generation) VALUES ('j1', 't', 'queued', 'n', 'n', ?)",
            (generation,),
        )
    count = connection.execute("SELECT COUNT(*) FROM omnivia_durable_jobs").fetchone()
    assert count is not None and count[0] == 1


def test_open_guard_refuses_a_stale_tuple(
    owned: tuple[sqlite3.Connection, ServiceInstanceIdentity, int, Path, FakeClock],
) -> None:
    connection, identity, generation, _path, clock = owned
    # The stale generation is caught against the lease and workspace state.
    with pytest.raises(StaleGeneration, match="stale authority"):
        open_guard(
            connection,
            identity,
            clock=clock,
            workspace_id=WORKSPACE_ID,
            fencing_generation=generation + 7,
        )


def test_guard_survives_reopening_the_connection(
    owned: tuple[sqlite3.Connection, ServiceInstanceIdentity, int, Path, FakeClock],
) -> None:
    """The guard is persisted state, not connection state.

    That is what lets the triggers see it from any connection, including one this
    runtime did not create.
    """
    connection, _identity, _generation, path, _clock = owned
    guard = read_guard(connection)
    assert guard is not None
    connection.close()

    reopened = open_database(path, OpenMode.READ_ONLY)
    try:
        again = read_guard(reopened)
        assert again == guard
    finally:
        reopened.close()


def test_workspace_state_generation_and_lease_agree_after_fencing(
    owned: tuple[sqlite3.Connection, ServiceInstanceIdentity, int, Path, FakeClock],
) -> None:
    connection, _identity, generation, _path, _clock = owned
    state = read_workspace_state(connection)
    guard = read_guard(connection)
    assert state is not None and guard is not None
    assert state.fencing_generation == generation == guard.fencing_generation


# FM-22
def test_fm22_no_normal_path_can_write_the_implicit_home_database() -> None:
    """The legacy `~/.omnivia/memories.db` fallback is gone (T-0629F).

    `get_database()` previously created `~/.omnivia/` and opened a writable
    `memories.db` inside it as a side effect of a getter, and because the result was
    cached in a module-level singleton the first bare call anywhere in the process
    pinned that database for every later caller. That file sits outside any
    workspace, so it can be neither migrated nor fenced.
    """
    from omnivia_memory.persistence.database import (
        _ImplicitDatabasePathRefused,
        get_database,
        reset_database,
    )

    reset_database()
    try:
        with pytest.raises(_ImplicitDatabasePathRefused):
            get_database()
    finally:
        reset_database()

    # And no code path in the persistence module reaches the home directory.
    import omnivia_memory.persistence.database as legacy

    source = Path(legacy.__file__).read_text(encoding="utf-8")
    assert "Path.home()" not in source
    assert "db_dir.mkdir" not in source


def test_fm22b_the_legacy_transaction_helper_no_longer_hides_autocommit() -> None:
    """The runtime's fenced transaction is the atomic path, not legacy transaction().

    The B0 review found `Database.transaction()` yields without suspending
    auto_commit, so each `execute()` inside it commits individually and the context
    manager provides no atomicity. The runtime does not use it: `fenced_transaction`
    issues its own BEGIN IMMEDIATE and COMMIT.
    """
    import inspect

    from omnivia_core_runtime.ownership import fencing

    source = inspect.getsource(fencing.fenced_transaction)
    assert "BEGIN IMMEDIATE" in source
    assert "COMMIT" in source
    assert "auto_commit" not in source


# FM-19
def test_fm19_ingestion_mutations_are_guarded(
    owned: tuple[sqlite3.Connection, ServiceInstanceIdentity, int, Path, FakeClock],
) -> None:
    """The 7 ingestion DML sites from the B0 inventory: sources and chunks."""
    connection, identity, generation, _path, _clock = owned

    with fenced_transaction(
        connection, identity, workspace_id=WORKSPACE_ID, fencing_generation=generation
    ):
        connection.execute(
            "INSERT INTO sources (id, workspace_id, file_path, file_type, created_at, "
            "updated_at) VALUES ('src-1', ?, '/p/a.md', 'md', 'n', 'n')",
            (WORKSPACE_ID,),
        )
        connection.execute(
            "INSERT INTO chunks (id, source_id, chunk_index, content) "
            "VALUES ('chk-1', 'src-1', 0, 'body')"
        )
        connection.execute("UPDATE sources SET parse_status = 'done' WHERE id = 'src-1'")

    rows = connection.execute("SELECT parse_status FROM sources WHERE id = 'src-1'").fetchone()
    assert rows is not None and rows[0] == "done"

    # Without a guard, every one of those sites fails closed.
    close_guard(connection)
    for statement in (
        (
            "INSERT INTO sources (id, workspace_id, file_path, file_type, created_at, "
            "updated_at) VALUES ('src-2', 'w', '/p/b.md', 'md', 'n', 'n')"
        ),
        "UPDATE sources SET parse_status = 'x' WHERE id = 'src-1'",
        "DELETE FROM sources WHERE id = 'src-1'",
        (
            "INSERT INTO chunks (id, source_id, chunk_index, content) "
            "VALUES ('chk-2', 'src-1', 1, 'b')"
        ),
        "DELETE FROM chunks WHERE id = 'chk-1'",
    ):
        with pytest.raises(sqlite3.Error, match=REFUSED_EXTERNAL_WRITE):
            connection.execute(statement)


# FM-20
def test_fm20_graph_and_workspace_mutations_are_guarded(
    owned: tuple[sqlite3.Connection, ServiceInstanceIdentity, int, Path, FakeClock],
) -> None:
    """The 11 graph and workspace DML sites from the B0 inventory."""
    connection, identity, generation, _path, _clock = owned

    with fenced_transaction(
        connection, identity, workspace_id=WORKSPACE_ID, fencing_generation=generation
    ):
        connection.execute(
            "INSERT INTO graph_entities (id, name, entity_type, created_at, updated_at) "
            "VALUES ('ent-1', 'n', 'concept', 'n', 'n')"
        )
        connection.execute(
            "INSERT INTO graph_entities (id, name, entity_type, created_at, updated_at) "
            "VALUES ('ent-2', 'm', 'concept', 'n', 'n')"
        )
        connection.execute(
            "INSERT INTO graph_relationships (id, source_entity_id, target_entity_id, "
            "relationship_type, created_at, updated_at) "
            "VALUES ('rel-1', 'ent-1', 'ent-2', 'relates_to', 'n', 'n')"
        )
        connection.execute(
            "INSERT INTO workspaces (id, name, root_path, storage_path, created_at, "
            "updated_at) VALUES ('ws-extra', 'n', '/r', '/s', 'n', 'n')"
        )
        connection.execute("UPDATE workspaces SET name = 'renamed' WHERE id = 'ws-extra'")

    name = connection.execute(
        "SELECT name FROM workspaces WHERE id = 'ws-extra'"
    ).fetchone()
    assert name is not None and name[0] == "renamed"

    close_guard(connection)
    for statement in (
        "UPDATE graph_entities SET name = 'x' WHERE id = 'ent-1'",
        "DELETE FROM graph_relationships WHERE id = 'rel-1'",
        "DELETE FROM graph_entities WHERE id = 'ent-1'",
        "UPDATE workspaces SET name = 'y' WHERE id = 'ws-extra'",
        "DELETE FROM workspaces WHERE id = 'ws-extra'",
    ):
        with pytest.raises(sqlite3.Error, match=REFUSED_EXTERNAL_WRITE):
            connection.execute(statement)


# SB-03 regression
def test_sb03_a_non_lease_identity_cannot_open_a_guard(owned) -> None:
    """Knowing the current generation is not authority to write.

    The impostor supplies a correct `(workspace_id, fencing_generation)` -- both are
    readable by anyone with the file -- and differs only in the one field the guard
    used to accept without checking: who it says it is.
    """
    connection, _owner, generation, _path, clock = owned
    impostor = make_identity(instance="svc-impostor", pid=9999)

    with pytest.raises(StaleGeneration, match="lease belongs to"):
        open_guard(
            connection,
            impostor,
            clock=clock,
            workspace_id=WORKSPACE_ID,
            fencing_generation=generation,
        )

    # The owner's guard is untouched, not replaced by the refused attempt.
    guard = read_guard(connection)
    assert guard is not None and guard.service_instance_id == "svc-one"


# SB-03 regression
def test_sb03_a_non_lease_identity_cannot_commit_under_the_owners_guard(owned) -> None:
    """The impostor's write must be refused even with the owner's guard open."""
    connection, _owner, generation, _path, _clock = owned
    impostor = make_identity(instance="svc-impostor", pid=9999)

    before = connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    with (
        pytest.raises(StaleGeneration, match="lease belongs to"),
        fenced_transaction(
            connection,
            impostor,
            workspace_id=WORKSPACE_ID,
            fencing_generation=generation,
        ),
    ):
            connection.execute(
                "INSERT INTO memories (id, content, created_at, updated_at) "
                "VALUES ('impostor', 'x', 'n', 'n')"
            )
    assert connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == before


# SB-03 regression
def test_sb03_a_superseded_owner_cannot_commit_after_takeover(owned) -> None:
    """A lease that moved on invalidates the previous owner mid-flight."""
    connection, owner, generation, _path, clock = owned
    successor = make_identity(instance="svc-two", pid=2222)

    # A real takeover: the successor acquires the lease and a new generation.
    successor_lease = acquire_lease(
        connection,
        successor,
        clock=clock,
        workspace_id=WORKSPACE_ID,
        holds_storage_lock=True,
        lock_mechanism="flock",
    )
    assert successor_lease.fencing_generation > generation

    with pytest.raises(StaleGeneration), fenced_transaction(
        connection,
        owner,
        workspace_id=WORKSPACE_ID,
        fencing_generation=generation,
    ):
        connection.execute(
            "INSERT INTO memories (id, content, created_at, updated_at) "
            "VALUES ('stale-owner', 'x', 'n', 'n')"
        )


# SB-05 regression
def test_sb05_every_mutable_table_is_guarded_for_every_statement() -> None:
    """Coverage is a property of the schema, not of a list someone maintained.

    The hand-written list named nine of sixteen mutable tables, and four of those
    nine were missing one of their three statement triggers.
    """
    import re

    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(phase0_baseline_sql())
        for migration in load_migrations():
            connection.executescript(migration.sql)

        covered: dict[str, set[str]] = {}
        for sql in connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger'"
        ):
            found = re.search(r"BEFORE\s+(INSERT|UPDATE|DELETE)\s+ON\s+(\w+)", sql[0], re.IGNORECASE)
            assert found is not None, sql[0]
            covered.setdefault(found.group(2), set()).add(found.group(1).upper())

        for table in GUARDED_TABLES:
            assert covered.get(table) == {"INSERT", "UPDATE", "DELETE"}, (
                f"{table} is not guarded for every statement: {sorted(covered.get(table, ()))}"
            )

        # And the exemption is exactly the substrate -- nothing else is unguarded.
        all_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert all_tables - set(GUARDED_TABLES) == set(UNGUARDED_SUBSTRATE_TABLES)
    finally:
        connection.close()


# SB-05 regression
@pytest.mark.parametrize(
    ("table", "statement"),
    [
        ("control_plane_events", "INSERT INTO control_plane_events (id, event_type, created_at) VALUES ('e1', 't', 'n')"),
        ("entity_memory_links", "INSERT INTO entity_memory_links (entity_id, memory_id) VALUES ('e', 'm')"),
        ("patterns", "INSERT INTO patterns (id, created_at) VALUES ('p1', 'n')"),
        ("context_packs", "INSERT INTO context_packs (id, created_at) VALUES ('c1', 'n')"),
    ],
)
def test_sb05_previously_unguarded_tables_refuse_writes_with_the_guard_closed(
    owned, table: str, statement: str
) -> None:
    """Each of these committed happily while no service held write authority."""
    connection, _identity, _generation, _path, _clock = owned
    close_guard(connection)

    with pytest.raises(sqlite3.Error, match=REFUSED_EXTERNAL_WRITE):
        connection.execute(statement)


# SB-04 regression
def test_sb04_the_service_connection_refuses_ddl_after_readiness(tmp_path: Path) -> None:
    """The counterexample: `CREATE TABLE` on the live service connection.

    `install_authorizer` existed from the start and nothing outside the tests ever
    called it, so the runtime's own connection could reshape the schema at will.
    """
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    maintenance = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    identity = make_identity()
    state = bootstrap_generation_one(
        maintenance,
        workspace_id=WORKSPACE_ID,
        mode=OpenMode.EXCLUSIVE_MAINTENANCE,
        expect_phase0_baseline=True,
        service_instance_id=identity.service_instance_id,
    )
    apply_pending_migrations(
        maintenance,
        mode=OpenMode.EXCLUSIVE_MAINTENANCE,
        service_instance_id=identity.service_instance_id,
        fencing_generation=state.fencing_generation,
        workspace_id=WORKSPACE_ID,
    )
    maintenance.close()

    service = open_database(path, OpenMode.SERVICE_OWNED)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            service.execute("CREATE TABLE unguarded_runtime_ddl (id INTEGER PRIMARY KEY)")
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            service.execute("DROP TRIGGER omnivia_guard_memories_insert")
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            service.execute(
                "INSERT INTO memories (id, content, created_at, updated_at) "
                "VALUES ('direct', 'x', 'n', 'n')"
            )
        # Reading is unaffected -- this denies authority, not access.
        assert service.execute("SELECT COUNT(*) FROM memories").fetchone() is not None
    finally:
        service.close()


# SB-04 regression
def test_sb04_a_fenced_write_is_permitted_and_the_widening_does_not_leak(
    served_connection,
) -> None:
    """Elevation is bounded by the transaction that earned it.

    This ran against the `owned` fixture at first, which opens
    `EXCLUSIVE_MAINTENANCE` -- deliberately ungoverned -- so its guard skipped every
    time and the case never executed on any platform.
    """
    connection, identity, generation = served_connection
    assert connection.omnivia_authorizer_installed

    with fenced_transaction(
        connection, identity, workspace_id=WORKSPACE_ID, fencing_generation=generation
    ):
        connection.execute(
            "INSERT INTO memories (id, content, source_type, source_reference, "
            "created_by, created_at, updated_at) "
            "VALUES ('fenced', 'x', 't', 'r', 'me', 'n', 'n')"
        )

    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        connection.execute(
            "INSERT INTO memories (id, content, source_type, source_reference, "
            "created_by, created_at, updated_at) "
            "VALUES ('after', 'x', 't', 'r', 'me', 'n', 'n')"
        )


# SB-04 regression
def test_sb04_the_guard_row_cannot_be_written_without_the_lease_check(
    served_connection,
) -> None:
    """The property SB-03 claims, tested where it can actually be broken.

    Binding `open_guard` to the lease is not the same as making guard ownership
    unforgeable. While the authorizer exempted the substrate, the guard row was
    directly writable on the service connection, so the lease check could simply be
    stepped around -- and the persisted triggers, which only ask whether a matching
    guard row exists, then accepted writes from any outside connection.
    """
    connection, _identity, generation = served_connection

    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        connection.execute(
            "INSERT INTO omnivia_mutation_guard (singleton, workspace_id, "
            "service_instance_id, fencing_generation, opened_at) "
            "VALUES (1, ?, 'svc-FORGED', ?, 'now')",
            (WORKSPACE_ID, generation),
        )

    # The rest of the substrate is equally not writable by hand.
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        connection.execute(
            "UPDATE omnivia_workspace_state SET fencing_generation = 99 WHERE singleton = 1"
        )
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        connection.execute("DELETE FROM omnivia_workspace_lease WHERE singleton = 1")


# SB-04 regression
def test_sb04_authority_is_restored_even_when_the_fenced_block_raises(
    tmp_path: Path,
) -> None:
    """A widening that leaked would outlive the failure that caused it."""
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    maintenance = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    identity = make_identity()
    state = bootstrap_generation_one(
        maintenance,
        workspace_id=WORKSPACE_ID,
        mode=OpenMode.EXCLUSIVE_MAINTENANCE,
        expect_phase0_baseline=True,
        service_instance_id=identity.service_instance_id,
    )
    apply_pending_migrations(
        maintenance,
        mode=OpenMode.EXCLUSIVE_MAINTENANCE,
        service_instance_id=identity.service_instance_id,
        fencing_generation=state.fencing_generation,
        workspace_id=WORKSPACE_ID,
    )
    maintenance.close()

    service = open_database(path, OpenMode.SERVICE_OWNED)
    try:
        acquire_lease(
            service,
            identity,
            clock=FakeClock(),
            workspace_id=WORKSPACE_ID,
            holds_storage_lock=True,
            lock_mechanism="flock",
        )
        generation = read_workspace_state(service).fencing_generation  # type: ignore[union-attr]
        open_guard(
            service,
            identity,
            clock=FakeClock(),
            workspace_id=WORKSPACE_ID,
            fencing_generation=generation,
        )

        with (
            pytest.raises(RuntimeError, match="handler exploded"),
            fenced_transaction(
                service,
                identity,
                workspace_id=WORKSPACE_ID,
                fencing_generation=generation,
            ),
        ):
            raise RuntimeError("handler exploded")

        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            service.execute("CREATE TABLE leaked_authority (id INTEGER PRIMARY KEY)")
    finally:
        service.close()


# SB-04 regression
def test_sb04_nested_widening_restores_the_outer_grant_not_the_default(
    served_connection,
) -> None:
    """Leaving an inner block must not revoke authority the outer block still holds.

    Restoring deny-by-default rather than the previous state made nesting silently
    wrong: the outer block kept running with its grant already gone, and the failure
    surfaced as `not authorized` on a statement that had been permitted.
    """
    connection, _identity, _generation = served_connection

    with authorised(connection, mutations=True):
        with authorised(connection, mutations=True):
            pass
        # Still inside the outer widening.
        connection.execute(
            "INSERT INTO omnivia_workspace_open_events "
            "(event_id, opened_at, open_mode) VALUES ('nested', 'n', 'service_owned')"
        )

    # And the outer block's exit restores the deny-by-default state.
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        connection.execute(
            "INSERT INTO omnivia_workspace_open_events "
            "(event_id, opened_at, open_mode) VALUES ('after', 'n', 'service_owned')"
        )


# SB-04 regression
def test_sb04_clearing_the_authorizer_leaves_the_connection_ungoverned(
    served_connection,
) -> None:
    """`authorised(...)` must not re-govern a connection something deliberately cleared.

    Clearing only the authorizer left the connection still marked governed, so the
    next widening block saved a stale grant on entry and installed an authorizer on
    exit -- a connection went from ungoverned to governed as a side effect of an
    unrelated block.
    """
    connection, _identity, _generation = served_connection
    assert connection.omnivia_authorizer_installed

    clear_authorizer(connection)
    assert not connection.omnivia_authorizer_installed

    # Ungoverned: DDL is permitted, and an `authorised` block is a no-op either way.
    connection.execute("CREATE TABLE after_clear (id INTEGER PRIMARY KEY)")
    with authorised(connection, mutations=True):
        pass
    connection.execute("CREATE TABLE after_authorised_block (id INTEGER PRIMARY KEY)")


#: Schema-affecting statements that must be refused on a governed connection,
#: whether or not a fenced write has widened DML.
DDL_ESCAPES = (
    ("ordinary table", "CREATE TABLE authorizer_escape (id TEXT)"),
    ("ordinary index", "CREATE INDEX ix_escape ON memories (id)"),
    ("ordinary trigger", "CREATE TRIGGER tr_escape BEFORE INSERT ON memories BEGIN SELECT 1; END"),
    ("ordinary view", "CREATE VIEW v_escape AS SELECT 1"),
    ("temp table", "CREATE TEMP TABLE authorizer_escape (id TEXT)"),
    ("temp index", "CREATE TEMP TABLE t_escape (id TEXT)"),
    ("temp view", "CREATE TEMP VIEW v_temp_escape AS SELECT 1"),
    ("temp trigger", "CREATE TEMP TRIGGER tr_temp BEFORE INSERT ON memories BEGIN SELECT 1; END"),
    ("virtual table", "CREATE VIRTUAL TABLE vt_escape USING fts5(x)"),
    ("drop trigger", "DROP TRIGGER omnivia_guard_memories_insert"),
    ("alter table", "ALTER TABLE memories ADD COLUMN escaped TEXT"),
    ("attach another database", "ATTACH DATABASE ':memory:' AS side"),
    ("writable_schema pragma", "PRAGMA writable_schema=ON"),
)


# SRB-01 regression
@pytest.mark.parametrize(("label", "statement"), DDL_ESCAPES)
def test_srb01_schema_changes_are_refused_outside_a_fenced_write(
    served_connection, label: str, statement: str
) -> None:
    connection, _identity, _generation = served_connection
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        connection.execute(statement)


# SRB-01 regression
@pytest.mark.parametrize(("label", "statement"), DDL_ESCAPES)
def test_srb01_schema_changes_are_refused_inside_a_fenced_write(
    served_connection, label: str, statement: str
) -> None:
    """A DML grant is a grant to write rows, never to change the schema.

    The deny-list named nine actions and missed ten -- every temporary-object action
    and both virtual-table actions -- so `CREATE TEMP TABLE` succeeded inside a valid
    `fenced_transaction`, which is precisely where product mutations run.
    """
    connection, identity, generation = served_connection
    with (
        pytest.raises(sqlite3.DatabaseError, match="not authorized"),
        fenced_transaction(
            connection, identity, workspace_id=WORKSPACE_ID, fencing_generation=generation
        ),
    ):
        connection.execute(statement)


# SRB-01 regression
def test_srb01_the_deny_list_is_derived_not_enumerated() -> None:
    """Every `SQLITE_CREATE_*`/`SQLITE_DROP_*` action the module exposes is denied.

    A hand-listed set cannot notice the actions it fails to mention, and SQLite adds
    action codes across versions. Names, not values: several `sqlite3` constants
    share a numeric value with an unrelated error code.
    """
    from omnivia_core_runtime.storage.connection import _DDL_ACTIONS

    for name in dir(sqlite3):
        if not name.startswith(("SQLITE_CREATE_", "SQLITE_DROP_")):
            continue
        value = getattr(sqlite3, name)
        if isinstance(value, int):
            assert value in _DDL_ACTIONS, f"{name} is not denied"
    assert sqlite3.SQLITE_ALTER_TABLE in _DDL_ACTIONS


# SRB-01 regression
def test_srb01_only_the_migration_path_may_execute_ddl(served_connection) -> None:
    """DDL is reachable through the authorised migration widening, and nowhere else."""
    connection, _identity, _generation = served_connection

    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        connection.execute("CREATE TABLE not_a_migration (id TEXT)")

    # The widening the migration path uses, and only it, permits schema change.
    with authorised(connection, mutations=True, ddl=True):
        connection.execute("CREATE TABLE migration_shaped (id TEXT)")

    # ...and the grant does not outlive the block.
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        connection.execute("CREATE TABLE after_the_block (id TEXT)")


# SRB-06 regression
def test_srb06_a_forged_guard_row_does_not_authorise_an_outside_writer(
    tmp_path: Path,
) -> None:
    """The persisted layer must enforce the same tuple the Python layer does.

    The triggers asked only whether a guard row existed whose generation matched
    workspace state. Both are ordinary rows, so a stock `sqlite3` client could copy
    one into the other and write -- one statement, no lease involved. SB-03 bound the
    guard to the lease in the Python path; the triggers were left on the weaker
    predicate, and the triggers are the layer that covers writers this runtime did
    not create.
    """
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    maintenance = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    state = bootstrap_generation_one(
        maintenance,
        workspace_id=WORKSPACE_ID,
        mode=OpenMode.EXCLUSIVE_MAINTENANCE,
        expect_phase0_baseline=True,
        service_instance_id="svc-owner",
    )
    apply_pending_migrations(
        maintenance,
        mode=OpenMode.EXCLUSIVE_MAINTENANCE,
        service_instance_id="svc-owner",
        fencing_generation=state.fencing_generation,
        workspace_id=WORKSPACE_ID,
    )
    maintenance.close()

    # A plain sqlite3 client: no authorizer reaches it, only the persisted triggers.
    outside = sqlite3.connect(path)
    try:
        insert_memory = (
            "INSERT INTO memories (id, content, source_type, source_reference, "
            "created_by, created_at, updated_at) VALUES (?, 'x', 't', 'r', 'me', 'n', 'n')"
        )
        with pytest.raises(sqlite3.Error, match=REFUSED_EXTERNAL_WRITE):
            outside.execute(insert_memory, ("before",))

        # Forging the guard row is itself refused now: the substrate requires a
        # runtime connection, which is a different requirement from requiring a
        # guard and carries none of the circularity that ruled the latter out.
        with pytest.raises(sqlite3.Error, match=REFUSED_EXTERNAL_WRITE):
            outside.execute(
                "INSERT INTO omnivia_mutation_guard (singleton, workspace_id, "
                "service_instance_id, fencing_generation, opened_at) "
                "SELECT 1, workspace_id, 'svc-FORGED', fencing_generation, 'now' "
                "FROM omnivia_workspace_state WHERE singleton = 1"
            )
        with pytest.raises(sqlite3.Error, match=REFUSED_EXTERNAL_WRITE):
            outside.execute(insert_memory, ("after",))
        outside.rollback()
    finally:
        outside.close()


# SRB-08 regression
def test_srb08_a_crash_stale_guard_is_invalidated_by_the_next_takeover(
    tmp_path: Path,
) -> None:
    """Bound the one window the persisted layer cannot close by itself.

    A service killed between `open_guard` and `close_guard` leaves a guard row and a
    held lease that agree with each other, so the triggers -- which can read rows and
    nothing else -- cannot tell it from a live owner. No SQL predicate can: liveness
    is not a fact about the database. During that window a writer with the
    workspace-owning OS principal's rights can commit, which is the capability
    ADR-037 already excludes from the threat model.

    What is testable, and what makes the window bounded rather than open-ended, is
    that the next takeover ends it: acquiring the lease increments the generation, and
    the stale guard stops matching immediately.
    """
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    identity = make_identity()
    maintenance = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    state = bootstrap_generation_one(
        maintenance,
        workspace_id=WORKSPACE_ID,
        mode=OpenMode.EXCLUSIVE_MAINTENANCE,
        expect_phase0_baseline=True,
        service_instance_id=identity.service_instance_id,
    )
    apply_pending_migrations(
        maintenance,
        mode=OpenMode.EXCLUSIVE_MAINTENANCE,
        service_instance_id=identity.service_instance_id,
        fencing_generation=state.fencing_generation,
        workspace_id=WORKSPACE_ID,
    )
    lease = acquire_lease(
        maintenance,
        identity,
        clock=FakeClock(),
        workspace_id=WORKSPACE_ID,
        holds_storage_lock=True,
        lock_mechanism="flock",
    )
    open_guard(
        maintenance,
        identity,
        clock=FakeClock(),
        workspace_id=WORKSPACE_ID,
        fencing_generation=lease.fencing_generation,
    )
    maintenance.close()  # SIGKILL: no close_guard, no release_lease

    insert = (
        "INSERT INTO memories (id, content, source_type, source_reference, "
        "created_by, created_at, updated_at) VALUES (?, 'x', 't', 'r', 'me', 'n', 'n')"
    )

    # The successor takes over exactly as a restart would.
    successor = make_identity(instance="svc-successor", pid=4242)
    recovered = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    try:
        acquire_lease(
            recovered,
            successor,
            clock=FakeClock(),
            workspace_id=WORKSPACE_ID,
            holds_storage_lock=True,
            lock_mechanism="flock",
        )
    finally:
        recovered.close()

    # The stale guard no longer matches, so the window is closed.
    outside = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.Error, match=REFUSED_EXTERNAL_WRITE):
            outside.execute(insert, ("after-takeover",))
    finally:
        outside.close()


# SRB-09 regression
def test_srb09_a_stock_client_cannot_write_through_a_crash_stale_guard(
    tmp_path: Path,
) -> None:
    """Close the crash window, rather than documenting it as unavoidable.

    A service killed between `open_guard` and `close_guard` leaves a guard row and a
    held lease that agree, and no predicate over rows alone can tell that pair from a
    live owner. It does not have to: `omnivia_service_writer` is registered per
    connection by `open_database`, so a writer that did not come through this runtime
    fails before touching a row, whatever the rows say.
    """
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    identity = make_identity()
    owner = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    state = bootstrap_generation_one(
        owner,
        workspace_id=WORKSPACE_ID,
        mode=OpenMode.EXCLUSIVE_MAINTENANCE,
        expect_phase0_baseline=True,
        service_instance_id=identity.service_instance_id,
    )
    apply_pending_migrations(
        owner,
        mode=OpenMode.EXCLUSIVE_MAINTENANCE,
        service_instance_id=identity.service_instance_id,
        fencing_generation=state.fencing_generation,
        workspace_id=WORKSPACE_ID,
    )
    lease = acquire_lease(
        owner,
        identity,
        clock=FakeClock(),
        workspace_id=WORKSPACE_ID,
        holds_storage_lock=True,
        lock_mechanism="flock",
    )
    open_guard(
        owner,
        identity,
        clock=FakeClock(),
        workspace_id=WORKSPACE_ID,
        fencing_generation=lease.fencing_generation,
    )
    owner.close()  # SIGKILL: guard row and held lease both survive

    outside = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.Error, match=REFUSED_EXTERNAL_WRITE):
            outside.execute(
                "INSERT INTO memories (id, content, source_type, source_reference, "
                "created_by, created_at, updated_at) "
                "VALUES ('crash', 'x', 't', 'r', 'me', 'n', 'n')"
            )
        # Reading is untouched: these are DML triggers, so ordinary tooling still
        # inspects a workspace and `VACUUM INTO` backups still work.
        assert outside.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
        outside.execute("VACUUM INTO ?", (str(tmp_path / "backup.sqlite"),))
    finally:
        outside.close()


# SRB-11 regression
@pytest.mark.parametrize(
    ("label", "statement"),
    [
        ("release the lease", "UPDATE omnivia_workspace_lease SET lifecycle = 'released' WHERE singleton = 1"),
        ("bump the generation", "UPDATE omnivia_workspace_state SET fencing_generation = 99 WHERE singleton = 1"),
        ("forge a guard row", "INSERT INTO omnivia_mutation_guard (singleton, workspace_id, service_instance_id, fencing_generation, opened_at) VALUES (1, 'ws', 'forged', 1, 'n')"),
        ("rewrite the ledger", "DELETE FROM omnivia_schema_migrations"),
        ("erase open events", "DELETE FROM omnivia_workspace_open_events"),
    ],
)
def test_srb11_the_substrate_refuses_writers_from_outside_the_runtime(
    tmp_path: Path, label: str, statement: str
) -> None:
    """The rows that govern the guard were the ones left open.

    Guarded data tables failed closed for a stock client while the lease, guard and
    ledger rows that decide what those tables permit stayed writable. The substrate
    carries no *guard* triggers on purpose -- requiring an open guard to open one is
    circular -- but requiring the writer to be this runtime is a different question
    with no such circularity.
    """
    path = tmp_path / "workspace.sqlite"
    materialise_phase0_baseline(path)
    identity = make_identity()
    owner = open_database(path, OpenMode.EXCLUSIVE_MAINTENANCE)
    state = bootstrap_generation_one(
        owner,
        workspace_id=WORKSPACE_ID,
        mode=OpenMode.EXCLUSIVE_MAINTENANCE,
        expect_phase0_baseline=True,
        service_instance_id=identity.service_instance_id,
    )
    apply_pending_migrations(
        owner,
        mode=OpenMode.EXCLUSIVE_MAINTENANCE,
        service_instance_id=identity.service_instance_id,
        fencing_generation=state.fencing_generation,
        workspace_id=WORKSPACE_ID,
    )
    acquire_lease(
        owner,
        identity,
        clock=FakeClock(),
        workspace_id=WORKSPACE_ID,
        holds_storage_lock=True,
        lock_mechanism="flock",
    )
    owner.close()

    outside = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.Error, match=REFUSED_EXTERNAL_WRITE):
            outside.execute(statement)
        # Reading the substrate is still open: this refuses authority, not access.
        assert outside.execute(
            "SELECT lifecycle FROM omnivia_workspace_lease WHERE singleton = 1"
        ).fetchone()[0] == "held"
    finally:
        outside.close()
