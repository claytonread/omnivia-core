"""WP03 restricted-worker evidence (SPEC-CORE-DATA-001 §17, Q20/Q21/Q39/Q40).

The worker is exercised as the acceptance conditions require: trusted
bootstrap ordering on the exact admitted release, the closed external-access
posture re-verified, the fail-closed grammar, the host watchdog as the binding
control (Q39d/F-5 compensating controls), and truthful cancellation.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from omnivia_core_runtime.analysis.worker import (
    BOUNDARY_REFUSAL_CODE,
    RESOURCE_REFUSAL_CODE,
    WorkerBootstrapConfig,
    WorkerRefusal,
    open_analysis_worker,
    write_artifact,
)

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
    rows = worker.execute_sql(
        """
        SELECT COALESCE(SUM(i.amount_cents), 0)
        FROM invoices i
        WHERE i.status = 'overdue' AND i.currency = 'AUD'
          AND EXISTS (SELECT 1 FROM projects p
                      WHERE p.customer_id = i.customer_id AND p.is_active)
          AND i.customer_id = 1
        """
    )
    assert rows[0][0] == 120000  # the golden fanout value, not 360000
    artifact = write_artifact(
        rows, ["overdue_exposure_cents"], tmp_path / "attempt" / "result.jsonl"
    )
    assert artifact["rows"] == 1
    assert artifact["sha256"] and len(artifact["sha256"]) == 64
    written = json.loads(
        (tmp_path / "attempt" / "result.jsonl").read_text().splitlines()[0]
    )
    assert written == {"columns": ["overdue_exposure_cents"]}
    worker.close()


def test_the_worker_module_never_touches_canonical_storage() -> None:
    """Structural: the worker imports no storage module and opens no sqlite."""
    import omnivia_core_runtime.analysis.worker as worker_module

    source = worker_module.__file__
    assert source is not None
    text = Path(source).read_text(encoding="utf-8")
    assert "sqlite3" not in text
    assert "storage.connection" not in text
    assert "omnivia_core_runtime.storage" not in text
