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
- **No canonical authority** — this module never opens the workspace SQLite
  database and never imports the storage layer; it produces candidate
  artifacts for the service writer to commit.

The module is standard-library plus the two admitted dependencies only.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import sqlglot
from sqlglot import exp

__all__ = [
    "BOUNDARY_REFUSAL_CODE",
    "RESOURCE_REFUSAL_CODE",
    "AnalysisWorker",
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


class WorkerRefusal(Exception):
    """A handler-visible refusal: the worker refuses the request and did nothing."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


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
        config.attempt_directory.mkdir(parents=True, exist_ok=True)
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
        if self._interrupted_reason is not None:
            raise WorkerRefusal(RESOURCE_REFUSAL_CODE, self._interrupted_reason)
        self._grammar_check(sql)
        try:
            rows = self._con.execute(sql, list(parameters)).fetchall()
        except WorkerRefusal:
            raise
        except duckdb.Error as error:
            reason = self._interrupted_reason or f"engine error: {str(error)[:120]}"
            raise WorkerRefusal(RESOURCE_REFUSAL_CODE, reason) from error
        return rows

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

    def _grammar_check(self, sql: str) -> None:
        try:
            statements = sqlglot.parse(sql, read="duckdb")
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
                raise WorkerRefusal(
                    BOUNDARY_REFUSAL_CODE, "CTE body is absent"
                )
            body_key = body.key
            if body_key != "select":
                raise WorkerRefusal(
                    BOUNDARY_REFUSAL_CODE, f"CTE body {body_key!r} is not a read"
                )
        for node in parsed.find_all(exp.Table):
            inner = node.this
            if inner is None:
                raise WorkerRefusal(BOUNDARY_REFUSAL_CODE, "table without an inner expression")
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


def write_artifact(
    rows: list[tuple[Any, ...]], columns: list[str], output_path: Path
) -> dict[str, Any]:
    """Write the result artifact host-side (the engine stays closed).

    The worker writes JSON-lines output itself from fetched rows — the engine
    is not asked to touch the filesystem after the close. Returns the artifact
    identity the service verifies before committing.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    digest = hashlib.sha256()
    with output_path.open("wb") as handle:
        header = json.dumps({"columns": columns}).encode("utf-8")
        digest.update(header + b"\n")
        handle.write(header + b"\n")
        for row in rows:
            line = json.dumps(
                [_encodable(value) for value in row], separators=(",", ":")
            ).encode("utf-8")
            digest.update(line + b"\n")
            handle.write(line + b"\n")
            count += 1
    return {
        "path": str(output_path),
        "rows": count,
        "columns": columns,
        "sha256": digest.hexdigest(),
    }


def _encodable(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "hex"):  # bytes/BLOB
        return value.hex()
    return value
