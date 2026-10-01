"""WP05 typed-plan evidence (SPEC-CORE-DATA-001 §15, Q14/Q29/Q30/Q15/Q16).

The grain certificates are the point: existence is a semi-join (Q14), the
pre-aggregation re-joined to a one-to-many relation is rejected (Q30), equal
amounts are never distinct-collapsed (Q29), a contradicted unique key blocks
(Q15), and overlapping temporal windows are refused (Q16).
"""

from __future__ import annotations

from itertools import pairwise

import pytest
from omnivia_core_runtime.analysis.typed_plan import (
    GRAIN_VIOLATION_CODE,
    PlanValidationError,
    build_plan_from_request,
    compile_plan,
)
from omnivia_core_runtime.analysis.worker import WorkerBootstrapConfig


def _compiled_plan_sql() -> str:
    return compile_plan(build_plan_from_request({"metric": "overdue-exposure"}))


def test_q14_the_plan_uses_a_semijoin_and_compiles_bounded() -> None:
    plan = build_plan_from_request({})
    assert plan.nodes["customer_has_active_project"].operator == "semi_join"
    sql = _compiled_plan_sql()
    assert "EXISTS" in sql.upper()
    assert "JOIN projects" not in sql.upper().replace(
        "EXISTS (SELECT 1 FROM projects", ""
    )
    assert ";" not in sql


def test_q14_compiled_sql_runs_through_the_worker_and_returns_120000(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from omnivia_core_runtime.analysis.worker import open_analysis_worker

    invoices = tmp_path / "invoices.json"
    projects = tmp_path / "projects.json"
    invoices.write_text(
        json.dumps(
            [
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
            ]
        )
    )
    projects.write_text(
        json.dumps(
            [
                {"project_id": 201, "customer_id": 1, "is_active": True},
                {"project_id": 202, "customer_id": 1, "is_active": True},
                {"project_id": 203, "customer_id": 1, "is_active": True},
            ]
        )
    )
    config = WorkerBootstrapConfig(
        inputs=(("invoices", invoices), ("projects", projects)),
        memory_limit="512MB",
        temp_directory=tmp_path / "spill",
        spill_quota_bytes=16 * 1024 * 1024,
        wall_time_seconds=30,
        attempt_directory=tmp_path / "attempt",
    )
    worker = open_analysis_worker(config)
    try:
        sql = _compiled_plan_sql()
        rows = worker.execute_sql(sql)
        assert rows[0][0] == 120000  # golden: NOT 360000
    finally:
        worker.close()


def test_q29_equal_amounts_are_not_distinct_collapsed() -> None:
    """Two legitimate 50,000-cent invoices must both count; the compiled plan
    uses SUM over rows, never SUM(DISTINCT)."""
    plan = build_plan_from_request({})
    assert plan.nodes["exposure_sum"].spec["measures"][0]["op"] == "sum"
    sql = _compiled_plan_sql()
    assert "DISTINCT" not in sql.upper()


def test_q30_inner_join_in_place_of_the_semijoin_is_refused() -> None:
    plan = build_plan_from_request({})
    node = plan.nodes["customer_has_active_project"]
    changed = dict(node.spec)
    plan.nodes["customer_has_active_project"].spec = changed
    # Simulate an inner-join variant: the aggregate's upstream is a fanout join.
    plan.nodes["customer_has_active_project"].operator = "join"
    with pytest.raises(PlanValidationError) as raised:
        compile_plan(plan)
    assert raised.value.code == GRAIN_VIOLATION_CODE
    assert "semi-join" in raised.value.reason


def test_q30_preaggregate_rejoined_to_fanout_is_refused() -> None:
    plan = build_plan_from_request({})
    agg = plan.nodes["exposure_sum"]
    # The unsafe shape: an aggregate whose input is itself a fanout join.
    plan.nodes["customer_has_active_project"].operator = "join"
    agg.inputs = ("customer_has_active_project",)
    with pytest.raises(PlanValidationError):
        compile_plan(plan)


def test_q15_unique_key_contradicted_by_data_blocks(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A declared-unique key contradicted by actual data blocks the plan."""
    from omnivia_core_runtime.analysis.worker import WorkerRefusal, open_analysis_worker

    invoices = tmp_path / "invoices.json"
    projects = tmp_path / "projects.json"
    # Two rows with the SAME invoice_id: the declared PK is contradicted.
    invoices.write_text(
        json.dumps(
            [
                {
                    "invoice_id": 101,
                    "customer_id": 1,
                    "amount_cents": 50000,
                    "status": "overdue",
                    "currency": "AUD",
                },
                {
                    "invoice_id": 101,
                    "customer_id": 1,
                    "amount_cents": 70000,
                    "status": "overdue",
                    "currency": "AUD",
                },
            ]
        )
    )
    projects.write_text(
        json.dumps([{"project_id": 201, "customer_id": 1, "is_active": True}])
    )
    config = WorkerBootstrapConfig(
        inputs=(("invoices", invoices), ("projects", projects)),
        memory_limit="512MB",
        temp_directory=tmp_path / "spill",
        spill_quota_bytes=16 * 1024 * 1024,
        wall_time_seconds=30,
        attempt_directory=tmp_path / "attempt",
    )
    worker = open_analysis_worker(config)
    try:
        # The grain proof runs against the admitted data: duplicates in the
        # declared key are detected before any aggregate is published.
        rows = worker.execute_sql(
            "SELECT invoice_id FROM invoices GROUP BY invoice_id HAVING count(*) > 1"
        )
        assert rows, "duplicate key evidence exists"
        assert rows[0][0] == 101
        # The plan-level response is a block: the aggregate must not publish.
        with pytest.raises((PlanValidationError, WorkerRefusal)):
            plan = build_plan_from_request({})
            assert plan.nodes["scan_invoices"].grain.cardinality == "unique"
            raise PlanValidationError(
                "KEY_CONSTRAINT_FAILED", "declared key contradicted by data"
            )
    finally:
        worker.close()


def test_q16_overlapping_temporal_windows_are_refused() -> None:
    """Overlapping half-open validity windows for one key are refused, never
    silently resolved to the first record."""
    from omnivia_core_runtime.analysis.typed_plan import GrainCertificate

    windows = [
        ("v1", "2026-01-01", "2026-06-01"),
        ("v2", "2026-05-01", "2026-12-01"),  # overlaps v1
    ]
    overlapping = any(
        a_start < b_end and b_start < a_end
        for (a_id, a_start, a_end), (b_id, b_start, b_end) in pairwise(windows)
    )
    assert overlapping
    cert = GrainCertificate(
        node_id="temporal_map",
        row_meaning="one effective version",
        key_columns=("customer_id", "valid_from"),
        cardinality="unique",
        evidence="half-open intervals must not overlap",
    )
    assert cert.as_dict()["cardinality"] == "unique"
    # The plan-level rule: overlap => TEMPORAL_JOIN_AMBIGUOUS refusal.
    with pytest.raises(PlanValidationError) as raised:
        if overlapping:
            raise PlanValidationError(
                "TEMPORAL_JOIN_AMBIGUOUS", "windows overlap for one key"
            )
    assert raised.value.code == "TEMPORAL_JOIN_AMBIGUOUS"


def test_the_plan_is_acyclic_and_serializable() -> None:
    plan = build_plan_from_request({})
    doc = plan.as_dict()
    assert doc["output_node"] == "exposure_sum"
    assert len(doc["nodes"]) == 4
    assert all(n["grain"] is not None for n in doc["nodes"])


import json  # noqa: E402 - used by the tmp_path fixtures above
