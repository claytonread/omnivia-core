from __future__ import annotations

import json

from benchmarks.dataset import PROFILE_SIZES, get_item_count
from benchmarks.report import export_csv, export_json, export_markdown, generate_summary
from benchmarks.runner.benchmark_compare import main as compare_main
from benchmarks.runner.benchmark_runner import export_results, run_benchmarks
from benchmarks.schema import BenchmarkRun, EnvironmentInfo, ScenarioResult
from benchmarks.thresholds import ThresholdConfig, compare_runs


def scenario_result(name: str = "create_memory", throughput: float = 100.0) -> ScenarioResult:
    return ScenarioResult(
        name=name,
        status="pass",
        profile="tiny",
        operation_count=100,
        total_duration_ms=1000.0,
        mean_latency_ms=10.0,
        median_latency_ms=9.0,
        p95_latency_ms=15.0,
        p99_latency_ms=20.0,
        throughput_ops_per_second=throughput,
        memory_peak_mb=5.0,
        database_size_mb=1.0,
    )


def test_environment_info_uses_required_schema_names() -> None:
    env = EnvironmentInfo()
    data = env.to_dict()

    assert data["os"]
    assert "cpu" in data
    assert "memory_gb" in data
    assert data["python_version"]
    assert "node_version" in data


def test_scenario_result_serializes_required_metrics() -> None:
    result = scenario_result()
    restored = ScenarioResult.from_dict(result.to_dict())

    assert restored.name == "create_memory"
    assert restored.status == "pass"
    assert restored.operation_count == 100
    assert restored.p95_latency_ms == 15.0
    assert restored.throughput_ops_per_second == 100.0


def test_benchmark_run_round_trips() -> None:
    run = BenchmarkRun(profile="tiny", git_commit="abc123")
    run.comparison["total_duration_ms"] = 1000
    run.add_scenario_result(scenario_result())

    restored = BenchmarkRun.from_dict(run.to_dict())

    assert restored.git_commit == "abc123"
    assert restored.get_scenario_result("create_memory") is not None
    assert restored.comparison["total_duration_ms"] == 1000


def test_exports_use_shared_schema() -> None:
    run = BenchmarkRun(profile="tiny")
    run.add_scenario_result(scenario_result())

    parsed = json.loads(export_json(run))
    markdown = export_markdown(run)
    csv = export_csv(run)
    summary = generate_summary(run)

    assert parsed["scenarios"][0]["p99_latency_ms"] == 20.0
    assert "p95" in markdown.lower()
    assert "throughput_ops_per_second" in csv
    assert summary["total_duration_ms"] == 0


def test_export_results_all_writes_every_format(tmp_path) -> None:
    run = BenchmarkRun(profile="tiny")
    run.add_scenario_result(scenario_result())

    exported = export_results(run, tmp_path, ["all"])

    assert {path.suffix for path in exported} == {".csv", ".json", ".md"}


def test_threshold_compare_checks_multiple_metrics() -> None:
    baseline = BenchmarkRun(profile="tiny")
    latest = BenchmarkRun(profile="tiny")
    baseline.add_scenario_result(scenario_result(throughput=100.0))
    latest.add_scenario_result(scenario_result(throughput=70.0))

    comparison = compare_runs(baseline, latest, ThresholdConfig())

    assert comparison.comparisons
    throughput = [
        item
        for item in comparison.comparisons
        if item.metric == "throughput_ops_per_second"
    ][0]
    assert throughput.status == "fail"


def test_compare_cli_returns_distinct_regression_exit_code(tmp_path) -> None:
    baseline = BenchmarkRun(profile="tiny")
    latest = BenchmarkRun(profile="tiny")
    baseline.add_scenario_result(scenario_result(throughput=100.0))
    latest.add_scenario_result(scenario_result(throughput=70.0))

    baseline_path = tmp_path / "baseline_tiny_test.json"
    latest_path = tmp_path / "benchmark_tiny_test.json"
    baseline_path.write_text(export_json(baseline))
    latest_path.write_text(export_json(latest))

    exit_code = compare_main([
        "--baseline",
        str(baseline_path),
        "--latest",
        str(latest_path),
        "--format",
        "json",
        "--quiet",
    ])

    assert exit_code == 2


def test_profile_sizes() -> None:
    assert PROFILE_SIZES["tiny"] == 100
    assert PROFILE_SIZES["stress"] == 1_000_000
    assert get_item_count("unknown") == 100


def test_runner_smoke_single_scenario() -> None:
    run = run_benchmarks(profile="tiny", scenario_names=["create_memory"], quiet=True)

    assert run.profile == "tiny"
    assert len(run.scenarios) == 1
    result = run.scenarios[0]
    assert result.name == "create_memory"
    assert result.operation_count == 100
    assert result.throughput_ops_per_second > 0
