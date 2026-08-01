"""SQLite connection modes and configuration (T-0629B, ADR-037).

Four explicit open modes replace the legacy "one connect() that also creates and
patches the schema". The mode is a required argument, not a default, because the
difference between inspecting a workspace and owning it for writing is the
difference the whole fencing design rests on.

Ordinary opens cannot create a database or a schema. The legacy
`Database.connect()` called `_init_schema()` unconditionally on every open, so any
process that touched a path materialised 14 tables into it; `sqlite3.connect()`
creates the file by default, so a typo produced a new empty workspace. Both are
closed here by opening through a `file:` URI with `mode=rw`, which fails when the
file is absent, and by never issuing DDL outside an explicit bootstrap or
migration.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

DEFAULT_BUSY_TIMEOUT_MS = 5_000


class OpenMode(str, Enum):
    """How a caller intends to use a workspace database.

    READ_ONLY
        Inspection. Opened `mode=ro` with `query_only`, so a write is refused by
        SQLite itself rather than by convention.
    EPHEMERAL
        A throwaway database the caller owns outright, for tests and probes. The
        only mode permitted to create a file.
    SERVICE_OWNED
        The authoritative writable connection held by one Core Service for the
        whole ownership lifetime, in exclusive locking mode.
    EXCLUSIVE_MAINTENANCE
        Offline maintenance. Also exclusive, but not advertising readiness.
    """

    READ_ONLY = "read_only"
    EPHEMERAL = "ephemeral"
    SERVICE_OWNED = "service_owned"
    EXCLUSIVE_MAINTENANCE = "exclusive_maintenance"

    @property
    def writable(self) -> bool:
        return self is not OpenMode.READ_ONLY

    @property
    def exclusive(self) -> bool:
        return self in (OpenMode.SERVICE_OWNED, OpenMode.EXCLUSIVE_MAINTENANCE)

    @property
    def may_create(self) -> bool:
        return self is OpenMode.EPHEMERAL


class StorageError(Exception):
    """A workspace database could not be opened or configured as requested."""


class SchemaCreationRefused(StorageError):
    """An ordinary open was asked to create or patch a schema."""


@dataclass(frozen=True)
class SchemaFingerprint:
    """Exact identity of a database's schema and triggers.

    `digest` covers every `sqlite_master` entry's type, name and SQL in a
    deterministic order. ADR-037 requires schema and trigger drift to be detected
    before writable readiness, so triggers are counted separately: a database that
    has lost its guard triggers must be distinguishable from one whose tables
    changed.
    """

    digest: str
    tables: int
    indexes: int
    triggers: int

    def matches(self, other: SchemaFingerprint) -> bool:
        return self.digest == other.digest


def fingerprint_schema(connection: sqlite3.Connection) -> SchemaFingerprint:
    """Compute the schema fingerprint of an open connection."""
    rows = connection.execute(
        "SELECT type, name, COALESCE(sql, '') FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()

    hasher = hashlib.sha256()
    tables = indexes = triggers = 0
    for kind, name, sql in rows:
        hasher.update(f"{kind}\x1f{name}\x1f{_normalise_sql(sql)}\x1e".encode())
        if kind == "table":
            tables += 1
        elif kind == "index":
            indexes += 1
        elif kind == "trigger":
            triggers += 1
    return SchemaFingerprint(
        digest=hasher.hexdigest(), tables=tables, indexes=indexes, triggers=triggers
    )


def _normalise_sql(sql: str) -> str:
    """Collapse whitespace so formatting alone never changes a fingerprint."""
    return " ".join(sql.split())


def fingerprint_sql_script(script: str) -> str:
    """Fingerprint of a DDL script, matching `fingerprint_schema` ordering.

    Lets a frozen `.sql` artifact be compared against a live database without
    materialising the artifact first, which is what makes the Phase 0 oracle
    independent of the runtime that originally produced the schema.
    """
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(script)
        return fingerprint_schema(connection).digest
    finally:
        connection.close()


def _schema_action_codes() -> frozenset[int]:
    """Every authorizer action that creates or destroys a schema object.

    Derived from the `sqlite3` module rather than listed. The listed version named
    nine actions and missed ten -- every temporary-object action and both
    virtual-table actions -- so `CREATE TEMP TABLE` was permitted during a widened
    DML block, and the authorizer did not in fact deny DDL.

    A list of action codes cannot notice the ones it does not mention, and SQLite
    adds action codes across versions. Matching `SQLITE_CREATE_*` and
    `SQLITE_DROP_*` by name means a newly exposed schema action is denied on the day
    the module exposes it, which is the correct default for a deny-list.

    Names, never values: several `sqlite3` constants share a numeric value with an
    unrelated error code, so building this by value silently collapses entries.
    """
    return frozenset(
        value
        for name in dir(sqlite3)
        if name.startswith(("SQLITE_CREATE_", "SQLITE_DROP_"))
        and isinstance(value := getattr(sqlite3, name), int)
    )


#: Statement classes the authorizer refuses outright on a governed connection.
_DDL_ACTIONS = _schema_action_codes() | {sqlite3.SQLITE_ALTER_TABLE}

_DML_ACTIONS = frozenset(
    {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}
)

#: Reaching a second database file is not DDL, and is refused on every governed
#: connection regardless of widening. `ATTACH` would let a fenced write copy
#: workspace rows somewhere no guard, trigger or lease covers, which defeats the
#: point of governing this connection at all. Nothing in the runtime attaches.
_REACH_ACTIONS = frozenset({sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH})

#: `PRAGMA writable_schema=ON` turns `sqlite_master` into an ordinary writable
#: table, so the schema can be rewritten through DML without any DDL action being
#: reported. Denied by name because the runtime's legitimate pragmas --
#: `integrity_check`, `user_version`, `foreign_keys`, `busy_timeout` and the rest --
#: must keep working on the service connection.
_FORBIDDEN_PRAGMAS = frozenset({"writable_schema"})


def install_authorizer(
    connection: sqlite3.Connection,
    *,
    allow_mutations: bool,
    allow_ddl: bool = False,
) -> None:
    """Deny DDL and DML on a governed connection unless explicitly widened.

    Connection-local by nature, so this cannot reach a connection the runtime did
    not create — which is why the persisted triggers exist as well.

    No table is exempt. An earlier version of this always permitted DML on the
    ownership substrate, reasoning that taking a lease and opening a guard are what
    *establish* authority and so cannot require it. That reasoning was right about
    the ordering and wrong about the mechanism: it made
    `omnivia_mutation_guard` directly writable, so a guard row could be inserted
    without going through `open_guard` and therefore without the lease check. The
    persisted triggers only ask whether a matching guard row exists, so a forged one
    let any outside connection commit unguarded writes -- the exact property the
    lease binding exists to enforce.

    The substrate is now written under `authorised(...)` inside the functions that
    check authority first. The permission lives with the check instead of being
    granted to the whole connection for its lifetime.

    It lives here rather than in `fencing` because `lease` and `migrations` both need
    it, and both are imported by `fencing`.
    """

    def authorize(action: int, arg1: str | None, arg2: str | None, *_rest: object) -> int:
        # Unconditional, because no widening this runtime performs has any use for
        # them and each is a way around the checks the widening is scoped by.
        if action in _REACH_ACTIONS:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA and (arg1 or "").lower() in _FORBIDDEN_PRAGMAS:
            return sqlite3.SQLITE_DENY
        if action in _DDL_ACTIONS:
            return sqlite3.SQLITE_OK if allow_ddl else sqlite3.SQLITE_DENY
        if action in _DML_ACTIONS:
            # A DML grant never reaches `sqlite_*`: with `writable_schema` on, an
            # UPDATE of `sqlite_master` is a schema edit wearing DML's clothes, and a
            # fenced write is exactly where the DML grant is live.
            #
            # Gated on the DDL grant rather than refused outright, because SQLite
            # reports schema creation *as* an insert into `sqlite_master` -- so a
            # blanket refusal here rejects every `CREATE TABLE` in a migration.
            if (arg1 or "").lower().startswith("sqlite_"):
                return sqlite3.SQLITE_OK if allow_ddl else sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK if allow_mutations else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorize)
    # Recorded because SQLite offers no way to read an authorizer back, and
    # `authorised` has to restore what was in force rather than assume.
    connection.omnivia_authority = (allow_mutations, allow_ddl)  # type: ignore[attr-defined]


def clear_authorizer(connection: sqlite3.Connection) -> None:
    """Remove the authorizer, and the record that one was ever installed.

    Both, because they have to agree. Clearing only the authorizer left the
    connection still marked governed, so the next `authorised(...)` block saved a
    stale grant on entry and *installed* an authorizer on exit -- silently
    re-governing a connection something had deliberately ungoverned, as a side effect
    of an unrelated block.
    """
    connection.set_authorizer(None)
    connection.omnivia_authorizer_installed = False  # type: ignore[attr-defined]
    connection.omnivia_authority = (False, False)  # type: ignore[attr-defined]


@contextmanager
def authorised(
    connection: sqlite3.Connection, *, mutations: bool = False, ddl: bool = False
) -> Iterator[sqlite3.Connection]:
    """Widen this connection's authority for one bounded block, then restore it.

    Exception-safe by construction: the `finally` restores the deny-by-default
    authorizer whether the block committed, raised or was cancelled. A widening that
    leaked would leave the connection able to do arbitrary DDL for the rest of the
    process, which is the state this mechanism exists to prevent.

    On a connection with no authorizer installed -- a maintenance or migration open,
    where DDL is the point -- this is a no-op, so callers do not have to know which
    kind of connection they were handed.

    What is restored is whatever was in force on entry, not the deny-by-default
    state. Restoring the default instead makes nesting silently wrong: the inner
    block's exit revokes the outer block's widening, and the caller sees `not
    authorized` from a statement it had already been granted. Nothing nests today,
    which is exactly why it would be found the hard way later.
    """
    if not getattr(connection, "omnivia_authorizer_installed", False):
        yield connection
        return

    previous: tuple[bool, bool] = getattr(connection, "omnivia_authority", (False, False))
    install_authorizer(connection, allow_mutations=mutations, allow_ddl=ddl)
    try:
        yield connection
    finally:
        install_authorizer(
            connection, allow_mutations=previous[0], allow_ddl=previous[1]
        )


def mark_authorizer_installed(connection: sqlite3.Connection) -> None:
    """Record that this connection is authorizer-governed.

    `sqlite3.Connection` exposes no way to read back the authorizer it is holding, so
    the fact is recorded next to it. Without this, `authorised` could not tell a
    governed service connection from a maintenance connection and would install a
    deny-by-default authorizer on one that deliberately has none.
    """
    connection.omnivia_authorizer_installed = True  # type: ignore[attr-defined]


#: Name of the connection-local function the persisted guard triggers call.
SERVICE_WRITER_FUNCTION = "omnivia_service_writer"


def _service_writer_token() -> int:
    """The value the guard predicate expects. Presence is the whole signal."""
    return 1


class GovernedConnection(sqlite3.Connection):
    """A connection that can record whether an authorizer governs it.

    `sqlite3.Connection` accepts no attributes and cannot be weak-referenced, and it
    offers no way to read back the authorizer it holds -- so without a subclass there
    is nowhere to put the one fact `fencing.authorised` needs: whether widening this
    connection's authority and restoring the default afterwards is correct, or would
    install a deny-by-default authorizer on a maintenance connection that is supposed
    to have none.
    """

    #: Set by `open_database` for the modes it governs.
    omnivia_authorizer_installed = False


def open_database(
    path: Path,
    mode: OpenMode,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    enable_wal: bool = True,
) -> sqlite3.Connection:
    """Open a workspace database in an explicit mode.

    Raises `StorageError` when the database is absent and the mode may not create
    it, so a mistyped path can never silently become a new empty workspace.
    """
    if mode.may_create:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.is_file():
        raise StorageError(f"no workspace database at {path} (mode={mode.value})")

    uri = _connection_uri(path, mode)
    connection = sqlite3.connect(
        uri,
        uri=True,
        check_same_thread=False,
        factory=GovernedConnection,
        # Manual transaction control. The legacy implementation left this at the
        # default and then called commit() after every statement, which is why its
        # transaction() context manager provided no atomicity at all.
        isolation_level=None,
    )
    try:
        _configure(connection, mode, busy_timeout_ms=busy_timeout_ms, enable_wal=enable_wal)
    except Exception:
        connection.close()
        raise

    # Registered on every connection this runtime opens, in every mode. The guard
    # triggers call it, so a writer that did not come through here fails with "no such
    # function" before touching a row -- including a stock `sqlite3` client walking in
    # on the guard row a crashed service left behind. It is connection-local by
    # nature, which is exactly why it cannot be forged by writing rows.
    connection.create_function(
        SERVICE_WRITER_FUNCTION, 0, _service_writer_token, deterministic=True
    )

    if mode is OpenMode.SERVICE_OWNED:
        # T-0629F requires the authorizer on every runtime-created connection, and
        # this is the only place they are created. Installing it at each call site
        # instead is how it came to be installed at none of them: the helper existed
        # and nothing outside the tests ever called it, so the service's own
        # connection could create tables after publishing readiness.
        #
        # Only the service connection. Maintenance and migration opens exist to do
        # DDL, and denying it there would refuse the work they are for.
        install_authorizer(connection, allow_mutations=False, allow_ddl=False)
        mark_authorizer_installed(connection)

    return connection


def _connection_uri(path: Path, mode: OpenMode) -> str:
    as_uri = path.resolve().as_uri().replace("file://", "file:", 1)
    if mode is OpenMode.READ_ONLY:
        return f"{as_uri}?mode=ro"
    if mode.may_create:
        return f"{as_uri}?mode=rwc"
    # `rw` fails when the file is missing instead of creating it.
    return f"{as_uri}?mode=rw"


def _configure(
    connection: sqlite3.Connection,
    mode: OpenMode,
    *,
    busy_timeout_ms: int,
    enable_wal: bool,
) -> None:
    # Order matters. locking_mode must be set before the first write acquires a
    # lock, and WAL cannot be set on a read-only connection.
    connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")

    if mode is OpenMode.READ_ONLY:
        connection.execute("PRAGMA query_only = ON")
    else:
        if mode.exclusive:
            connection.execute("PRAGMA locking_mode = EXCLUSIVE")
        if enable_wal:
            journal = connection.execute("PRAGMA journal_mode = WAL").fetchone()
            if journal is not None and str(journal[0]).lower() not in ("wal", "memory"):
                raise StorageError(
                    f"WAL could not be enabled (journal_mode={journal[0]!r}); "
                    "the filesystem may not support it"
                )

    # Foreign keys are per-connection in SQLite and default to OFF. The legacy
    # implementation never enabled them, so referential integrity was unenforced
    # even though the schema declared it.
    connection.execute("PRAGMA foreign_keys = ON")
    enforced = connection.execute("PRAGMA foreign_keys").fetchone()
    if mode.writable and (enforced is None or int(enforced[0]) != 1):
        raise StorageError("foreign key enforcement could not be enabled")


def integrity_check(connection: sqlite3.Connection) -> list[str]:
    """Run `PRAGMA integrity_check`, returning problems (empty means healthy)."""
    rows = connection.execute("PRAGMA integrity_check").fetchall()
    return [str(row[0]) for row in rows if str(row[0]).lower() != "ok"]


def foreign_key_check(connection: sqlite3.Connection) -> list[str]:
    """Rows violating a declared foreign key."""
    return [
        f"{row[0]}: rowid {row[1]} violates FK to {row[2]}"
        for row in connection.execute("PRAGMA foreign_key_check").fetchall()
    ]


@contextmanager
def immediate_transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A real write transaction: `BEGIN IMMEDIATE`, commit or roll back.

    Every managed mutation uses this. It takes SQLite's reserved write lock up
    front, so a competing writer blocks or fails fast rather than discovering the
    conflict at commit time.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.OperationalError:
            # BEGIN IMMEDIATE itself failed on contention; nothing to undo.
            pass
        raise
    else:
        connection.execute("COMMIT")


def _is_unterminated_trigger(statement: str) -> bool:
    """Whether `statement` is a CREATE TRIGGER whose body has not closed yet.

    A trigger body runs from BEGIN to its matching END. Until END is the last
    keyword seen, an intervening semicolon belongs to the body rather than ending
    the statement.
    """
    normalised = " ".join(statement.split()).upper()
    if not normalised.startswith("CREATE TRIGGER"):
        return False
    if " BEGIN" not in normalised:
        return False
    return not normalised.endswith("END")


def split_sql_statements(script: str) -> list[str]:
    """Split a DDL script into individual statements.

    Needed because `sqlite3.Connection.executescript` issues an implicit COMMIT
    before running, which would silently end an enclosing `BEGIN IMMEDIATE` and
    split a supposedly atomic bootstrap into separately committed pieces. Executing
    statement by statement keeps them inside the caller's transaction.

    Handles `--` line comments, `/* */` blocks and quoted literals so a semicolon
    inside a string or comment does not split a statement.
    """
    statements: list[str] = []
    current: list[str] = []
    index = 0
    length = len(script)
    quote: str | None = None

    while index < length:
        char = script[index]

        if quote is not None:
            current.append(char)
            if char == quote:
                # Doubled quote is an escaped literal, not a terminator.
                if index + 1 < length and script[index + 1] == quote:
                    current.append(script[index + 1])
                    index += 2
                    continue
                quote = None
            index += 1
            continue

        if char in ("'", '"'):
            quote = char
            current.append(char)
            index += 1
            continue

        if char == "-" and index + 1 < length and script[index + 1] == "-":
            newline = script.find("\n", index)
            index = length if newline == -1 else newline + 1
            continue

        if char == "/" and index + 1 < length and script[index + 1] == "*":
            end = script.find("*/", index + 2)
            index = length if end == -1 else end + 2
            continue

        if char == ";":
            statement = "".join(current).strip()
            # A trigger body contains its own semicolons: `CREATE TRIGGER ... BEGIN
            # SELECT RAISE(...); END;`. Splitting on the first one would cut the
            # body in half and SQLite reports only "incomplete input", so the
            # statement is not terminated until its closing END is seen.
            if _is_unterminated_trigger(statement):
                current.append(char)
                index += 1
                continue
            if statement:
                statements.append(statement)
            current = []
            index += 1
            continue

        current.append(char)
        index += 1

    trailing = "".join(current).strip()
    if trailing:
        statements.append(trailing)
    return statements


def execute_script(connection: sqlite3.Connection, script: str) -> int:
    """Run every statement of `script` on `connection`, preserving any transaction.

    Returns the number of statements executed. Unlike `executescript`, this never
    commits on the caller's behalf.
    """
    statements = split_sql_statements(script)
    for statement in statements:
        connection.execute(statement)
    return len(statements)


def table_names(connection: sqlite3.Connection) -> list[str]:
    """Sorted user table names."""
    return [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]


__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "OpenMode",
    "SchemaCreationRefused",
    "SchemaFingerprint",
    "StorageError",
    "execute_script",
    "fingerprint_schema",
    "fingerprint_sql_script",
    "foreign_key_check",
    "immediate_transaction",
    "integrity_check",
    "open_database",
    "split_sql_statements",
    "table_names",
]
