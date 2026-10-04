"""The restricted local analytical worker (WP03, SPEC-CORE-DATA-001 §17).

One worker process isolates one analytical attempt. The module implements the
U-029 trusted bootstrap and the D04 admission conditions as executable code:

- **Trusted bootstrap order** — admitted immutable inputs are registered as
  engine tables during trusted setup; external access is then closed; the
  closed state is verified and non-reopenable; every later `SET` is refused by
  this module's grammar (the Q39d finding: the engine itself would accept one).
- **The host, not the engine, is the binding control** — a watchdog enforces
  wall-time and the spill-directory quota by interrupting the engine; the
  module never claims RLIMIT support on a platform that cannot enforce it.
- **Fail-closed grammar** — `execute_sql` accepts exactly one read statement
  over the registered tables: no INTO, no locks, no mutating CTE bodies, no
  `SET`/`ATTACH`/`PRAGMA`/extension/UDF/file-function surface, no
  multi-statement input.
- **UTC session** — the trusted bootstrap pins the engine session `TimeZone`
  to `UTC` and re-verifies it, so `TIMESTAMPTZ` cells are unambiguous instants.
- **No canonical authority** — this module never opens the workspace SQLite
  database and never imports the storage layer; it produces candidate
  artifacts for the service writer to commit. `execute_result_artifact` stages
  one fixed-leaf candidate in the attempt directory and returns its handle.

The module is standard-library, the two admitted dependencies, and the pure
Core contracts and result artifact protocol only.
"""

from __future__ import annotations

import ctypes
import errno
import os
import re
import shutil
import stat
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

import duckdb
import sqlglot
from sqlglot import exp
from sqlglot.tokenizer_core import Token, TokenType

from omnivia_core.contracts.v1 import is_identifier
from omnivia_core_runtime.analysis.result_artifact import (
    LOGICAL_BOOLEAN,
    LOGICAL_BYTES,
    LOGICAL_DATE,
    LOGICAL_DECIMAL,
    LOGICAL_FLOAT,
    LOGICAL_INTEGER,
    LOGICAL_STRING,
    LOGICAL_TIMESTAMPTZ,
    MAX_RESULT_BYTES_CEILING,
    MAX_RESULT_COLUMNS,
    MAX_RESULT_ROWS_CEILING,
    AnalysisExecutionEcho,
    AnalysisResultArtifactCandidate,
    AnalysisResultArtifactRefused,
    AnalysisResultColumn,
    encode_analysis_result_artifact,
    validate_analysis_result_artifact_candidate,
)

__all__ = [
    "BOUNDARY_REFUSAL_CODE",
    "RESOURCE_REFUSAL_CODE",
    "AnalysisWorker",
    "StagedAnalysisResult",
    "WorkerBootstrapConfig",
    "WorkerRefusal",
    "open_analysis_worker",
]


#: Refusal codes, mirroring the application error vocabulary (typed, not strings
#: invented here): a grammar refusal is `invalid_request`-shaped; a resource
#: bound is `execution_limit`-shaped (the Runtime budget vocabulary).
BOUNDARY_REFUSAL_CODE: str = "invalid_request"
RESOURCE_REFUSAL_CODE: str = "execution_limit"

#: Table functions admitted by the grammar: explicit bounded integer arguments
#: only. Everything else that names a function-table is refused.
_ADMITTED_TABLE_FUNCTIONS: frozenset[str] = frozenset({"range", "generateseries"})
_MUTATING_ROOTS: frozenset[str] = frozenset(
    {
        "insert",
        "update",
        "delete",
        "create",
        "attach",
        "detach",
        "drop",
        "alter",
        "copy",
        "export",
        "import",
        "install",
        "load",
        "set",
        "pragma",
        "call",
        "command",
        "checkpoint",
        "use",
        "begin",
        "commit",
        "rollback",
        "grant",
        "revoke",
    }
)
_FORBIDDEN_FUNCTIONS: frozenset[str] = frozenset(
    {  # Anything that reaches outside the registered tables.
        "read_csv",
        "read_csv_auto",
        "read_parquet",
        "read_json",
        "read_json_auto",
        "read_text",
        "read_blob",
        "read_xlsx",
        "glob",
        "parquet_scan",
        "iceberg_scan",
        "delta_scan",
        "postgres_scan",
        "mysql_scan",
        "sqlite_scan",
        "st_read",
    }
)


#: The one staged result leaf; callers never choose a name or a path.
_RESULT_LEAF: Final = "analysis-result.json"
_PRIVATE_DIRECTORY_MODE: Final = 0o700
_PRIVATE_FILE_MODE: Final = 0o600
#: Windows leaf create: exclusive (CREATE_NEW), unshared, and never following a link
#: (FILE_FLAG_OPEN_REPARSE_POINT). The CRT flags adopt the handle as write-only.
_WIN_GENERIC_WRITE: Final = 0x40000000
_WIN_CREATE_NEW: Final = 1
_WIN_CREATE_FLAGS: Final = (
    0x00200080  # FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT
)
_WIN_CRT_FLAGS: Final = 0x00008081  # _O_WRONLY | _O_NOINHERIT | _O_BINARY
_WIN_INVALID_HANDLE: Final = (1 << (8 * ctypes.sizeof(ctypes.c_void_p))) - 1
#: Native resource exhaustion and write failures; every other code is topology.
_WIN_RESOURCE_CODES: Final = frozenset(
    {4, 8, 14, 29, 31, 39, 110, 112, 223, 1117, 1127, 1295, *range(1450, 1456), 1816}
)

#: The closed `str(DuckDBPyType)` vocabulary the artifact admits. Everything else
#: (unsigned, UUID, JSON, naive temporal, interval, enum, collections) refuses.
_COLUMN_TYPES: Final = {
    "BOOLEAN": LOGICAL_BOOLEAN,
    "TINYINT": LOGICAL_INTEGER,
    "SMALLINT": LOGICAL_INTEGER,
    "INTEGER": LOGICAL_INTEGER,
    "BIGINT": LOGICAL_INTEGER,
    "HUGEINT": LOGICAL_INTEGER,
    "FLOAT": LOGICAL_FLOAT,
    "DOUBLE": LOGICAL_FLOAT,
    "VARCHAR": LOGICAL_STRING,
    "BLOB": LOGICAL_BYTES,
    "DATE": LOGICAL_DATE,
    "TIMESTAMP WITH TIME ZONE": LOGICAL_TIMESTAMPTZ,
}
#: Failures that are resource, limit or I/O pressure, not topology or mode.
_RESOURCE_ERRNOS: Final = frozenset(
    getattr(errno, name)
    for name in (
        "ENOSPC",
        "EDQUOT",
        "EIO",
        "EMFILE",
        "ENFILE",
        "ENOMEM",
        "EFBIG",
        "EOVERFLOW",
    )
    if hasattr(errno, name)
)
_DECIMAL_TYPE: Final = re.compile(r"DECIMAL\(([1-9][0-9]?),(0|[1-9][0-9]?)\)")

_REFUSE_UTC: Final = "the engine session time zone is not UTC"
_REFUSE_AUTHORITY: Final = "the result artifact authority is not admitted"
_REFUSE_BOUNDS: Final = "the result artifact bounds are not admitted"
_REFUSE_SCHEMA: Final = "the result columns are not admitted"
_REFUSE_TOPOLOGY: Final = "the attempt directory or result leaf is not safe"
_REFUSE_RESULT: Final = "the result exceeds its admitted bounds"
_REFUSE_WRITE: Final = "the result artifact could not be staged"


class WorkerRefusal(Exception):
    """A handler-visible refusal: the worker did not produce an authoritative result.

    Refusals before the fixed result leaf is exclusively created leave no new leaf.
    A refusal after that point retains whatever occupies the leaf name: possibly
    partial, complete or foreign, and never authoritative. Only a returned
    `StagedAnalysisResult` is; retry in a fresh attempt directory.
    """

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


@dataclass(frozen=True, slots=True)
class StagedAnalysisResult:
    """One staged result: the validated Phase A candidate and its fixed relative handle."""

    candidate: AnalysisResultArtifactCandidate
    handle: str


@dataclass(frozen=True)
class WorkerBootstrapConfig:
    """Everything the trusted bootstrap may touch. Nothing else exists.

    `inputs` maps admitted immutable input files to the table name the SQL may
    reference. Both sides are literals from the service's attempt plan — never
    from a request, a model or a caller.
    """

    inputs: tuple[tuple[str, Path], ...]
    memory_limit: str
    temp_directory: Path
    spill_quota_bytes: int
    wall_time_seconds: int
    attempt_directory: Path
    engine_extension_paths: str = ""

    def __post_init__(self) -> None:
        if self.spill_quota_bytes <= 0:
            raise ValueError("spill_quota_bytes must be positive")
        if self.wall_time_seconds <= 0:
            raise ValueError("wall_time_seconds must be positive")
        names = [name for name, _ in self.inputs]
        if len(names) != len(set(names)):
            raise ValueError("duplicate input table names")
        if not all(_safe_name(name) for name in names):
            raise ValueError("input table names must be plain identifiers")


def _safe_name(name: str) -> bool:
    return (bool(name) and name[0].isalpha() or name[0] == "_") and all(
        ch.isalnum() or ch == "_" for ch in name
    )


def _detect_filesystem_for_record(path: Path) -> str:
    """Best-effort filesystem name for the evidence record only.

    The worker's confinement does NOT depend on this: the closed posture and
    the watchdog enforce it. This records the host honestly (the D04 record:
    RLIMIT is not enforceable on macOS, so the evidence record names the
    platform rather than claiming an unenforceable control).
    """
    try:
        import subprocess

        completed = subprocess.run(
            ["df", "-P", str(path)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return "recorded" if completed.returncode == 0 else "unrecorded"
    except Exception:  # noqa: BLE001
        return "unrecorded"


class AnalysisWorker:
    """One bootstrapped engine over admitted inputs. Not reusable across attempts."""

    def __init__(self, config: WorkerBootstrapConfig) -> None:
        self._config = config
        self._registered: dict[str, tuple[int, ...]] = {}
        self._closed = False
        self._interrupted_reason: str | None = None
        self._watchdog_stop = threading.Event()
        self._watchdog: threading.Thread | None = None
        self._con: duckdb.DuckDBPyConnection = self._bootstrap()

    # -- trusted bootstrap ---------------------------------------------------

    def _bootstrap(self) -> duckdb.DuckDBPyConnection:
        config = self._config
        _prepare_attempt_directory(config.attempt_directory)
        config.temp_directory.mkdir(parents=True, exist_ok=True)
        # Step 1: open with the admitted resource configuration. No connection
        # string, no extension path, no filesystem knob beyond the admitted
        # temp directory.
        con = duckdb.connect(
            ":memory:",
            config={
                "memory_limit": config.memory_limit,
                "temp_directory": str(config.temp_directory),
                "autoinstall_known_extensions": False,
                "autoload_known_extensions": False,
            },
        )
        # Pin the session to UTC before anything else runs, so every TIMESTAMPTZ
        # the engine returns is an unambiguous UTC instant.
        _require_utc_session(con, pin=True)
        # Step 2: register the admitted inputs during trusted setup, while file
        # access is still possible. The engine reads each admitted file through
        # its own reader; the worker never passes file paths at execute time.
        for name, path in config.inputs:
            if not path.is_file():
                raise WorkerRefusal(
                    BOUNDARY_REFUSAL_CODE,
                    f"admitted input {name} is missing: {path.name}",
                )
            con.execute(
                f"CREATE OR REPLACE TEMP TABLE {name} AS "
                f"SELECT * FROM read_parquet('{path}')"
                if path.suffix == ".parquet"
                else f"CREATE OR REPLACE TEMP TABLE {name} AS "
                f"SELECT * FROM read_json_auto('{path}')"
            )
            rows = con.execute(f"SELECT count(*) FROM {name}").fetchone()
            self._registered[name] = (int(rows[0]) if rows else 0,)
        # Step 3: close external access. After this the engine cannot read
        # files, load extensions or reach the network.
        con.execute("SET enable_external_access=false")
        state = con.execute(
            "SELECT current_setting('enable_external_access')"
        ).fetchone()
        if state is None or state[0] is not False:
            raise WorkerRefusal(
                RESOURCE_REFUSAL_CODE, "external access did not close at bootstrap"
            )
        # Step 4: prove non-reopenable on the exact release (Q39b evidence).
        try:
            con.execute("SET enable_external_access=true")
            reopened = con.execute(
                "SELECT current_setting('enable_external_access')"
            ).fetchone()
            if reopened is not None and reopened[0] is True:
                raise WorkerRefusal(
                    RESOURCE_REFUSAL_CODE,
                    "external access re-opened after close on this engine release",
                )
        except WorkerRefusal:
            raise
        except Exception:  # noqa: BLE001, S110 - refused by the engine: the required outcome
            pass
        _require_utc_session(con, pin=False)  # the probes above must not move it
        # Step 5: start the host watchdog — wall-time and spill quota. The
        # engine's own settings are not a binding control (Q39d/F-5 evidence).
        self._watchdog = threading.Thread(target=self._watch, daemon=True)
        self._watchdog.start()
        self._started_at = time.monotonic()
        self._closed = True
        return con

    def _watch(self) -> None:
        deadline = time.monotonic() + self._config.wall_time_seconds
        while not self._watchdog_stop.wait(timeout=1.0):
            if time.monotonic() > deadline:
                self._interrupted_reason = "wall_time_exceeded"
                self.interrupt()
                return
            used = _tree_size(self._config.temp_directory)
            if used > self._config.spill_quota_bytes:
                self._interrupted_reason = "spill_quota_exceeded"
                self.interrupt()
                return

    # -- execution boundary ---------------------------------------------------

    def execute_sql(
        self, sql: str, parameters: tuple[Any, ...] = ()
    ) -> list[tuple[Any, ...]]:
        """Execute exactly one read statement over the registered tables.

        Every refusal here leaves the engine state unchanged: the statement is
        analysed before the engine sees it, and the engine is interrupted by
        the host watchdog when it exceeds the admitted resources.
        """
        return self._execute_once(sql, parameters, limit=None, aliased=False)[1]

    def execute_result_artifact(
        self,
        sql: str,
        parameters: tuple[Any, ...] = (),
        *,
        echo: AnalysisExecutionEcho,
        units: tuple[str | None, ...],
        max_rows: int,
        max_bytes: int,
    ) -> StagedAnalysisResult:
        """Execute one read statement and stage its whole result as one candidate.

        Everything the artifact needs is proven before the engine sees the SQL:
        the exact echo, the ordinal units and the bounds. The result is fetched
        once, bounded by `max_rows` plus one sentinel row, encoded only through
        the Phase A protocol, validated again immediately before the fixed leaf
        in the attempt directory is created, and returned as that validated
        snapshot with its relative handle. Nothing here ever returns a path. A
        refusal after the leaf is created retains it, non-authoritative.
        """
        expected_echo = _echo_snapshot(echo)
        if expected_echo is None or type(parameters) is not tuple:
            raise WorkerRefusal(BOUNDARY_REFUSAL_CODE, _REFUSE_AUTHORITY)
        if not _admitted_units(units):
            raise WorkerRefusal(BOUNDARY_REFUSAL_CODE, _REFUSE_AUTHORITY)
        if not (
            _bounded_int(max_rows, 0, MAX_RESULT_ROWS_CEILING)
            and _bounded_int(max_bytes, 1, MAX_RESULT_BYTES_CEILING)
        ):
            raise WorkerRefusal(RESOURCE_REFUSAL_CODE, _REFUSE_BOUNDS)
        description, rows = self._execute_once(
            sql, parameters, limit=max_rows + 1, aliased=True
        )
        if len(rows) > max_rows:
            raise WorkerRefusal(RESOURCE_REFUSAL_CODE, _REFUSE_RESULT)
        self._check_interrupted()
        schema = _result_columns(description, units)
        if schema is None:
            raise WorkerRefusal(BOUNDARY_REFUSAL_CODE, _REFUSE_SCHEMA)
        try:
            candidate: AnalysisResultArtifactCandidate | None = (
                encode_analysis_result_artifact(
                    schema,
                    rows,
                    echo=expected_echo,
                    max_rows=max_rows,
                    max_bytes=max_bytes,
                )
            )
        except AnalysisResultArtifactRefused:
            candidate = None
        if candidate is None:
            raise WorkerRefusal(RESOURCE_REFUSAL_CODE, _REFUSE_RESULT)
        # The proof and the stage are adjacent: only the returned snapshot is used.
        try:
            snapshot: AnalysisResultArtifactCandidate | None = (
                validate_analysis_result_artifact_candidate(candidate)
            )
        except AnalysisResultArtifactRefused:
            snapshot = None
        if (
            snapshot is None
            or snapshot.schema != schema
            or snapshot.echo != expected_echo
        ):
            raise WorkerRefusal(BOUNDARY_REFUSAL_CODE, _REFUSE_AUTHORITY)
        self._check_interrupted()
        _stage_leaf(
            self._config.attempt_directory,
            snapshot.artifact_bytes,
            self._check_interrupted,
        )
        return StagedAnalysisResult(candidate=snapshot, handle=_RESULT_LEAF)

    def _check_interrupted(self) -> None:
        if self._interrupted_reason is not None:
            raise WorkerRefusal(RESOURCE_REFUSAL_CODE, self._interrupted_reason)

    def _execute_once(
        self,
        sql: str,
        parameters: tuple[Any, ...],
        *,
        limit: int | None,
        aliased: bool,
    ) -> tuple[tuple[tuple[object, str], ...], list[tuple[Any, ...]]]:
        """Grammar-check once, execute the caller's SQL once, and fetch it.

        Returns the cursor's own description (name, `str(type)`) and the rows;
        `limit=None` fetches everything, otherwise exactly one `fetchmany`. The
        engine can fail lazily at fetch, so both calls sit in one handler.
        """
        self._check_interrupted()
        parsed, tokens = self._grammar_check(sql)
        if aliased:
            _require_explicit_aliases(parsed, tokens)
        try:
            cursor = self._con.execute(sql, list(parameters))
            description = tuple(
                (column[0], str(column[1])) for column in (cursor.description or ())
            )
            rows = cursor.fetchall() if limit is None else cursor.fetchmany(limit)
        except WorkerRefusal:
            raise
        except duckdb.Error as error:
            reason = self._interrupted_reason or f"engine error: {str(error)[:120]}"
            raise WorkerRefusal(RESOURCE_REFUSAL_CODE, reason) from error
        return description, rows

    def registered_tables(self) -> dict[str, int]:
        """The admitted tables and their row counts, for the attempt receipt."""
        return {name: rows[0] for name, rows in self._registered.items()}

    def interrupt_reason(self) -> str | None:
        return self._interrupted_reason

    def interrupt(self) -> None:
        """Truthful cooperative stop: the engine call raises inside execute_sql."""
        try:
            self._con.interrupt()
        except Exception:  # noqa: BLE001, S110 - interrupt is best-effort; watchdog records state
            pass

    def close(self) -> dict[str, Any]:
        """Stop the watchdog and release the engine. Returns cleanup evidence."""
        self._watchdog_stop.set()
        if self._watchdog is not None:
            self._watchdog.join(timeout=5)
        try:
            self._con.close()
        except Exception:  # noqa: BLE001, S110 - close is best-effort; cleanup recorded
            pass
        remaining = _tree_size(self._config.temp_directory)
        shutil.rmtree(self._config.temp_directory, ignore_errors=True)
        return {
            "spill_directory_cleaned": True,
            "spill_bytes_at_close": remaining,
            "within_quota": remaining <= self._config.spill_quota_bytes,
            "interrupted": self._interrupted_reason,
        }

    # -- grammar ---------------------------------------------------------------

    def _grammar_check(self, sql: str) -> tuple[exp.Expr, list[Token]]:
        try:
            # The one tokenization and the one parse, exactly what `sqlglot.parse`
            # does; the tokens stay so an alias can be bound to its source `AS`.
            dialect = sqlglot.Dialect.get_or_raise("duckdb")
            tokens = dialect.tokenize(sql)
            statements = dialect.parser().parse(tokens, sql)
        except Exception as error:
            raise WorkerRefusal(
                BOUNDARY_REFUSAL_CODE, f"unparseable statement: {str(error)[:90]}"
            ) from error
        if len(statements) != 1:
            raise WorkerRefusal(
                BOUNDARY_REFUSAL_CODE,
                f"expected exactly one statement, found {len(statements)}",
            )
        parsed = statements[0]
        if parsed is None:
            raise WorkerRefusal(
                BOUNDARY_REFUSAL_CODE, "the statement parsed to nothing"
            )
        if parsed.key != "select":
            raise WorkerRefusal(
                BOUNDARY_REFUSAL_CODE, f"statement root {parsed.key!r} is not a read"
            )
        if parsed.args.get("into") is not None or parsed.args.get("locks") is not None:
            raise WorkerRefusal(
                BOUNDARY_REFUSAL_CODE, "INTO or lock clauses are not admitted"
            )
        if not (parsed.args.get("from_") or parsed.args.get("from")):
            raise WorkerRefusal(
                BOUNDARY_REFUSAL_CODE, "a governed read requires a FROM clause"
            )
        for cte in parsed.find_all(exp.CTE):
            body = cte.this
            if body is None:
                raise WorkerRefusal(BOUNDARY_REFUSAL_CODE, "CTE body is absent")
            body_key = body.key
            if body_key != "select":
                raise WorkerRefusal(
                    BOUNDARY_REFUSAL_CODE, f"CTE body {body_key!r} is not a read"
                )
        for node in parsed.find_all(exp.Table):
            inner = node.this
            if inner is None:
                raise WorkerRefusal(
                    BOUNDARY_REFUSAL_CODE, "table without an inner expression"
                )
            if not isinstance(inner, (exp.Identifier, str)):
                # A table function in FROM: admitted only when the function is
                # in the bounded admitted set (name normalised: read_csv ->
                # readcsv).
                fname = (
                    (getattr(inner, "key", "") or getattr(inner, "name", "") or "")
                    .lower()
                    .replace("_", "")
                )
                if fname not in _ADMITTED_TABLE_FUNCTIONS:
                    raise WorkerRefusal(
                        BOUNDARY_REFUSAL_CODE,
                        f"table function {fname!r} is not an admitted input",
                    )
                continue
            name = getattr(node, "name", "")
            if name and name not in self._registered:
                raise WorkerRefusal(
                    BOUNDARY_REFUSAL_CODE,
                    f"table {name!r} is not an admitted input",
                )
        for function in parsed.find_all(exp.Anonymous):
            fname = (function.name or "").lower()
            if fname in _FORBIDDEN_FUNCTIONS:
                raise WorkerRefusal(
                    BOUNDARY_REFUSAL_CODE, f"function {fname!r} is not admitted"
                )
        for walk_node in parsed.walk():
            key = getattr(walk_node, "key", None)
            if not isinstance(key, str):
                continue
            if key.startswith("read") and key != "read":
                raise WorkerRefusal(
                    BOUNDARY_REFUSAL_CODE,
                    f"reader function {key!r} is not admitted",
                )
        return parsed, tokens


def _tree_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


def open_analysis_worker(config: WorkerBootstrapConfig) -> AnalysisWorker:
    """Bootstrap one worker: registered inputs, closed posture, host watchdog."""
    _ = _detect_filesystem_for_record(config.temp_directory)
    return AnalysisWorker(config)


def _bounded_int(value: object, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high  # never a bool


def _close_quietly(con: duckdb.DuckDBPyConnection) -> None:
    try:
        con.close()
    except Exception:  # noqa: BLE001, S110 - best-effort release on a refused bootstrap
        pass


def _session_is_utc(con: duckdb.DuckDBPyConnection) -> bool:
    row = con.execute("SELECT current_setting('TimeZone')").fetchone()
    return row is not None and len(row) == 1 and type(row[0]) is str and row[0] == "UTC"


def _require_utc_session(con: duckdb.DuckDBPyConnection, *, pin: bool) -> None:
    """Pin (optionally) and verify the exact `UTC` session, or close and refuse."""
    try:
        if pin:
            con.execute("SET TimeZone='UTC'")
        utc = _session_is_utc(con)
    except Exception:  # noqa: BLE001
        utc = False
    if not utc:
        _close_quietly(con)
        raise WorkerRefusal(RESOURCE_REFUSAL_CODE, _REFUSE_UTC)


def _echo_snapshot(echo: object) -> AnalysisExecutionEcho | None:
    """Rebuild the exact Phase A echo once, so later mutation cannot reach the worker."""
    if type(echo) is not AnalysisExecutionEcho:
        return None
    try:
        return replace(echo)
    except Exception:  # noqa: BLE001 - a forged or malformed echo is just refused
        return None


def _admitted_units(units: object) -> bool:
    return (
        type(units) is tuple
        and 1 <= len(units) <= MAX_RESULT_COLUMNS
        and all(
            unit is None or (type(unit) is str and is_identifier(unit))
            for unit in units
        )
    )


def _require_explicit_aliases(parsed: exp.Expr, tokens: list[Token]) -> None:
    """Every computed final projection needs the literal `AS <identifier>`; the
    engine's own label for an expression can look like a valid name and must not
    be trusted. The AST gives `AS x` and a bare `x` the same Alias, so the alias
    identifier's source position is bound to the token just before it: only an
    `AS` token directly ahead of that exact identifier counts (comments are not
    tokens, and a CTE, table or cast `AS` is never that token)."""
    index_by_start = {token.start: index for index, token in enumerate(tokens)}
    for projection in parsed.expressions:
        if isinstance(projection, (exp.Column, exp.Star)):
            continue
        if isinstance(projection, exp.Alias) and is_identifier(projection.alias):
            if isinstance(projection.this, exp.Column):
                continue  # a renamed direct column: the cursor name is the alias
            start = projection.args["alias"].meta.get("start")
            index = index_by_start.get(start) if type(start) is int else None
            if index and tokens[index - 1].token_type == TokenType.ALIAS:
                continue
        raise WorkerRefusal(BOUNDARY_REFUSAL_CODE, _REFUSE_SCHEMA)


def _result_columns(
    description: tuple[tuple[object, str], ...], units: tuple[str | None, ...]
) -> tuple[AnalysisResultColumn, ...] | None:
    """Phase A columns from the cursor's metadata and ordinal units, or None."""
    if not 1 <= len(description) <= MAX_RESULT_COLUMNS or len(units) != len(
        description
    ):
        return None
    names = [name for name, _ in description]
    if not all(type(name) is str and is_identifier(name) for name in names):
        return None
    if len({str(name).casefold() for name in names}) != len(names):
        return None
    columns: list[AnalysisResultColumn] = []
    try:
        for (name, type_text), unit in zip(description, units, strict=True):
            logical = _COLUMN_TYPES.get(type_text)
            precision = scale = None
            if logical is None:
                decimal = _DECIMAL_TYPE.fullmatch(type_text)
                if decimal is None:
                    return None
                logical = LOGICAL_DECIMAL
                precision, scale = int(decimal[1]), int(decimal[2])
            columns.append(
                AnalysisResultColumn(str(name), logical, True, precision, scale, unit)
            )
    except AnalysisResultArtifactRefused:
        return None
    return tuple(columns)


def _is_unsafe_directory(info: os.stat_result) -> bool:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or bool(getattr(info, "st_file_attributes", 0) & reparse)
    )


def _lstat_directory(path: Path) -> os.stat_result | None:
    """The directory's own lstat, None when absent; a link, reparse point or
    non-directory is a boundary refusal and is never followed."""
    info: os.stat_result | None = None
    unsafe = False
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        unsafe = True
    if unsafe or info is None or _is_unsafe_directory(info):
        raise WorkerRefusal(BOUNDARY_REFUSAL_CODE, _REFUSE_TOPOLOGY)
    return info


def _prepare_attempt_directory(path: Path) -> None:
    """lstat, then create owner-private if absent, then lstat again."""
    if _lstat_directory(path) is None:
        path.mkdir(mode=_PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
        if _lstat_directory(path) is None:
            raise WorkerRefusal(BOUNDARY_REFUSAL_CODE, _REFUSE_TOPOLOGY)


def _open_verified_directory(path: Path, before: os.stat_result) -> int:
    """POSIX: an fd on the very directory lstat saw, owner-private, or a refusal.

    Resource or I/O pressure is the write refusal; an unsafe, missing or replaced
    directory and any permission or mode problem is the boundary refusal.
    """
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    safe = False
    resource = False
    try:
        fd = os.open(path, flags)
        info = os.fstat(fd)
        safe = (
            stat.S_ISDIR(info.st_mode)
            and (info.st_dev, info.st_ino) == (before.st_dev, before.st_ino)
            and info.st_uid == os.geteuid()
        )
        if safe and stat.S_IMODE(info.st_mode) != _PRIVATE_DIRECTORY_MODE:
            os.fchmod(fd, _PRIVATE_DIRECTORY_MODE)
            safe = stat.S_IMODE(os.fstat(fd).st_mode) == _PRIVATE_DIRECTORY_MODE
    except OSError as error:
        safe = False
        resource = error.errno in _RESOURCE_ERRNOS
    if not safe:
        _close_directory_fd(fd if fd >= 0 else None)  # the refusal below stands
        if resource:
            raise WorkerRefusal(RESOURCE_REFUSAL_CODE, _REFUSE_WRITE)
        raise WorkerRefusal(BOUNDARY_REFUSAL_CODE, _REFUSE_TOPOLOGY)
    return fd


def _stage_leaf(directory: Path, data: bytes, check: Callable[[], None]) -> None:
    """Create the one fixed leaf exclusively and write `data`, or refuse.

    The directory is re-lstat immediately before staging. Where the platform
    allows, the leaf is created relative to a verified directory fd so a parent
    swap cannot redirect it. An existing object of any kind is a boundary refusal
    and is kept. Once the exclusive create succeeds nothing here ever mutates the
    pathname again: no portable API unlinks a name only if it still identifies one
    inode, so a check-then-unlink could delete a foreign object swapped in between.
    Every later refusal closes its descriptors and retains whatever occupies the
    fixed name, which is non-authoritative; only a returned stage is authoritative.
    On Windows the leaf is created by full path with CREATE_NEW, which closes the
    leaf race but establishes no parent-directory DACL or private-directory
    guarantee; attempt-directory trust on Windows is a separate, open question.
    """
    before = _lstat_directory(directory)
    if before is None:
        raise WorkerRefusal(BOUNDARY_REFUSAL_CODE, _REFUSE_TOPOLOGY)
    dir_fd = (
        _open_verified_directory(directory, before)
        if os.open in os.supports_dir_fd
        else None
    )
    try:
        _create_leaf(directory, dir_fd, data, check)
    except BaseException:
        _close_directory_fd(dir_fd)  # best-effort: the refusal in flight stands
        raise
    if not _close_directory_fd(dir_fd):
        raise WorkerRefusal(RESOURCE_REFUSAL_CODE, _REFUSE_WRITE)
    check()  # the last success boundary: the close itself can outlast the deadline


def _close_directory_fd(dir_fd: int | None) -> bool:
    """Close the directory descriptor (never the result leaf's); False if the close failed."""
    if dir_fd is None:
        return True
    try:
        os.close(dir_fd)
    except OSError:
        return False
    return True


@dataclass(frozen=True)
class _Win32Api:
    """The native calls the fixed leaf needs: real on Windows, injected in tests."""

    create_file: Callable[..., int | None]
    close_handle: Callable[[int], object]
    last_error: Callable[[], int]
    open_osfhandle: Callable[[int, int], int]


def _win32_api() -> _Win32Api:
    """kernel32 and the CRT, loaded only on Windows; no other host imports them."""
    if sys.platform != "win32":
        raise WorkerRefusal(BOUNDARY_REFUSAL_CODE, _REFUSE_TOPOLOGY)
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    return _Win32Api(
        create_file=create_file,
        close_handle=close_handle,
        last_error=ctypes.get_last_error,
        open_osfhandle=msvcrt.open_osfhandle,
    )


def _create_native_leaf(path: str, api: _Win32Api) -> int:
    """Atomically create the leaf with CREATE_NEW and return its CRT descriptor, or refuse.

    The create never follows a link (the reparse point is opened, so an existing
    file, link or dangling link fails CREATE_NEW). Once the handle is native, the
    CRT descriptor owns it and `os.close` is the only close; if the conversion
    fails the handle is closed here, once, and the refusal stands.
    """
    handle = api.create_file(
        path, _WIN_GENERIC_WRITE, 0, None, _WIN_CREATE_NEW, _WIN_CREATE_FLAGS, None
    )
    if handle in (None, 0, _WIN_INVALID_HANDLE):
        # First call after the failed create: nothing runs before the error is read.
        code = api.last_error()
        if code in _WIN_RESOURCE_CODES:
            raise WorkerRefusal(RESOURCE_REFUSAL_CODE, _REFUSE_WRITE)
        raise WorkerRefusal(BOUNDARY_REFUSAL_CODE, _REFUSE_TOPOLOGY)
    try:
        fd = api.open_osfhandle(handle, _WIN_CRT_FLAGS)
    except BaseException:  # noqa: BLE001 - audit hooks may raise any BaseException; the handle is closed below
        fd = -1
    if fd == -1:
        try:
            api.close_handle(handle)  # the only close of this handle
        except BaseException:  # noqa: BLE001, S110 - the refusal below stands even if this fails
            pass
        raise WorkerRefusal(RESOURCE_REFUSAL_CODE, _REFUSE_WRITE)
    return fd


def _acquire_leaf(directory: Path, dir_fd: int | None) -> int:
    """Exclusively create the fixed leaf and return its open descriptor, or refuse.

    POSIX creates it relative to the verified directory fd with O_NOFOLLOW. Windows
    uses the native atomic create above. Neither path ever stats or unlinks the name.
    """
    if sys.platform == "win32":
        return _create_native_leaf(os.fspath(directory / _RESULT_LEAF), _win32_api())
    target = _RESULT_LEAF if dir_fd is not None else os.fspath(directory / _RESULT_LEAF)
    flags = (
        os.O_CREAT
        | os.O_EXCL
        | os.O_WRONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        return os.open(target, flags, _PRIVATE_FILE_MODE, dir_fd=dir_fd)
    except OSError as error:
        # Resource or I/O pressure is a write refusal; an existing leaf (file,
        # link or broken link), an unsafe or vanished directory and any mode
        # problem is the boundary refusal. Nothing is ours either way.
        if error.errno in _RESOURCE_ERRNOS:
            raise WorkerRefusal(RESOURCE_REFUSAL_CODE, _REFUSE_WRITE) from error
        raise WorkerRefusal(BOUNDARY_REFUSAL_CODE, _REFUSE_TOPOLOGY) from error


def _create_leaf(
    directory: Path, dir_fd: int | None, data: bytes, check: Callable[[], None]
) -> None:
    """Acquire and fill the leaf, or refuse; a refusal after acquisition retains the leaf."""
    fd = _acquire_leaf(directory, dir_fd)
    # The leaf now exists. Every failure below closes `fd` and refuses without
    # touching the name: the retained object may be partial, complete or foreign,
    # and is non-authoritative. A retry must use a fresh attempt directory.
    failure: WorkerRefusal | None = None
    try:
        _fill_leaf(fd, data, check)
    except WorkerRefusal as refusal:
        failure = refusal
    except OSError:
        failure = WorkerRefusal(RESOURCE_REFUSAL_CODE, _REFUSE_WRITE)
    try:
        os.close(fd)
    except OSError:
        failure = failure or WorkerRefusal(RESOURCE_REFUSAL_CODE, _REFUSE_WRITE)
    if failure is None:
        try:
            check()  # the last safe success boundary: a late deadline still refuses
        except WorkerRefusal as refusal:
            failure = refusal
    if failure is not None:
        raise failure


def _fill_leaf(fd: int, data: bytes, check: Callable[[], None]) -> None:
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        check()
        written = os.write(fd, view[offset:])
        if written < 1:
            raise OSError("no progress writing the result leaf")
        offset += written
    check()
    if hasattr(os, "fchmod"):
        os.fchmod(fd, _PRIVATE_FILE_MODE)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or (
        os.name == "posix" and stat.S_IMODE(info.st_mode) != _PRIVATE_FILE_MODE
    ):
        raise WorkerRefusal(BOUNDARY_REFUSAL_CODE, _REFUSE_TOPOLOGY)
    if info.st_size != len(data):
        raise OSError("result leaf size differs from the bytes written")
    os.fsync(fd)
    check()  # a watchdog interruption that landed during the durable write
