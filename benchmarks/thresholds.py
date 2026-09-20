"""Threshold comparison logic for benchmark results.

Provides utilities to compare benchmark results against baselines and
apply warning/fail thresholds to determine pass/fail status.
"""

from __future__ import annotations

from dataclasses import dataclass
from benchmarks.schema import (
    BenchmarkRun,
    ComparisonResult,
    ScenarioComparison,
    ScenarioResult,
)


# Default thresholds (percentage)
DEFAULT_WARNING_THRESHOLD = 10.0  # 10% slowdown triggers warning
DEFAULT_FAIL_THRESHOLD = 25.0  # 25% slowdown triggers failure


@dataclass
class ThresholdConfig:
    """Configuration for comparison thresholds.

    Attributes:
        warning_threshold: Percentage slowdown to trigger warning (default 10%)
        fail_threshold: Percentage slowdown to trigger failure (default 25%)
    """

    warning_threshold: float = DEFAULT_WARNING_THRESHOLD
    fail_threshold: float = DEFAULT_FAIL_THRESHOLD


def calculate_percentage_change(baseline_value: float, latest_value: float) -> float:
    """Calculate percentage change between baseline and latest.

    A negative value means regression (slower), positive means improvement.

    Args:
        baseline_ops: Baseline operations per second
        latest_ops: Latest operations per second

    Returns:
        Percentage change ((latest - baseline) / baseline * 100)
    """
    if baseline_value == 0:
        return 0.0
    return ((latest_value - baseline_value) / baseline_value) * 100


def determine_status(
    percentage_change: float,
    warning_threshold: float = DEFAULT_WARNING_THRESHOLD,
    fail_threshold: float = DEFAULT_FAIL_THRESHOLD,
) -> str:
    """Determine pass/warning/fail status based on percentage change.

    Args:
        percentage_change: Percentage change from baseline
        warning_threshold: Threshold for warning (default 10%)
        fail_threshold: Threshold for failure (default 25%)

    Returns:
        "pass", "warning", or "fail"
    """
    if percentage_change >= 0:
        return "pass"  # Improvement or no change

    # Negative change means regression
    abs_change = abs(percentage_change)

    if abs_change >= fail_threshold:
        return "fail"
    elif abs_change >= warning_threshold:
        return "warning"
    else:
        return "pass"


def compare_scenario_results(
    baseline: ScenarioResult,
    latest: ScenarioResult,
    config: ThresholdConfig | None = None,
    metric: str = "throughput_ops_per_second",
) -> ScenarioComparison:
    """Compare two scenario results.

    Args:
        baseline: Baseline scenario result
        latest: Latest scenario result
        config: Threshold configuration (optional)

    Returns:
        ScenarioComparison with percentage change and status
    """
    if config is None:
        config = ThresholdConfig()

    baseline_value = float(getattr(baseline, metric))
    latest_value = float(getattr(latest, metric))
    percentage_change = calculate_percentage_change(baseline_value, latest_value)
    # Higher throughput is better; lower latency/memory/db size is better.
    if metric != "throughput_ops_per_second":
        percentage_change = -percentage_change
    status = determine_status(
        percentage_change,
        warning_threshold=config.warning_threshold,
        fail_threshold=config.fail_threshold,
    )

    return ScenarioComparison(
        scenario_name=latest.name,
        metric=metric,
        baseline_value=baseline_value,
        latest_value=latest_value,
        percentage_change=percentage_change,
        status=status,
    )


def compare_runs(
    baseline_run: BenchmarkRun,
    latest_run: BenchmarkRun,
    config: ThresholdConfig | None = None,
) -> ComparisonResult:
    """Compare two complete benchmark runs.

    Args:
        baseline_run: Baseline benchmark run
        latest_run: Latest benchmark run
        config: Threshold configuration (optional)

    Returns:
        ComparisonResult with all scenario comparisons
    """
    if config is None:
        config = ThresholdConfig()

    comparisons = []

    # Match scenarios by name
    latest_scenarios = {s.name: s for s in latest_run.scenarios}
    metrics = [
        "mean_latency_ms",
        "p95_latency_ms",
        "p99_latency_ms",
        "throughput_ops_per_second",
    ]

    for baseline_scenario in baseline_run.scenarios:
        latest_scenario = latest_scenarios.get(baseline_scenario.name)
        if latest_scenario is None:
            continue  # Scenario not in latest run

        for metric in metrics:
            comparisons.append(
                compare_scenario_results(baseline_scenario, latest_scenario, config, metric)
            )

    return ComparisonResult(
        baseline_run=baseline_run,
        latest_run=latest_run,
        profile=latest_run.profile,
        comparisons=comparisons,
    )


def get_summary_status(comparisons: list[ScenarioComparison]) -> str:
    """Get overall summary status from a list of comparisons.

    Args:
        comparisons: List of scenario comparisons

    Returns:
        "pass" if all pass, "warning" if any warning, "fail" if any failure
    """
    if not comparisons:
        return "pass"

    statuses = [c.status for c in comparisons]

    if "fail" in statuses:
        return "fail"
    elif "warning" in statuses:
        return "warning"
    else:
        return "pass"


def format_comparison_summary(comparison: ComparisonResult) -> str:
    """Format a comparison result as a human-readable summary.

    Args:
        comparison: Comparison result to format

    Returns:
        Formatted string summary
    """
    lines = [
        f"=== Benchmark Comparison ({comparison.profile}) ===",
        f"Baseline: {comparison.baseline_run.run_id[:8]} "
        f"({comparison.baseline_run.timestamp[:10]})",
        f"Latest:   {comparison.latest_run.run_id[:8]} "
        f"({comparison.latest_run.timestamp[:10]})",
        "",
    ]

    overall_status = get_summary_status(comparison.comparisons)
    lines.append(f"Overall Status: {overall_status.upper()}")
    lines.append("")

    for comp in comparison.comparisons:
        sign = "+" if comp.percentage_change >= 0 else ""
        lines.append(
            f"  {comp.scenario_name}: {sign}{comp.percentage_change:.1f}% "
            f"{comp.metric} ({comp.status.upper()})"
        )
        lines.append(f"    Baseline: {comp.baseline_value:.3f}")
        lines.append(f"    Latest:   {comp.latest_value:.3f}")

    return "\n".join(lines)
