from __future__ import annotations

import warnings

import pytest

from benchmarks.registry import get_registry
from benchmarks.thresholds import RuntimeSloThresholds, evaluate_runtime_slo


EXPECTED_SCENARIOS = {
    "create_memory",
    "retrieve_memory",
    "update_memory",
    "delete_memory",
    "keyword_search",
    "tag_filter",
    "source_filter",
    "graph_linking",
    "graph_traversal_1_hop",
    "graph_traversal_2_hop",
    "import_json",
    "export_json",
    "mixed_workload",
    "control_plane_runtime_load_soak",
}

RUNTIME_SLO_SCENARIO = "control_plane_runtime_load_soak"


def test_all_expected_scenarios_are_registered() -> None:
    import benchmarks.scenarios  # noqa: F401

    assert EXPECTED_SCENARIOS.issubset(set(get_registry().names()))


@pytest.mark.parametrize("scenario_name", sorted(EXPECTED_SCENARIOS))
def test_scenario_smoke_runs_on_synthetic_temp_data(scenario_name: str) -> None:
    import benchmarks.scenarios  # noqa: F401

    scenario = get_registry().get(scenario_name)
    assert scenario is not None

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        result = scenario.func(":memory:", 5)

    assert result["item_count"] > 0
    assert result["duration"] >= 0
    assert result["ops_per_second"] >= 0
    assert result.get("error") is None


@pytest.fixture(scope="module")
def runtime_slo_result() -> dict:
    import benchmarks.scenarios  # noqa: F401

    scenario = get_registry().get(RUNTIME_SLO_SCENARIO)
    assert scenario is not None
    return scenario.func(":memory:", 12)


def test_runtime_load_soak_emits_storage_and_threshold_evidence(
    runtime_slo_result: dict,
) -> None:
    slo = runtime_slo_result["slo"]

    assert slo["operation_count"] == 12
    assert slo["completed_count"] == 12
    assert slo["database_bytes"] > 0
    assert slo["storage_bytes_per_run"] > 0
    assert slo["thresholds"] == {
        field: getattr(RuntimeSloThresholds(), field)
        for field in RuntimeSloThresholds.__dataclass_fields__
    }
    assert slo["breaches"] == []
    assert slo["status"] == "pass"
    assert len(runtime_slo_result["latency_ms_samples"]) == 12
    assert runtime_slo_result["database_size_mb"] > 0


def test_runtime_load_soak_projection_and_metrics_counts_agree(
    runtime_slo_result: dict,
) -> None:
    slo = runtime_slo_result["slo"]

    assert slo["metrics_run_count"] == slo["operation_count"]
    assert slo["metrics_completed_count"] == slo["completed_count"]
    assert slo["metrics_failed_count"] == 0
    assert slo["projection_metrics_completed_count"] == slo["completed_count"]
    assert slo["projection_span_count"] >= slo["completed_count"]
    assert slo["redaction_violations"] == []


def test_runtime_slo_fails_closed_on_unbounded_storage_growth(
    runtime_slo_result: dict,
) -> None:
    evidence = dict(runtime_slo_result["slo"])
    evidence["storage_bytes_per_run"] = (
        RuntimeSloThresholds().max_storage_bytes_per_run + 1
    )

    breaches = evaluate_runtime_slo(evidence)

    assert any("storage_bytes_per_run" in breach for breach in breaches)


def test_runtime_slo_fails_closed_on_dropped_and_inconsistent_runs() -> None:
    evidence = {
        "operation_count": 10,
        "completed_count": 9,
        "storage_bytes_per_run": 1024.0,
        "p99_latency_ms": 5.0,
        "throughput_ops_per_second": 100.0,
        "projection_span_count": 0,
        "metrics_completed_count": 8,
        "projection_metrics_completed_count": 7,
        "redaction_violations": ["projection leaked forbidden term 'secret://'"],
    }

    breaches = evaluate_runtime_slo(evidence)

    assert any("completed ratio" in breach for breach in breaches)
    assert any("projected spans per completed run" in breach for breach in breaches)
    assert any("does not match projection completed_count" in breach for breach in breaches)
    assert any("secret://" in breach for breach in breaches)
    assert evaluate_runtime_slo({"operation_count": 0}) == [
        "operation_count is zero: no runtime evidence recorded"
    ]
