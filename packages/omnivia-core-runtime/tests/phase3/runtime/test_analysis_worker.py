"""WP03 restricted-worker evidence (SPEC-CORE-DATA-001 §17, Q20/Q21/Q39/Q40).

The worker is exercised as the acceptance conditions require: trusted
bootstrap ordering on the exact admitted release, the closed external-access
posture re-verified, the fail-closed grammar, the host watchdog as the binding
control (Q39d/F-5 compensating controls), and truthful cancellation.
"""

from __future__ import annotations

import ctypes
import dataclasses
import errno
import hashlib
import inspect
import json
import os
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest
import pytz
from omnivia_core_runtime.analysis import result_artifact
from omnivia_core_runtime.analysis import worker as worker_module
from omnivia_core_runtime.analysis.result_artifact import (
    MAX_RESULT_BYTES_CEILING,
    MAX_RESULT_ROWS_CEILING,
    AnalysisExecutionEcho,
    AnalysisResultColumn,
    encode_analysis_result_artifact,
)
from omnivia_core_runtime.analysis.worker import (
    BOUNDARY_REFUSAL_CODE,
    RESOURCE_REFUSAL_CODE,
    AnalysisWorker,
    StagedAnalysisResult,
    WorkerBootstrapConfig,
    WorkerRefusal,
    open_analysis_worker,
)

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
DIGEST_D = "sha256:" + "d" * 64
ECHO = AnalysisExecutionEcho(
    workspace_id="ws-1",
    run_id="run-1",
    run_step_id="step.1",
    attempt_id="attempt:1",
    plan_digest=DIGEST_A,
    parameters_digest=DIGEST_B,
    final_sql_digest=DIGEST_C,
    input_vector_digest=DIGEST_D,
)
LEAF = "analysis-result.json"

INVOICES = [
    {
        "invoice_id": 101,
        "customer_id": 1,
        "amount_cents": 50000,
        "status": "overdue",
        "currency": "AUD",
    },
    {
        "invoice_id": 102,
        "customer_id": 1,
        "amount_cents": 70000,
        "status": "overdue",
        "currency": "AUD",
    },
    {
        "invoice_id": 103,
        "customer_id": 2,
        "amount_cents": 30000,
        "status": "overdue",
        "currency": "AUD",
    },
]
PROJECTS = [
    {"project_id": 201, "customer_id": 1, "is_active": True},
    {"project_id": 202, "customer_id": 1, "is_active": True},
    {"project_id": 203, "customer_id": 1, "is_active": True},
    {"project_id": 204, "customer_id": 2, "is_active": False},
]


@pytest.fixture()
def config(tmp_path: Path) -> WorkerBootstrapConfig:
    inputs_dir = tmp_path / "inputs"
    inputs_dir.mkdir()
    invoices = inputs_dir / "invoices.json"
    projects = inputs_dir / "projects.json"
    invoices.write_text(json.dumps(INVOICES))
    projects.write_text(json.dumps(PROJECTS))
    return WorkerBootstrapConfig(
        inputs=(("invoices", invoices), ("projects", projects)),
        memory_limit="512MB",
        temp_directory=tmp_path / "spill",
        spill_quota_bytes=16 * 1024 * 1024,
        wall_time_seconds=20,
        attempt_directory=tmp_path / "attempt",
    )


# ---------------------------------------------------------------------------
# Q39: trusted bootstrap ordering on the exact release
# ---------------------------------------------------------------------------


def test_q39_bootstrap_registers_inputs_then_closes_external_access(
    config: WorkerBootstrapConfig,
) -> None:
    worker = open_analysis_worker(config)
    try:
        # The registered inputs are queryable through the closed posture.
        rows = worker.execute_sql(
            "SELECT count(*) FROM invoices WHERE status = 'overdue' AND currency = 'AUD'"
        )
        assert rows[0][0] == 3
        assert worker.registered_tables() == {"invoices": 3, "projects": 4}
    finally:
        worker.close()


def test_q39_external_access_cannot_reopen(config: WorkerBootstrapConfig) -> None:
    worker = open_analysis_worker(config)
    try:
        with pytest.raises(WorkerRefusal) as raised:
            worker.execute_sql("SET enable_external_access = true")
        assert raised.value.code == BOUNDARY_REFUSAL_CODE
        assert "SET" in raised.value.reason or "statement root" in raised.value.reason
    finally:
        worker.close()


# ---------------------------------------------------------------------------
# Q20: unapproved paths, extensions, and out-of-grammar surfaces
# ---------------------------------------------------------------------------


def test_q20_file_functions_are_refused(config: WorkerBootstrapConfig) -> None:
    worker = open_analysis_worker(config)
    try:
        with pytest.raises(WorkerRefusal) as raised:
            worker.execute_sql("SELECT * FROM read_csv('/etc/passwd')")
        assert raised.value.code == BOUNDARY_REFUSAL_CODE
        assert "readcsv" in raised.value.reason
        with pytest.raises(WorkerRefusal):
            worker.execute_sql("SELECT * FROM read_parquet('/etc/passwd')")
    finally:
        worker.close()


def test_q20_extension_and_attach_statements_are_refused(
    config: WorkerBootstrapConfig,
) -> None:
    worker = open_analysis_worker(config)
    try:
        for sql in (
            "LOAD httpfs",
            "INSTALL httpfs",
            "ATTACH 'x.db' AS other",
            "PRAGMA enable_profiling",
        ):
            with pytest.raises(WorkerRefusal) as raised:
                worker.execute_sql(sql)
            assert raised.value.code == BOUNDARY_REFUSAL_CODE
    finally:
        worker.close()


def test_q20_unknown_tables_and_unparsable_input_are_refused(
    config: WorkerBootstrapConfig,
) -> None:
    worker = open_analysis_worker(config)
    try:
        with pytest.raises(WorkerRefusal) as raised:
            worker.execute_sql("SELECT * FROM secret_table")
        assert "not an admitted input" in raised.value.reason
        with pytest.raises(WorkerRefusal):
            worker.execute_sql("SELECT (")
        with pytest.raises(WorkerRefusal) as raised:
            worker.execute_sql("SELECT 1; DELETE FROM invoices")
        assert "exactly one statement" in raised.value.reason
    finally:
        worker.close()


def test_q20_mutating_cte_bodies_are_refused(config: WorkerBootstrapConfig) -> None:
    worker = open_analysis_worker(config)
    try:
        with pytest.raises(WorkerRefusal) as raised:
            worker.execute_sql(
                "WITH del AS (DELETE FROM invoices RETURNING *) SELECT 1 FROM del"
            )
        assert raised.value.code == BOUNDARY_REFUSAL_CODE
    finally:
        worker.close()


# ---------------------------------------------------------------------------
# Q21/Q40: the host watchdog is the binding control
# ---------------------------------------------------------------------------


def test_q21_wall_time_is_enforced_by_the_host(config: WorkerBootstrapConfig) -> None:
    tight = WorkerBootstrapConfig(
        inputs=config.inputs,
        memory_limit=config.memory_limit,
        temp_directory=config.temp_directory,
        spill_quota_bytes=config.spill_quota_bytes,
        wall_time_seconds=2,
        attempt_directory=config.attempt_directory,
    )
    worker = open_analysis_worker(tight)
    try:
        start = time.monotonic()
        with pytest.raises(WorkerRefusal) as raised:
            worker.execute_sql(
                "SELECT count(DISTINCT a.generate_series) FROM generate_series(30000000) a, "
                "generate_series(1000000) b"
            )
        elapsed = time.monotonic() - start
        assert raised.value.code == RESOURCE_REFUSAL_CODE
        assert worker.interrupt_reason() == "wall_time_exceeded"
        assert elapsed < 15  # the watchdog, not the query, ended it
    finally:
        worker.close()


def test_q21_spill_quota_is_enforced_by_the_host(
    config: WorkerBootstrapConfig, tmp_path: Path
) -> None:
    tiny = WorkerBootstrapConfig(
        inputs=config.inputs,
        memory_limit="64MB",  # big enough to spill, small enough to force it
        temp_directory=tmp_path / "spill-tiny",
        spill_quota_bytes=1,  # one byte: any spill exceeds it
        wall_time_seconds=30,
        attempt_directory=tmp_path / "attempt-tiny",
    )
    worker = open_analysis_worker(tiny)
    try:
        with pytest.raises(WorkerRefusal) as raised:
            worker.execute_sql(
                "SELECT list(generate_series) FROM generate_series(30000000)"
            )
        # A bounded outcome by construction: either the host watchdog
        # interrupts (quota/wall-time, with the reason recorded), or the
        # engine refuses the allocation itself (an atomic list cannot spill
        # — the same class of bound the D04 F-5 evidence recorded). Both are
        # bounded failures, never silent over-consumption.
        assert raised.value.code == RESOURCE_REFUSAL_CODE
        assert worker.interrupt_reason() in (
            None,
            "spill_quota_exceeded",
            "wall_time_exceeded",
        )
    finally:
        evidence = worker.close()
        assert evidence["within_quota"] is True


def test_close_cleans_the_spill_directory(config: WorkerBootstrapConfig) -> None:
    worker = open_analysis_worker(config)
    evidence = worker.close()
    assert evidence["spill_directory_cleaned"] is True
    assert not config.temp_directory.exists()


# ---------------------------------------------------------------------------
# The Q14 golden calculation through the worker
# ---------------------------------------------------------------------------


def test_q14_golden_value_through_the_worker(
    config: WorkerBootstrapConfig, tmp_path: Path
) -> None:
    worker = open_analysis_worker(config)
    sql = """
        SELECT COALESCE(SUM(i.amount_cents), 0) AS overdue_exposure_cents
        FROM invoices i
        WHERE i.status = 'overdue' AND i.currency = 'AUD'
          AND EXISTS (SELECT 1 FROM projects p
                      WHERE p.customer_id = i.customer_id AND p.is_active)
          AND i.customer_id = 1
        """
    try:
        rows = worker.execute_sql(sql)
        assert rows[0][0] == 120000  # the golden fanout value, not 360000
        staged = worker.execute_result_artifact(
            sql,
            echo=ECHO,
            units=("currency:AUD",),
            max_rows=10,
            max_bytes=10_000,
        )
    finally:
        worker.close()
    assert staged.handle == "analysis-result.json"
    leaf = tmp_path / "attempt" / "analysis-result.json"
    assert sorted(path.name for path in leaf.parent.iterdir()) == [leaf.name]
    assert leaf.read_bytes() == staged.candidate.artifact_bytes
    expected = encode_analysis_result_artifact(
        (
            AnalysisResultColumn(
                "overdue_exposure_cents", "integer", True, None, None, "currency:AUD"
            ),
        ),
        [(120000,)],
        echo=ECHO,
        max_rows=10,
        max_bytes=10_000,
    )
    candidate = staged.candidate
    assert candidate.artifact_bytes == expected.artifact_bytes
    assert candidate.artifact_digest == expected.artifact_digest
    assert candidate.schema_digest == expected.schema_digest
    assert (
        candidate.artifact_digest
        == "sha256:" + hashlib.sha256(leaf.read_bytes()).hexdigest()
    )
    assert candidate.row_count == 1
    document = json.loads(leaf.read_bytes())
    assert document["rows"] == [["120000"]]
    assert document["schema"]["columns"][0]["name"] == "overdue_exposure_cents"
    assert document["schema"]["columns"][0]["unit"] == "currency:AUD"


def test_the_worker_module_never_touches_canonical_storage() -> None:
    """Structural: the worker imports no storage module and opens no sqlite."""
    import omnivia_core_runtime.analysis.worker as worker_module

    source = worker_module.__file__
    assert source is not None
    text = Path(source).read_text(encoding="utf-8")
    assert "sqlite3" not in text
    assert "storage.connection" not in text
    assert "omnivia_core_runtime.storage" not in text


# ---------------------------------------------------------------------------
# WP07 Phase B: UTC session, execute-once and the staged result artifact
# ---------------------------------------------------------------------------

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX modes and dir_fd")
posix_os_open_seam = pytest.mark.skipif(
    os.name != "posix",
    reason="drives the POSIX os.open leaf path; Windows creates the leaf via CreateFileW",
)
open_leaf_write_posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="Windows denies deleting or replacing the leaf while it is open for writing",
)


class SpyConnection:
    """Delegates to the real engine connection and records exactly what is asked of it."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.executed: list[str] = []
        self.calls: list[tuple[Any, ...]] = []
        self.description_override: Any = None
        self.rows_override: Any = None
        self.fetch_error: Exception | None = None
        self.on_fetch: Callable[[], None] | None = None

    def execute(self, sql: str, params: Any = None) -> SpyConnection:
        self.executed.append(sql)
        self.inner.execute(sql, params)
        return self

    @property
    def description(self) -> Any:
        if self.description_override is not None:
            return self.description_override
        return self.inner.description

    def _rows(self, rows: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
        if self.fetch_error is not None:
            raise self.fetch_error
        if self.on_fetch is not None:
            self.on_fetch()
        return self.rows_override if self.rows_override is not None else rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        self.calls.append(("fetchall",))
        return self._rows(self.inner.fetchall())

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        self.calls.append(("fetchmany", size))
        return self._rows(self.inner.fetchmany(size))

    def interrupt(self) -> None:
        self.inner.interrupt()

    def close(self) -> None:
        self.inner.close()


@pytest.fixture()
def worker(config: WorkerBootstrapConfig) -> Iterator[AnalysisWorker]:
    opened = open_analysis_worker(config)
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture()
def spy(worker: AnalysisWorker) -> SpyConnection:
    connection = SpyConnection(worker._con)
    worker._con = connection  # type: ignore[assignment]
    return connection


def stage(worker: AnalysisWorker, sql: str, **overrides: Any) -> StagedAnalysisResult:
    arguments: dict[str, Any] = {
        "echo": ECHO,
        "units": (None,),
        "max_rows": 10,
        "max_bytes": 100_000,
    }
    arguments.update(overrides)
    return worker.execute_result_artifact(sql, **arguments)


def refused(code: str, call: Callable[[], object]) -> WorkerRefusal:
    with pytest.raises(WorkerRefusal) as raised:
        call()
    assert raised.value.code == code
    return raised.value


def attempt_leaf(config: WorkerBootstrapConfig) -> Path:
    return config.attempt_directory / LEAF


def test_execute_sql_keeps_its_list_shape_and_runs_once(
    worker: AnalysisWorker, spy: SpyConnection
) -> None:
    rows = worker.execute_sql("SELECT invoice_id FROM invoices ORDER BY 1")
    assert rows == [(101,), (102,), (103,)]
    assert type(rows) is list
    assert spy.executed == ["SELECT invoice_id FROM invoices ORDER BY 1"]
    assert spy.calls == [("fetchall",)]


def test_artifact_path_fetches_one_sentinel_and_never_fetchall(
    worker: AnalysisWorker, spy: SpyConnection, config: WorkerBootstrapConfig
) -> None:
    sql = "SELECT range AS n FROM range(3)"
    staged = stage(worker, sql, max_rows=3)
    assert spy.executed == [sql]
    assert spy.calls == [("fetchmany", 4)]
    assert staged.candidate.row_count == 3
    spy.executed.clear()
    spy.calls.clear()
    leaf = attempt_leaf(config)
    leaf.unlink()
    refusal = refused(RESOURCE_REFUSAL_CODE, lambda: stage(worker, sql, max_rows=2))
    assert spy.executed == [sql]
    assert spy.calls == [("fetchmany", 3)]  # the sentinel row refuses before any file
    assert not leaf.exists()
    assert refusal.reason == "the result exceeds its admitted bounds"


def test_zero_rows_are_a_whole_result_when_the_bound_allows_it(
    worker: AnalysisWorker, spy: SpyConnection
) -> None:
    staged = stage(worker, "SELECT invoice_id FROM invoices WHERE false", max_rows=0)
    assert spy.calls == [("fetchmany", 1)]
    assert staged.candidate.row_count == 0


def test_every_admitted_type_maps_from_the_real_cursor(
    worker: AnalysisWorker, config: WorkerBootstrapConfig
) -> None:
    sql = (
        "SELECT true AS b, 1::TINYINT AS ti, 2::SMALLINT AS si, 3::INTEGER AS i, "
        "4::BIGINT AS bi, 5::HUGEINT AS hi, 1.5::FLOAT AS f, 2.25::DOUBLE AS d, "
        "12.345::DECIMAL(18,3) AS dec, 'x'::VARCHAR AS s, 'abc'::BLOB AS bl, "
        "DATE '2024-02-29' AS dt, TIMESTAMPTZ '2024-11-03 01:30:00-04' AS ts, "
        "NULL::INTEGER AS nothing FROM range(1)"
    )
    staged = stage(worker, sql, units=(None,) * 14)
    document = json.loads(staged.candidate.artifact_bytes)
    columns = document["schema"]["columns"]
    assert [(c["name"], c["logical_type"]) for c in columns] == [
        ("b", "boolean"),
        ("ti", "integer"),
        ("si", "integer"),
        ("i", "integer"),
        ("bi", "integer"),
        ("hi", "integer"),
        ("f", "float"),
        ("d", "float"),
        ("dec", "decimal"),
        ("s", "string"),
        ("bl", "bytes"),
        ("dt", "date"),
        ("ts", "timestamptz"),
        ("nothing", "integer"),
    ]
    assert all(c["nullable"] is True for c in columns)
    assert (columns[8]["precision"], columns[8]["scale"]) == (18, 3)
    assert document["rows"] == [
        [
            True,
            "1",
            "2",
            "3",
            "4",
            "5",
            "1.5",
            "2.25",
            "12.345",
            "x",
            "YWJj",
            "2024-02-29",
            "2024-11-03T05:30:00.000000Z",
            None,
        ]
    ]
    assert attempt_leaf(config).read_bytes() == staged.candidate.artifact_bytes


def test_the_utc_session_returns_the_captured_pytz_singleton(
    worker: AnalysisWorker,
) -> None:
    setting = worker.execute_sql("SELECT current_setting('TimeZone') FROM range(1)")
    assert setting == [("UTC",)] and type(setting[0][0]) is str
    value = worker.execute_sql(
        "SELECT TIMESTAMPTZ '2024-11-03 01:30:00-04' FROM range(1)"
    )
    assert value[0][0].tzinfo is pytz.UTC
    assert value[0][0].tzinfo is result_artifact._PYTZ_UTC


def test_both_fallback_instants_encode_as_distinct_utc_instants(
    worker: AnalysisWorker,
) -> None:
    staged = stage(
        worker,
        "SELECT TIMESTAMPTZ '2024-11-03 01:30:00-04' AS first_pass, "
        "TIMESTAMPTZ '2024-11-03 01:30:00-05' AS second_pass FROM range(1)",
        units=(None, None),
    )
    assert json.loads(staged.candidate.artifact_bytes)["rows"] == [
        ["2024-11-03T05:30:00.000000Z", "2024-11-03T06:30:00.000000Z"]
    ]


def test_a_later_set_time_zone_is_refused_by_the_grammar(
    worker: AnalysisWorker,
) -> None:
    refusal = refused(
        BOUNDARY_REFUSAL_CODE,
        lambda: worker.execute_sql("SET TimeZone='Australia/Sydney'"),
    )
    assert "SET" in refusal.reason or "statement root" in refusal.reason
    assert worker.execute_sql("SELECT current_setting('TimeZone') FROM range(1)") == [
        ("UTC",)
    ]


@pytest.mark.parametrize("passes", [0, 1])
def test_a_failed_utc_verification_closes_the_engine_and_refuses(
    config: WorkerBootstrapConfig, monkeypatch: pytest.MonkeyPatch, passes: int
) -> None:
    connections: list[Any] = []
    real_connect = duckdb.connect

    def connect(*args: Any, **kwargs: Any) -> Any:
        connections.append(real_connect(*args, **kwargs))
        return connections[-1]

    verifications = iter([True] * passes)
    monkeypatch.setattr(duckdb, "connect", connect)
    monkeypatch.setattr(
        worker_module, "_session_is_utc", lambda _con: next(verifications, False)
    )
    refusal = refused(RESOURCE_REFUSAL_CODE, lambda: open_analysis_worker(config))
    assert refusal.reason == "the engine session time zone is not UTC"
    with pytest.raises(duckdb.Error):
        connections[0].execute("SELECT 1")  # best-effort close happened


def test_non_utc_time_zone_text_is_not_accepted() -> None:
    class Text(str):
        pass

    class Connection:
        def execute(self, _sql: str) -> Connection:
            return self

        def fetchone(self) -> tuple[object]:
            return (Text("UTC"),)

    assert worker_module._session_is_utc(Connection()) is False  # type: ignore[arg-type]


REJECTED_TYPE_SQL = [
    "1::UTINYINT",
    "1::USMALLINT",
    "1::UINTEGER",
    "1::UBIGINT",
    "1::UHUGEINT",
    "uuid()",
    "'{}'::JSON",
    "TIMESTAMP '2024-01-01 00:00:00'",
    "TIME '01:02:03'",
    "INTERVAL 1 DAY",
    "[1, 2]",
    "{'a': 1}",
    "MAP {'a': 1}",
    "TIMESTAMP_S '2024-01-01 00:00:00'",
]


@pytest.mark.parametrize("expression", REJECTED_TYPE_SQL)
def test_rejected_types_fail_closed_after_the_one_fetch(
    worker: AnalysisWorker,
    spy: SpyConnection,
    config: WorkerBootstrapConfig,
    expression: str,
) -> None:
    sql = f"SELECT {expression} AS x FROM range(1)"
    refused(BOUNDARY_REFUSAL_CODE, lambda: stage(worker, sql))
    assert spy.calls == [
        ("fetchmany", 11)
    ]  # past the parse, refused on cursor metadata
    assert not attempt_leaf(config).exists()


@pytest.mark.parametrize(
    "type_text",
    [
        "UTINYINT",
        "UHUGEINT",
        "UUID",
        "JSON",
        "TIMESTAMP",
        "TIMESTAMP_S",
        "TIMESTAMP_NS",
        "TIME",
        "TIME WITH TIME ZONE",
        "INTERVAL",
        "ENUM('a', 'b')",
        "INTEGER[]",
        "STRUCT(a INTEGER)",
        "MAP(VARCHAR, INTEGER)",
        "DECIMAL(39,2)",
        "DECIMAL(5,6)",
        "DECIMAL(05,2)",
        "DECIMAL(5,02)",
        "DECIMAL(0,0)",
        "decimal(5,2)",
        "DECIMAL(5,2) ",
        "BIT",
        "VARINT",
        "SOMETHING_NEW",
        "",
    ],
)
def test_the_type_vocabulary_is_closed(type_text: str) -> None:
    assert worker_module._result_columns((("x", type_text),), (None,)) is None


def test_the_type_vocabulary_admits_decimal_precision_and_scale() -> None:
    columns = worker_module._result_columns(
        (("x", "DECIMAL(38,38)"), ("y", "DECIMAL(1,0)")), (None, "currency:AUD")
    )
    assert columns is not None
    assert [(c.precision, c.scale, c.unit) for c in columns] == [
        (38, 38, None),
        (1, 0, "currency:AUD"),
    ]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM invoices",
        "SELECT 1 FROM range(1)",
        "SELECT current_date FROM range(1)",
        "SELECT amount_cents + 1 FROM invoices",
        "SELECT (invoice_id) FROM invoices",
        "SELECT CASE WHEN true THEN 1 ELSE 2 END FROM range(1)",
        "SELECT 'a' FROM range(1)",
        "SELECT lower(status) FROM invoices",
        "SELECT invoice_id, SUM(amount_cents) AS total, count(*) FROM invoices GROUP BY 1",
        'SELECT 1 AS "bad name" FROM range(1)',
        "SELECT 1 AS _ FROM range(1)",
    ],
)
def test_computed_projections_need_an_explicit_identifier_alias(
    worker: AnalysisWorker, spy: SpyConnection, sql: str
) -> None:
    refused(BOUNDARY_REFUSAL_CODE, lambda: stage(worker, sql, units=(None, None, None)))
    assert spy.executed == []  # refused from the one parse, before the engine ran it


IMPLICIT_ALIAS_SQL = [
    "SELECT 1 total FROM range(1)",
    "SELECT amount_cents + 1 total FROM invoices",
    'SELECT 1 "total" FROM range(1)',
    "SELECT 1 AS a, 2 b FROM range(1)",
    "SELECT 1 /* AS */ total FROM range(1)",
    "SELECT 1 -- AS\n total FROM range(1)",
    "SELECT /* AS */ 1 total FROM range(1)",
    "SELECT CAST(amount_cents AS BIGINT) total FROM invoices",
    "SELECT CAST(amount_cents AS BIGINT) AS cast_ok, 1 total FROM invoices",
    "WITH c AS (SELECT 1 AS x FROM range(1)) SELECT 2 total FROM range(1)",
    "SELECT 1 total FROM (SELECT 1 AS x FROM range(1)) AS t",
    "SELECT 1 total FROM range(1) AS r",
    "SELECT 1 total FROM invoices AS i",
]


@pytest.mark.parametrize("sql", IMPLICIT_ALIAS_SQL)
def test_an_implicit_alias_is_refused_whatever_other_as_tokens_exist(
    worker: AnalysisWorker, spy: SpyConnection, sql: str
) -> None:
    refusal = refused(
        BOUNDARY_REFUSAL_CODE, lambda: stage(worker, sql, units=(None, None, None))
    )
    assert refusal.reason == "the result columns are not admitted"
    assert spy.executed == []


@pytest.mark.parametrize(
    ("sql", "name"),
    [
        ("SELECT 1 AS total FROM range(1)", "total"),
        ("SELECT 1 as total FROM range(1)", "total"),
        ("SELECT 1 AS /* c */ total FROM range(1)", "total"),
        ("SELECT 1 /* AS */ AS total FROM range(1)", "total"),
        ('SELECT 1 AS "total" FROM range(1)', "total"),
        ("SELECT CAST(amount_cents AS BIGINT) AS total FROM invoices", "total"),
        (
            "WITH c AS (SELECT 1 AS x FROM range(1)) SELECT 2 AS total FROM range(1)",
            "total",
        ),
        ("SELECT 1 AS total FROM (SELECT 1 AS x FROM range(1)) AS t", "total"),
        ("SELECT invoice_id id FROM invoices", "id"),  # a renamed direct column
    ],
)
def test_an_explicit_alias_is_accepted_and_names_the_column(
    worker: AnalysisWorker, config: WorkerBootstrapConfig, sql: str, name: str
) -> None:
    staged = stage(worker, sql)
    columns = json.loads(staged.candidate.artifact_bytes)["schema"]["columns"]
    assert [column["name"] for column in columns] == [name]
    os.unlink(attempt_leaf(config))


def test_direct_column_and_star_projections_use_the_cursor_names(
    worker: AnalysisWorker,
) -> None:
    for sql, width in (
        ("SELECT i.invoice_id, customer_id FROM invoices i", 2),
        ("SELECT * FROM projects", 3),
        ("SELECT p.* FROM projects p", 3),
    ):
        staged = stage(worker, sql, units=(None,) * width, max_rows=10)
        document = json.loads(staged.candidate.artifact_bytes)
        assert len(document["schema"]["columns"]) == width
        os.unlink(worker._config.attempt_directory / LEAF)
    names = json.loads(staged.candidate.artifact_bytes)["schema"]["columns"]
    assert [c["name"] for c in names] == ["project_id", "customer_id", "is_active"]
    assert [c["logical_type"] for c in names] == ["integer", "integer", "boolean"]


def test_duplicate_and_invalid_cursor_names_refuse(worker: AnalysisWorker) -> None:
    refused(
        BOUNDARY_REFUSAL_CODE,
        lambda: stage(
            worker, "SELECT 1 AS a, 2 AS A FROM range(1)", units=(None, None)
        ),
    )
    refused(
        BOUNDARY_REFUSAL_CODE,
        lambda: stage(
            worker, "SELECT invoice_id, invoice_id FROM invoices", units=(None, None)
        ),
    )
    refused(
        BOUNDARY_REFUSAL_CODE,
        lambda: stage(worker, 'SELECT "bad name" FROM (SELECT 1 AS "bad name") t'),
    )


def test_pre_sql_authority_and_bounds_are_proven_before_the_engine_runs(
    worker: AnalysisWorker, spy: SpyConnection
) -> None:
    class SubEcho(AnalysisExecutionEcho):
        pass

    forged = object.__new__(AnalysisExecutionEcho)
    sub_echo = object.__new__(SubEcho)
    for field in dataclasses.fields(ECHO):
        object.__setattr__(sub_echo, field.name, getattr(ECHO, field.name))
    sql = "SELECT range AS n FROM range(1)"
    for bad_echo in (None, {}, sub_echo, forged, "echo"):
        refused(BOUNDARY_REFUSAL_CODE, lambda e=bad_echo: stage(worker, sql, echo=e))  # type: ignore[misc]
    for bad_units in (
        [None],
        (),
        (1,),
        ("bad unit",),
        ("a", "b"),
        "currency:AUD",
        None,
    ):
        if bad_units == ("a", "b"):
            continue  # a well-formed tuple: wrong width is proven after the cursor exists
        refused(BOUNDARY_REFUSAL_CODE, lambda u=bad_units: stage(worker, sql, units=u))  # type: ignore[misc]
    refused(BOUNDARY_REFUSAL_CODE, lambda: stage(worker, sql, units=(None,) * 257))
    refused(
        BOUNDARY_REFUSAL_CODE,
        lambda: worker.execute_result_artifact(
            sql, [], echo=ECHO, units=(None,), max_rows=1, max_bytes=999
        ),
    )  # type: ignore[arg-type]
    for bad_rows in (True, 1.0, "1", -1, MAX_RESULT_ROWS_CEILING + 1, None):
        refused(
            RESOURCE_REFUSAL_CODE, lambda r=bad_rows: stage(worker, sql, max_rows=r)
        )  # type: ignore[misc]
    for bad_bytes in (True, 1.0, "1", 0, -1, MAX_RESULT_BYTES_CEILING + 1, None):
        refused(
            RESOURCE_REFUSAL_CODE, lambda b=bad_bytes: stage(worker, sql, max_bytes=b)
        )  # type: ignore[misc]
    assert spy.executed == []


def test_wrong_unit_width_refuses_after_the_cursor_exists(
    worker: AnalysisWorker, config: WorkerBootstrapConfig
) -> None:
    refused(
        BOUNDARY_REFUSAL_CODE,
        lambda: stage(worker, "SELECT range AS n FROM range(1)", units=(None, None)),
    )
    assert not attempt_leaf(config).exists()


@pytest.mark.parametrize(
    "literal", ["'NaN'::DOUBLE", "'Infinity'::DOUBLE", "'-Infinity'::FLOAT"]
)
def test_non_finite_floats_refuse_without_a_file(
    worker: AnalysisWorker, config: WorkerBootstrapConfig, literal: str
) -> None:
    refused(
        RESOURCE_REFUSAL_CODE,
        lambda: stage(worker, f"SELECT {literal} AS x FROM range(1)"),
    )
    assert not attempt_leaf(config).exists()


def test_the_encoder_bounds_refuse_instead_of_truncating(
    worker: AnalysisWorker, config: WorkerBootstrapConfig
) -> None:
    refused(
        RESOURCE_REFUSAL_CODE,
        lambda: stage(
            worker, "SELECT range AS n FROM range(100)", max_rows=100, max_bytes=500
        ),
    )
    assert not attempt_leaf(config).exists()


class Poison:
    """Any attribute access, coercion or call would record itself."""

    touched: list[str]

    def __init__(self) -> None:
        object.__setattr__(self, "touched", [])

    def __getattr__(self, name: str) -> Any:
        self.touched.append(name)
        raise AttributeError(name)

    @property
    def hex(self) -> str:  # a generic `.hex` coercion must never be tried
        self.touched.append("hex")
        return "00"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(Poison(), id="poison"),
        pytest.param(uuid.UUID(int=1), id="uuid"),
        "text",
        True,
        1,
        b"bytes",
        1.5,
        object(),
    ],
)
@pytest.mark.parametrize("type_text", ["INTEGER", "VARCHAR", "BLOB", "DATE", "DOUBLE"])
def test_incompatible_values_refuse_without_coercion(
    worker: AnalysisWorker,
    spy: SpyConnection,
    config: WorkerBootstrapConfig,
    value: Any,
    type_text: str,
) -> None:
    exact = {
        "INTEGER": 1,
        "VARCHAR": "text",
        "BLOB": b"bytes",
        "DOUBLE": 1.5,
    }.get(type_text)
    if type(value) is type(exact):
        return
    spy.description_override = [("x", type_text, None, None, None, None, None)]
    spy.rows_override = [(value,)]
    refused(
        RESOURCE_REFUSAL_CODE, lambda: stage(worker, "SELECT range AS x FROM range(1)")
    )
    if isinstance(value, Poison):
        assert value.touched == []
    assert not attempt_leaf(config).exists()


def test_the_artifact_call_has_no_path_parameter_and_the_value_is_frozen() -> None:
    parameters = inspect.signature(AnalysisWorker.execute_result_artifact).parameters
    assert list(parameters) == [
        "self",
        "sql",
        "parameters",
        "echo",
        "units",
        "max_rows",
        "max_bytes",
    ]
    assert [p.name for p in parameters.values() if p.kind is p.KEYWORD_ONLY] == [
        "echo",
        "units",
        "max_rows",
        "max_bytes",
    ]
    assert [f.name for f in dataclasses.fields(StagedAnalysisResult)] == [
        "candidate",
        "handle",
    ]
    assert "__slots__" in vars(StagedAnalysisResult)
    assert "StagedAnalysisResult" in worker_module.__all__
    assert not hasattr(worker_module, "write_artifact")
    assert not hasattr(worker_module, "_encodable")


def encode_other(**changes: Any) -> Any:
    column = AnalysisResultColumn("n", "integer", True, None, None, None)
    echo = dataclasses.replace(ECHO, **changes.pop("echo", {}))
    return encode_analysis_result_artifact(
        (changes.pop("column", column),),
        changes.pop("rows", [(5,)]),
        echo=echo,
        max_rows=10,
        max_bytes=100_000,
    )


@pytest.fixture()
def recorded_opens(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every fixed-leaf acquisition attempt, on any platform (os.open or CreateFileW)."""
    attempts: list[Path] = []
    real_acquire = worker_module._acquire_leaf

    def recording(directory: Path, dir_fd: int | None) -> int:
        attempts.append(directory)
        return real_acquire(directory, dir_fd)

    monkeypatch.setattr(worker_module, "_acquire_leaf", recording)
    return attempts


def track_leaf_fds(monkeypatch: pytest.MonkeyPatch) -> set[int]:
    """The descriptor each successful leaf acquisition returns, on any platform."""
    fds: set[int] = set()
    real_acquire = worker_module._acquire_leaf

    def tracking(directory: Path, dir_fd: int | None) -> int:
        fd = real_acquire(directory, dir_fd)
        fds.add(fd)
        return fd

    monkeypatch.setattr(worker_module, "_acquire_leaf", tracking)
    return fds


@pytest.mark.parametrize(
    "make_candidate",
    [
        lambda: encode_other(echo={"run_id": "run-2"}),
        lambda: encode_other(
            column=AnalysisResultColumn(
                "n", "integer", True, None, None, "currency:AUD"
            )
        ),
        lambda: encode_other(
            column=AnalysisResultColumn("m", "integer", True, None, None, None)
        ),
        lambda: "not a candidate",
    ],
)
def test_a_foreign_or_mismatched_candidate_is_refused_before_the_leaf_opens(
    worker: AnalysisWorker,
    config: WorkerBootstrapConfig,
    monkeypatch: pytest.MonkeyPatch,
    recorded_opens: list[Any],
    make_candidate: Callable[[], Any],
) -> None:
    foreign = make_candidate()
    monkeypatch.setattr(
        worker_module, "encode_analysis_result_artifact", lambda *a, **k: foreign
    )
    refused(
        BOUNDARY_REFUSAL_CODE, lambda: stage(worker, "SELECT range AS n FROM range(1)")
    )
    assert recorded_opens == []
    assert not attempt_leaf(config).exists()


def test_a_forged_candidate_is_refused_before_the_leaf_opens(
    worker: AnalysisWorker,
    config: WorkerBootstrapConfig,
    monkeypatch: pytest.MonkeyPatch,
    recorded_opens: list[Any],
) -> None:
    real = encode_other()
    forged = object.__new__(type(real))
    for field in dataclasses.fields(real):
        object.__setattr__(forged, field.name, getattr(real, field.name))
    object.__setattr__(forged, "artifact_bytes", real.artifact_bytes + b" ")
    monkeypatch.setattr(
        worker_module, "encode_analysis_result_artifact", lambda *a, **k: forged
    )
    refused(
        BOUNDARY_REFUSAL_CODE, lambda: stage(worker, "SELECT range AS n FROM range(1)")
    )
    assert recorded_opens == []


def test_only_the_validated_snapshot_is_staged(
    worker: AnalysisWorker,
    config: WorkerBootstrapConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = encode_other(rows=[(7,)])
    real_validate = worker_module.validate_analysis_result_artifact_candidate
    seen: list[Any] = []

    def validate(candidate: Any) -> Any:
        seen.append(real_validate(candidate))
        return other  # same schema and echo, different bytes: the snapshot wins

    monkeypatch.setattr(
        worker_module, "validate_analysis_result_artifact_candidate", validate
    )
    staged = stage(worker, "SELECT range AS n FROM range(1)")
    assert staged.candidate is other
    assert attempt_leaf(config).read_bytes() == other.artifact_bytes


def test_a_watchdog_interrupt_after_fetch_or_before_open_refuses(
    worker: AnalysisWorker,
    spy: SpyConnection,
    config: WorkerBootstrapConfig,
    monkeypatch: pytest.MonkeyPatch,
    recorded_opens: list[Any],
) -> None:
    sql = "SELECT range AS n FROM range(1)"

    def interrupt() -> None:
        worker._interrupted_reason = "wall_time_exceeded"

    spy.on_fetch = interrupt
    refusal = refused(RESOURCE_REFUSAL_CODE, lambda: stage(worker, sql))
    assert refusal.reason == "wall_time_exceeded"
    assert recorded_opens == [] and not attempt_leaf(config).exists()
    # Interrupted workers refuse before any engine call at all.
    spy.executed.clear()
    refused(RESOURCE_REFUSAL_CODE, lambda: stage(worker, sql))
    refused(RESOURCE_REFUSAL_CODE, lambda: worker.execute_sql(sql))
    assert spy.executed == []

    worker._interrupted_reason = None
    spy.on_fetch = None
    real_validate = worker_module.validate_analysis_result_artifact_candidate

    def validate(candidate: Any) -> Any:
        snapshot = real_validate(candidate)
        interrupt()
        return snapshot

    monkeypatch.setattr(
        worker_module, "validate_analysis_result_artifact_candidate", validate
    )
    refusal = refused(RESOURCE_REFUSAL_CODE, lambda: stage(worker, sql))
    assert refusal.reason == "wall_time_exceeded"
    assert recorded_opens == [] and not attempt_leaf(config).exists()


def test_lazy_fetch_errors_map_to_the_resource_refusal(
    worker: AnalysisWorker, spy: SpyConnection, config: WorkerBootstrapConfig
) -> None:
    spy.fetch_error = duckdb.InvalidInputException("lazy failure")
    sql = "SELECT range AS n FROM range(1)"
    for call in (lambda: stage(worker, sql), lambda: worker.execute_sql(sql)):
        refusal = refused(RESOURCE_REFUSAL_CODE, call)
        assert refusal.reason.startswith("engine error: ")
    assert not attempt_leaf(config).exists()


# -- attempt directory and fixed leaf ----------------------------------------


def test_an_existing_regular_leaf_is_preserved_and_refused(
    worker: AnalysisWorker, config: WorkerBootstrapConfig
) -> None:
    attempt_leaf(config).write_bytes(b"keep me")
    refused(
        BOUNDARY_REFUSAL_CODE, lambda: stage(worker, "SELECT range AS n FROM range(1)")
    )
    assert attempt_leaf(config).read_bytes() == b"keep me"


def test_symlink_and_broken_symlink_leaves_are_preserved_and_refused(
    worker: AnalysisWorker, config: WorkerBootstrapConfig, tmp_path: Path
) -> None:
    sql = "SELECT range AS n FROM range(1)"
    target = tmp_path / "outside.txt"
    target.write_bytes(b"outside")
    leaf = attempt_leaf(config)
    leaf.symlink_to(target)
    refused(BOUNDARY_REFUSAL_CODE, lambda: stage(worker, sql))
    assert leaf.is_symlink() and target.read_bytes() == b"outside"
    leaf.unlink()
    missing = tmp_path / "missing.txt"
    leaf.symlink_to(missing)
    refused(BOUNDARY_REFUSAL_CODE, lambda: stage(worker, sql))
    assert leaf.is_symlink()
    assert leaf.resolve(strict=False) == missing.resolve(strict=False)
    assert not missing.exists()


def test_a_symlink_or_file_attempt_directory_refuses_at_bootstrap(
    config: WorkerBootstrapConfig, tmp_path: Path
) -> None:
    real = tmp_path / "real-attempt"
    real.mkdir()
    link = tmp_path / "linked-attempt"
    link.symlink_to(real, target_is_directory=True)
    as_file = tmp_path / "file-attempt"
    as_file.write_bytes(b"x")
    for bad in (link, as_file):
        bad_config = dataclasses.replace(config, attempt_directory=bad)
        refused(BOUNDARY_REFUSAL_CODE, lambda c=bad_config: open_analysis_worker(c))  # type: ignore[misc]
    assert list(real.iterdir()) == [] and link.is_symlink()
    assert as_file.read_bytes() == b"x"


def test_an_attempt_directory_swapped_after_bootstrap_is_refused_at_staging(
    worker: AnalysisWorker, config: WorkerBootstrapConfig, tmp_path: Path
) -> None:
    sql = "SELECT range AS n FROM range(1)"
    outside = tmp_path / "outside"
    outside.mkdir()
    attempt = config.attempt_directory
    attempt.rmdir()
    attempt.symlink_to(outside, target_is_directory=True)
    refused(BOUNDARY_REFUSAL_CODE, lambda: stage(worker, sql))
    assert list(outside.iterdir()) == [] and attempt.is_symlink()
    attempt.unlink()
    attempt.write_bytes(b"x")
    refused(BOUNDARY_REFUSAL_CODE, lambda: stage(worker, sql))
    assert attempt.read_bytes() == b"x"
    attempt.unlink()
    refused(BOUNDARY_REFUSAL_CODE, lambda: stage(worker, sql))  # absent at staging


def test_windows_reparse_points_are_unsafe_directories() -> None:
    class Info:
        st_mode = stat.S_IFDIR | 0o700
        st_file_attributes = stat.FILE_ATTRIBUTE_REPARSE_POINT

    class Plain(Info):
        st_file_attributes = 0

    assert worker_module._is_unsafe_directory(Info()) is True  # type: ignore[arg-type]
    assert worker_module._is_unsafe_directory(Plain()) is False  # type: ignore[arg-type]


@posix_only
def test_modes_are_owner_private(
    worker: AnalysisWorker, config: WorkerBootstrapConfig
) -> None:
    assert stat.S_IMODE(config.attempt_directory.stat().st_mode) == 0o700
    stage(worker, "SELECT range AS n FROM range(1)")
    assert stat.S_IMODE(attempt_leaf(config).stat().st_mode) == 0o600


@posix_only
def test_a_loose_attempt_directory_is_tightened_before_staging(
    worker: AnalysisWorker, config: WorkerBootstrapConfig
) -> None:
    config.attempt_directory.chmod(0o755)
    stage(worker, "SELECT range AS n FROM range(1)")
    assert stat.S_IMODE(config.attempt_directory.stat().st_mode) == 0o700


@posix_only
def test_an_unsafe_leaf_mode_is_refused_and_the_leaf_is_retained(
    worker: AnalysisWorker,
    config: WorkerBootstrapConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_fchmod = os.fchmod  # the leaf really ends up group/other readable
    monkeypatch.setattr(os, "fchmod", lambda fd, _mode: real_fchmod(fd, 0o644))
    refused(
        BOUNDARY_REFUSAL_CODE, lambda: stage(worker, "SELECT range AS n FROM range(1)")
    )
    assert attempt_leaf(config).is_file()  # created, then refused: retained


def test_short_writes_complete_and_fsync_is_observed(
    worker: AnalysisWorker,
    config: WorkerBootstrapConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_write, real_fsync = os.write, os.fsync
    writes: list[int] = []
    synced: list[int] = []

    def short_write(fd: int, data: Any) -> int:
        count = real_write(fd, bytes(data)[:7])
        writes.append(count)
        return count

    def fsync(fd: int) -> None:
        synced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "write", short_write)
    monkeypatch.setattr(os, "fsync", fsync)
    staged = stage(worker, "SELECT range AS n FROM range(1)")
    assert len(writes) > 1 and set(writes) == {7} | {writes[-1]}
    assert len(synced) == 1
    assert attempt_leaf(config).read_bytes() == staged.candidate.artifact_bytes


def failing(name: str) -> Callable[..., Any]:
    return {
        "zero_write": lambda fd, data: 0,
        "raised_write": lambda fd, data: (_ for _ in ()).throw(OSError("disk")),
    }[name]


@pytest.mark.parametrize(
    "failure",
    [
        "zero_write",
        "raised_write",
        pytest.param("fchmod", marks=posix_only),  # Windows has no `os.fchmod`
        "fstat",
        "fsync",
        "close",
    ],
)
def test_post_create_failures_refuse_and_retain_the_leaf(
    worker: AnalysisWorker,
    config: WorkerBootstrapConfig,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    sibling = config.attempt_directory / "keep.txt"
    sibling.write_bytes(b"kept")
    real_close = os.close
    real_fstat = os.fstat
    leaf_fds = track_leaf_fds(monkeypatch)

    def closing(fd: int) -> None:
        real_close(fd)
        if fd in leaf_fds:
            leaf_fds.discard(fd)
            raise OSError("close")

    def broken_fstat(fd: int) -> os.stat_result:
        if fd in leaf_fds:
            raise OSError(errno.EIO, "fstat")
        return real_fstat(fd)

    def broken_fsync(_fd: int) -> None:
        raise OSError("fsync")

    if failure in ("zero_write", "raised_write"):
        monkeypatch.setattr(os, "write", failing(failure))
    elif failure == "fchmod":
        real_fchmod = os.fchmod  # only reached on POSIX

        def broken_fchmod(fd: int, mode: int) -> None:
            if fd in leaf_fds:
                raise OSError(errno.EIO, "fchmod")
            real_fchmod(fd, mode)

        monkeypatch.setattr(os, "fchmod", broken_fchmod)
    elif failure == "fstat":
        monkeypatch.setattr(os, "fstat", broken_fstat)
    elif failure == "fsync":
        monkeypatch.setattr(os, "fsync", broken_fsync)
    else:
        monkeypatch.setattr(os, "close", closing)
    refusal = refused(
        RESOURCE_REFUSAL_CODE, lambda: stage(worker, "SELECT range AS n FROM range(1)")
    )
    assert refusal.reason == "the result artifact could not be staged"
    assert attempt_leaf(config).is_file()  # retained; its bytes are not authoritative
    assert sibling.read_bytes() == b"kept"


def test_an_interrupt_between_writes_retains_the_partial_leaf(
    worker: AnalysisWorker,
    config: WorkerBootstrapConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_write = os.write

    def interrupting(fd: int, data: Any) -> int:
        count = real_write(fd, bytes(data)[:5])
        worker._interrupted_reason = "spill_quota_exceeded"
        return count

    monkeypatch.setattr(os, "write", interrupting)
    refusal = refused(
        RESOURCE_REFUSAL_CODE, lambda: stage(worker, "SELECT range AS n FROM range(1)")
    )
    assert refusal.reason == "spill_quota_exceeded"
    assert attempt_leaf(config).is_file()  # whatever is there is non-authoritative


def test_the_worker_source_has_no_obsolete_writer_or_forbidden_imports() -> None:
    text = Path(worker_module.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "hashlib",
        "import json",
        "write_artifact",
        "_encodable",
        "operations",
    ):
        assert forbidden not in text


def patch_open(monkeypatch: pytest.MonkeyPatch, wrapper: Callable[..., int]) -> None:
    """Replace `os.open`, keeping the dir_fd staging branch live where the platform has it."""
    dir_fd_capable = os.open in os.supports_dir_fd
    monkeypatch.setattr(os, "open", wrapper)
    if dir_fd_capable:
        monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {wrapper})


@posix_os_open_seam
@pytest.mark.parametrize(
    "code",
    [
        errno.ENOSPC,
        errno.EIO,
        errno.EMFILE,
        errno.ENFILE,
        errno.ENOMEM,
        errno.EFBIG,
        errno.EOVERFLOW,
    ],
)
def test_resource_errors_on_leaf_create_are_the_write_refusal(
    worker: AnalysisWorker,
    config: WorkerBootstrapConfig,
    monkeypatch: pytest.MonkeyPatch,
    code: int,
) -> None:
    real_open = os.open
    sibling = config.attempt_directory / "keep.txt"
    sibling.write_bytes(b"kept")

    def failing_open(path: Any, *args: Any, **kwargs: Any) -> int:
        if str(path).endswith(LEAF):
            raise OSError(code, os.strerror(code))
        return real_open(path, *args, **kwargs)

    patch_open(monkeypatch, failing_open)
    refusal = refused(
        RESOURCE_REFUSAL_CODE, lambda: stage(worker, "SELECT range AS n FROM range(1)")
    )
    assert refusal.reason == "the result artifact could not be staged"
    assert not attempt_leaf(config).exists() and sibling.read_bytes() == b"kept"


@posix_os_open_seam
@pytest.mark.parametrize(
    "code",
    [errno.EEXIST, errno.ELOOP, errno.ENOENT, errno.ENOTDIR, errno.EACCES, errno.EROFS],
)
def test_topology_and_mode_errors_on_leaf_create_stay_the_boundary_refusal(
    worker: AnalysisWorker,
    config: WorkerBootstrapConfig,
    monkeypatch: pytest.MonkeyPatch,
    code: int,
) -> None:
    real_open = os.open

    def failing_open(path: Any, *args: Any, **kwargs: Any) -> int:
        if str(path).endswith(LEAF):
            raise OSError(code, os.strerror(code))
        return real_open(path, *args, **kwargs)

    patch_open(monkeypatch, failing_open)
    refusal = refused(
        BOUNDARY_REFUSAL_CODE, lambda: stage(worker, "SELECT range AS n FROM range(1)")
    )
    assert refusal.reason == "the attempt directory or result leaf is not safe"
    assert not attempt_leaf(config).exists()


@posix_only
@pytest.mark.parametrize("preexisting", [False, True])
def test_a_directory_fd_close_failure_is_a_refusal_and_retains_every_object(
    worker: AnalysisWorker,
    config: WorkerBootstrapConfig,
    monkeypatch: pytest.MonkeyPatch,
    preexisting: bool,
) -> None:
    sibling = config.attempt_directory / "keep.txt"
    sibling.write_bytes(b"kept")
    if preexisting:
        attempt_leaf(config).write_bytes(b"keep me")
    real_close = os.close
    failed: list[int] = []

    def closing(fd: int) -> None:
        is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)  # never the leaf's descriptor
        real_close(fd)
        if is_directory and not failed:  # the first directory close only
            failed.append(fd)
            raise OSError(errno.EIO, "close")

    monkeypatch.setattr(os, "close", closing)
    code, reason = (
        (BOUNDARY_REFUSAL_CODE, "the attempt directory or result leaf is not safe")
        if preexisting
        else (RESOURCE_REFUSAL_CODE, "the result artifact could not be staged")
    )
    refusal = refused(code, lambda: stage(worker, "SELECT range AS n FROM range(1)"))
    assert refusal.reason == reason
    assert len(failed) == 1  # the dir_fd branch really ran
    assert sibling.read_bytes() == b"kept"
    if preexisting:
        assert attempt_leaf(config).read_bytes() == b"keep me"
    else:
        assert attempt_leaf(config).is_file()  # created, then refused: retained


@pytest.mark.parametrize("where", ["fsync", "close"])
def test_a_deadline_during_the_durable_write_refuses_and_retains_the_leaf(
    worker: AnalysisWorker,
    config: WorkerBootstrapConfig,
    monkeypatch: pytest.MonkeyPatch,
    where: str,
) -> None:
    sibling = config.attempt_directory / "keep.txt"
    sibling.write_bytes(b"kept")
    real_fsync, real_close = os.fsync, os.close
    leaf_fds = track_leaf_fds(monkeypatch)

    def interrupting_fsync(fd: int) -> None:
        real_fsync(fd)
        worker._interrupted_reason = "wall_time_exceeded"

    def interrupting_close(fd: int) -> None:
        real_close(fd)
        if fd in leaf_fds:
            worker._interrupted_reason = "spill_quota_exceeded"

    if where == "fsync":
        monkeypatch.setattr(os, "fsync", interrupting_fsync)
    else:
        monkeypatch.setattr(os, "close", interrupting_close)
    refusal = refused(
        RESOURCE_REFUSAL_CODE, lambda: stage(worker, "SELECT range AS n FROM range(1)")
    )
    assert refusal.reason == (
        "wall_time_exceeded" if where == "fsync" else "spill_quota_exceeded"
    )
    assert attempt_leaf(config).is_file()
    assert sibling.read_bytes() == b"kept"


# -- directory verification errors, close errors and retain-on-refusal --------

RESOURCE_CODES = [errno.EMFILE, errno.ENFILE, errno.EIO, errno.ENOMEM, errno.EFBIG]
BOUNDARY_CODES = [errno.ELOOP, errno.ENOTDIR, errno.ENOENT, errno.EACCES, errno.EPERM]
DIRECTORY_FAULTS = [
    (where, code, RESOURCE_REFUSAL_CODE)
    for where in ("open", "fstat", "fchmod")
    for code in RESOURCE_CODES
] + [
    (where, code, BOUNDARY_REFUSAL_CODE)
    for where in ("open", "fstat", "fchmod")
    for code in BOUNDARY_CODES
]


@posix_only
@pytest.mark.parametrize("close_fails", [False, True])
@pytest.mark.parametrize(("where", "code", "expected"), DIRECTORY_FAULTS)
def test_directory_verification_errors_are_classified_and_the_fd_is_released(
    worker: AnalysisWorker,
    config: WorkerBootstrapConfig,
    monkeypatch: pytest.MonkeyPatch,
    where: str,
    code: int,
    expected: str,
    close_fails: bool,
) -> None:
    config.attempt_directory.chmod(0o755)  # so the directory fchmod is reached
    sibling = config.attempt_directory / "keep.txt"
    sibling.write_bytes(b"kept")
    real_open, real_fstat, real_fchmod, real_close = (
        os.open,
        os.fstat,
        os.fchmod,
        os.close,
    )
    held: set[int] = set()  # directory descriptors opened and not yet closed

    def fault() -> OSError:
        return OSError(code, os.strerror(code))

    def opening(path: Any, *args: Any, **kwargs: Any) -> int:
        if Path(path) != config.attempt_directory:
            return real_open(path, *args, **kwargs)
        if where == "open":
            raise fault()
        held.add(fd := real_open(path, *args, **kwargs))
        return fd

    def fstat(fd: int) -> os.stat_result:
        info = real_fstat(fd)
        if where == "fstat" and stat.S_ISDIR(info.st_mode):
            raise fault()
        return info

    def fchmod(fd: int, mode: int) -> None:
        if where == "fchmod" and stat.S_ISDIR(real_fstat(fd).st_mode):
            raise fault()
        real_fchmod(fd, mode)

    def closing(fd: int) -> None:
        real_close(fd)
        if fd in held:
            held.discard(fd)
            if close_fails:  # the primary refusal must survive a failing close
                raise OSError(errno.EIO, "close")

    patch_open(monkeypatch, opening)
    monkeypatch.setattr(os, "fstat", fstat)
    monkeypatch.setattr(os, "fchmod", fchmod)
    monkeypatch.setattr(os, "close", closing)
    refusal = refused(
        expected, lambda: stage(worker, "SELECT range AS n FROM range(1)")
    )
    assert refusal.reason == (
        "the result artifact could not be staged"
        if expected == RESOURCE_REFUSAL_CODE
        else "the attempt directory or result leaf is not safe"
    )
    assert not held  # every acquired directory descriptor was closed
    assert not attempt_leaf(config).exists() and sibling.read_bytes() == b"kept"
    monkeypatch.undo()
    stage(worker, "SELECT range AS n FROM range(1)")  # nothing was left behind


FOREIGN = b"foreign"


def swap_leaf(config: WorkerBootstrapConfig, tmp_path: Path, kind: str) -> Path:
    """Replace the fixed name with a foreign regular file or a symlink; return the target."""
    leaf = attempt_leaf(config)
    leaf.unlink()
    target = tmp_path / "foreign-target.txt"
    if kind == "file":
        leaf.write_bytes(FOREIGN)
        return leaf
    target.write_bytes(FOREIGN)
    leaf.symlink_to(target)
    return target


def assert_foreign_survives(
    config: WorkerBootstrapConfig, target: Path, kind: str
) -> None:
    leaf = attempt_leaf(config)
    assert leaf.is_symlink() == (kind == "symlink")
    assert target.read_bytes() == FOREIGN and leaf.read_bytes() == FOREIGN


@pytest.mark.parametrize("kind", ["file", "symlink"])
@pytest.mark.parametrize(
    "where",
    [pytest.param("write", marks=open_leaf_write_posix_only), "leaf_close"],
)
def test_a_replaced_name_is_never_touched_by_an_in_place_failure(
    worker: AnalysisWorker,
    config: WorkerBootstrapConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    where: str,
    kind: str,
) -> None:
    real_write, real_close = os.write, os.close
    leaf_fds = track_leaf_fds(monkeypatch)
    swapped: list[Path] = []

    def interrupting_write(fd: int, data: Any) -> int:
        count = real_write(fd, bytes(data)[:5])
        swapped.append(swap_leaf(config, tmp_path, kind))
        worker._interrupted_reason = "spill_quota_exceeded"
        return count

    def interrupting_close(fd: int) -> None:
        real_close(fd)
        if fd in leaf_fds:
            leaf_fds.discard(fd)
            swapped.append(swap_leaf(config, tmp_path, kind))
            worker._interrupted_reason = "spill_quota_exceeded"

    if where == "write":
        monkeypatch.setattr(os, "write", interrupting_write)
    else:
        monkeypatch.setattr(os, "close", interrupting_close)
    refusal = refused(
        RESOURCE_REFUSAL_CODE, lambda: stage(worker, "SELECT range AS n FROM range(1)")
    )
    assert refusal.reason == "spill_quota_exceeded"
    assert len(swapped) == 1  # the race really happened
    assert_foreign_survives(config, swapped[0], kind)


@posix_only
@pytest.mark.parametrize("kind", [None, "file", "symlink"])
@pytest.mark.parametrize("failure", ["close_error", "deadline_during_close"])
def test_the_directory_close_transition_retains_whatever_occupies_the_name(
    worker: AnalysisWorker,
    config: WorkerBootstrapConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
    kind: str | None,
) -> None:
    sibling = config.attempt_directory / "keep.txt"
    sibling.write_bytes(b"kept")
    real_close, real_fstat = os.close, os.fstat
    seen: list[Path] = []

    def closing(fd: int) -> None:
        is_directory = stat.S_ISDIR(
            real_fstat(fd).st_mode
        )  # never the leaf's descriptor
        real_close(fd)
        if not is_directory or seen:  # the first directory close only
            return
        seen.append(attempt_leaf(config))
        if kind is not None:
            seen[0] = swap_leaf(config, tmp_path, kind)
        if failure == "close_error":
            raise OSError(errno.EIO, "close")
        worker._interrupted_reason = (
            "wall_time_exceeded"  # visible only after a good close
        )

    monkeypatch.setattr(os, "close", closing)
    refusal = refused(
        RESOURCE_REFUSAL_CODE, lambda: stage(worker, "SELECT range AS n FROM range(1)")
    )
    assert refusal.reason == (
        "the result artifact could not be staged"
        if failure == "close_error"
        else "wall_time_exceeded"
    )
    assert seen and sibling.read_bytes() == b"kept"
    if kind is None:
        assert attempt_leaf(config).is_file()  # retained, non-authoritative
    else:
        assert_foreign_survives(config, seen[0], kind)


NAMESPACE_CALLS = (
    "stat",
    "lstat",
    "unlink",
    "remove",
    "rename",
    "replace",
    "link",
    "symlink",
    "mkdir",
    "rmdir",
)


@pytest.mark.parametrize("where", ["write", "deadline"])
def test_the_old_check_then_unlink_exploit_has_no_interval_to_hit(
    worker: AnalysisWorker,
    config: WorkerBootstrapConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    where: str,
) -> None:
    """The old cleanup did `stat(leaf)` then `unlink(leaf)`; a swap hooked after the
    real stat deleted a foreign object. After creation no pathname call may happen."""
    created = track_leaf_fds(monkeypatch)
    calls: list[str] = []
    swapped: list[Path] = []

    def instrument(name: str) -> None:
        real = getattr(os, name)

        def wrapper(path: Any, *args: Any, **kwargs: Any) -> Any:
            hit = created and Path(path).name in (LEAF, config.attempt_directory.name)
            if hit:
                calls.append(name)
            result = real(path, *args, **kwargs)
            if hit and name == "stat":  # the exact exploit: swap after the real stat
                swapped.append(swap_leaf(config, tmp_path, "file"))
            return result

        monkeypatch.setattr(os, name, wrapper)

    for name in NAMESPACE_CALLS:
        instrument(name)
    real_write = os.write

    def failing_write(fd: int, data: Any) -> int:
        if where == "write":
            raise OSError(errno.EIO, "disk")
        count = real_write(fd, bytes(data)[:5])
        worker._interrupted_reason = "wall_time_exceeded"
        return count

    monkeypatch.setattr(os, "write", failing_write)
    refusal = refused(
        RESOURCE_REFUSAL_CODE, lambda: stage(worker, "SELECT range AS n FROM range(1)")
    )
    assert refusal.reason == (
        "the result artifact could not be staged"
        if where == "write"
        else "wall_time_exceeded"
    )
    assert created and calls == [] and swapped == []  # no stat, no unlink, no swap
    monkeypatch.undo()  # the existence check below must not run the exploit hook
    leaf = attempt_leaf(config)
    assert leaf.is_file() and leaf.parent == config.attempt_directory
    assert swapped == [] and not leaf.is_symlink()


def test_a_retry_in_the_same_attempt_directory_refuses_on_the_retained_leaf(
    worker: AnalysisWorker,
    config: WorkerBootstrapConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = "SELECT range AS n FROM range(1)"
    monkeypatch.setattr(os, "write", failing("raised_write"))
    refused(RESOURCE_REFUSAL_CODE, lambda: stage(worker, sql))
    monkeypatch.undo()
    retained = attempt_leaf(config).read_bytes()
    refusal = refused(BOUNDARY_REFUSAL_CODE, lambda: stage(worker, sql))
    assert refusal.reason == "the attempt directory or result leaf is not safe"
    assert attempt_leaf(config).read_bytes() == retained  # the retry changed nothing


def test_a_fresh_attempt_directory_stages_after_a_refused_attempt(
    config: WorkerBootstrapConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sql = "SELECT range AS n FROM range(1)"
    with monkeypatch.context() as broken:
        broken.setattr(os, "write", failing("raised_write"))
        first = open_analysis_worker(config)
        try:
            refused(RESOURCE_REFUSAL_CODE, lambda: stage(first, sql))
        finally:
            first.close()
    fresh = dataclasses.replace(
        config,
        attempt_directory=tmp_path / "attempt-2",
        temp_directory=tmp_path / "spill-2",
    )
    second = open_analysis_worker(fresh)
    try:
        staged = stage(second, sql)
    finally:
        second.close()
    assert (
        fresh.attempt_directory / LEAF
    ).read_bytes() == staged.candidate.artifact_bytes


# -- Windows native leaf: CreateFileW and the CRT adapter, injected -----------

NATIVE_HANDLE = 0x1_0000_0008  # above 32 bits: the CRT must receive it unchanged
INVALID_HANDLE = ctypes.c_void_p(-1).value  # pointer-width INVALID_HANDLE_VALUE
NATIVE_RESOURCE_CODES = {4, 8, 14, 29, 31, 39, 110, 112, 223, 1117, 1127, 1295, 1816}
NATIVE_RESOURCE_CODES |= set(range(1450, 1456))


class HostileSentinel(BaseException):
    """A non-secret stand-in for a BaseException raised by an audit hook."""


class FakeWin32:
    """Stands in for kernel32 and the CRT on any host.

    The create is a real O_EXCL file, so the leaf exists exactly as CREATE_NEW
    would leave it, and the close releases that real descriptor first.

    `open_mode` is the CRT conversion: "fd" succeeds, "minus_one" is the CRT's
    failure return, "raise" is an OSError and "base" is a hostile BaseException.
    `close_mode` is CloseHandle: "true" returns nonzero (success), "false" returns
    zero (the real BOOL failure, which ctypes does not raise), and "base" is a
    hostile BaseException from the injected callable.
    """

    def __init__(
        self,
        *,
        create_result: int | None = NATIVE_HANDLE,
        error: int = 0,
        open_mode: str = "fd",
        close_mode: str = "true",
    ) -> None:
        self.create_result = create_result
        self.error = error
        self.open_mode = open_mode
        self.close_mode = close_mode
        self.events: list[str] = []
        self.create_args: tuple[Any, ...] = ()
        self.opened_with: tuple[int, int] | None = None
        self.closes: list[int] = []
        self.fd = -1

    def api(self) -> worker_module._Win32Api:
        return worker_module._Win32Api(
            create_file=self.create_file,
            close_handle=self.close_handle,
            last_error=self.last_error,
            open_osfhandle=self.open_osfhandle,
        )

    def create_file(self, *args: Any) -> int | None:
        self.events.append("create")
        self.create_args = args
        if self.create_result != NATIVE_HANDLE:
            return self.create_result
        try:
            self.fd = os.open(args[0], os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            self.error = 80  # ERROR_FILE_EXISTS
            return INVALID_HANDLE
        return NATIVE_HANDLE

    def last_error(self) -> int:
        self.events.append("last_error")
        return self.error

    def open_osfhandle(self, handle: int, flags: int) -> int:
        self.events.append("open")
        self.opened_with = (handle, flags)
        if self.open_mode == "raise":
            raise OSError("crt conversion failed")
        if self.open_mode == "base":
            raise HostileSentinel("hostile-open-sentinel")
        if self.open_mode == "minus_one":
            return -1
        return self.fd

    def close_handle(self, handle: int) -> int:
        self.events.append("close")
        self.closes.append(handle)
        os.close(self.fd)  # release the real descriptor whatever the close reports
        if self.close_mode == "base":
            raise HostileSentinel("hostile-close-sentinel")
        return 1 if self.close_mode == "true" else 0  # BOOL: nonzero ok, zero failed


def native_directory(tmp_path: Path) -> str:
    directory = tmp_path / "native"
    directory.mkdir()
    return str(directory / LEAF)


def test_native_create_passes_the_exact_win32_arguments_and_crt_flags(
    tmp_path: Path,
) -> None:
    path = native_directory(tmp_path)
    fake = FakeWin32()
    fd = worker_module._create_native_leaf(path, fake.api())
    try:
        # CreateFileW: GENERIC_WRITE, no sharing, NULL security, CREATE_NEW,
        # FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT, NULL template.
        assert fake.create_args == (path, 0x40000000, 0, None, 1, 0x00200080, None)
        # _O_WRONLY | _O_NOINHERIT | _O_BINARY, and the handle passes through whole.
        assert fake.opened_with == (NATIVE_HANDLE, 0x00008081)
        assert NATIVE_HANDLE > 0xFFFFFFFF
        assert fd == fake.fd
        assert fake.events == ["create", "open"]
    finally:
        os.close(fd)
    assert fake.closes == []  # ownership moved to the CRT: os.close is the only close


@pytest.mark.parametrize("code", sorted(NATIVE_RESOURCE_CODES))
def test_native_resource_codes_are_the_write_refusal(tmp_path: Path, code: int) -> None:
    fake = FakeWin32(create_result=INVALID_HANDLE, error=code)
    refusal = refused(
        RESOURCE_REFUSAL_CODE,
        lambda: worker_module._create_native_leaf(
            native_directory(tmp_path), fake.api()
        ),
    )
    assert refusal.reason == "the result artifact could not be staged"
    assert fake.events == ["create", "last_error"]  # the error read comes first


@pytest.mark.parametrize("code", [2, 3, 5, 32, 80, 183, 1921])
def test_native_other_codes_are_the_boundary_refusal(tmp_path: Path, code: int) -> None:
    assert code not in NATIVE_RESOURCE_CODES
    fake = FakeWin32(create_result=INVALID_HANDLE, error=code)
    refusal = refused(
        BOUNDARY_REFUSAL_CODE,
        lambda: worker_module._create_native_leaf(
            native_directory(tmp_path), fake.api()
        ),
    )
    assert refusal.reason == "the attempt directory or result leaf is not safe"
    assert fake.events == ["create", "last_error"]


def test_a_null_native_handle_is_refused_like_an_invalid_one(tmp_path: Path) -> None:
    fake = FakeWin32(create_result=None, error=8)
    refused(
        RESOURCE_REFUSAL_CODE,
        lambda: worker_module._create_native_leaf(
            native_directory(tmp_path), fake.api()
        ),
    )
    assert fake.events == ["create", "last_error"]


@pytest.mark.parametrize("open_mode", ["raise", "minus_one", "base"])
@pytest.mark.parametrize("close_mode", ["true", "false", "base"])
def test_a_failed_crt_conversion_closes_once_and_refuses_with_the_fixed_reason(
    tmp_path: Path, open_mode: str, close_mode: str
) -> None:
    path = native_directory(tmp_path)
    fake = FakeWin32(open_mode=open_mode, close_mode=close_mode)
    refusal = refused(
        RESOURCE_REFUSAL_CODE,
        lambda: worker_module._create_native_leaf(path, fake.api()),
    )
    assert refusal.reason == "the result artifact could not be staged"
    # exactly one CloseHandle, whatever it reports
    assert fake.closes == [NATIVE_HANDLE]
    assert fake.events == ["create", "open", "close"]
    assert Path(path).is_file()  # created, then refused: retained, never unlinked


def test_an_open_osfhandle_base_exception_cannot_escape_and_closes_once(
    tmp_path: Path,
) -> None:
    path = native_directory(tmp_path)
    fake = FakeWin32(open_mode="base")
    refusal = refused(
        RESOURCE_REFUSAL_CODE,
        lambda: worker_module._create_native_leaf(path, fake.api()),
    )
    assert str(refusal) == refusal.reason == "the result artifact could not be staged"
    assert refusal.__context__ is None  # the sentinel is not chained onto the refusal
    assert "sentinel" not in repr(refusal)
    assert fake.closes == [NATIVE_HANDLE]


def test_concurrent_native_creates_have_exactly_one_winner(tmp_path: Path) -> None:
    """CREATE_NEW is atomic: of racing creators exactly one owns the leaf. The fake
    uses O_EXCL, so this checks the adapter's outcome mapping, not the OS itself."""
    path = native_directory(tmp_path)
    barrier = threading.Barrier(2)
    outcomes: list[int | WorkerRefusal] = []

    def racer() -> None:
        fake = FakeWin32()
        barrier.wait()
        try:
            outcomes.append(worker_module._create_native_leaf(path, fake.api()))
        except WorkerRefusal as refusal:
            outcomes.append(refusal)

    threads = [threading.Thread(target=racer) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    winners = [item for item in outcomes if isinstance(item, int)]
    losers = [item for item in outcomes if isinstance(item, WorkerRefusal)]
    assert len(winners) == 1 and len(losers) == 1
    assert losers[0].code == BOUNDARY_REFUSAL_CODE
    os.close(winners[0])


def test_the_native_adapter_is_lazy_and_never_imported_elsewhere() -> None:
    assert "msvcrt" not in vars(worker_module)  # imported inside the loader only
    if os.name != "nt":
        refused(BOUNDARY_REFUSAL_CODE, worker_module._win32_api)


@pytest.mark.skipif(os.name != "nt", reason="real CreateFileW on Windows only")
def test_real_native_creates_have_exactly_one_winner_on_windows(tmp_path: Path) -> None:
    path = native_directory(tmp_path)
    api = worker_module._win32_api()
    barrier = threading.Barrier(2)
    outcomes: list[int | WorkerRefusal] = []

    def racer() -> None:
        barrier.wait()
        try:
            outcomes.append(worker_module._create_native_leaf(path, api))
        except WorkerRefusal as refusal:
            outcomes.append(refusal)

    threads = [threading.Thread(target=racer) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    winners = [item for item in outcomes if isinstance(item, int)]
    assert len(winners) == 1
    os.close(winners[0])
    loser = next(item for item in outcomes if isinstance(item, WorkerRefusal))
    assert loser.code == BOUNDARY_REFUSAL_CODE
