"""Threshold comparison logic for benchmark results.

Provides utilities to compare benchmark results against baselines and
apply warning/fail thresholds to determine pass/fail status.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from benchmarks.schema import (
    BenchmarkRun,
    ComparisonResult,
    ScenarioComparison,
    ScenarioResult,
)


# Default thresholds (percentage)
DEFAULT_WARNING_THRESHOLD = 10.0  # 10% slowdown triggers warning
DEFAULT_FAIL_THRESHOLD = 25.0  # 25% slowdown triggers failure


def percentile(values: list[float], percentile_rank: int) -> float:
    """Return a percentile from an unsorted sample list (0.0 when empty)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(
        len(ordered) - 1,
        round((percentile_rank / 100) * (len(ordered) - 1)),
    )
    return ordered[index]


@dataclass(frozen=True)
class RuntimeSloThresholds:
    """Absolute pass/fail bounds for the control-plane runtime load/soak gate.

    These are baseline-free bounds: they exist to catch unbounded storage
    growth, dropped runs, and pathological slowness on an ordinary developer
    laptop, not to police small timing variance. Regression-vs-baseline
    tracking stays with :class:`ThresholdConfig`.

    Observed local-dev values for the ``tiny`` profile are roughly 9-14 KiB of
    SQLite growth per completed run and 5-50 ms per ingest+execute pair, so the
    defaults keep several times that headroom.
    """

    max_storage_bytes_per_run: int = 65_536
    max_p99_latency_ms: float = 750.0
    min_throughput_ops_per_second: float = 2.0
    min_completed_ratio: float = 1.0
    min_spans_per_completed_run: float = 1.0


def evaluate_runtime_slo(
    evidence: Mapping[str, Any],
    thresholds: RuntimeSloThresholds | None = None,
) -> list[str]:
    """Return threshold breaches for a runtime SLO evidence mapping.

    Fails closed: missing or zero evidence counts are a breach rather than a
    silent pass, so a scenario that records nothing cannot report success.

    Args:
        evidence: Mapping produced by a runtime scenario. Recognised keys are
            ``operation_count``, ``completed_count``, ``storage_bytes_per_run``,
            ``p99_latency_ms``, ``throughput_ops_per_second``,
            ``projection_span_count``, ``metrics_completed_count``,
            ``projection_metrics_completed_count`` and
            ``redaction_violations``.
        thresholds: Bounds to enforce (defaults to
            :class:`RuntimeSloThresholds`).

    Returns:
        List of human-readable breach descriptions; empty means pass.
    """
    limits = thresholds or RuntimeSloThresholds()
    breaches: list[str] = []

    operation_count = int(evidence.get("operation_count", 0))
    completed_count = int(evidence.get("completed_count", 0))
    if operation_count <= 0:
        breaches.append("operation_count is zero: no runtime evidence recorded")
        return breaches

    completed_ratio = completed_count / operation_count
    if completed_ratio < limits.min_completed_ratio:
        breaches.append(
            f"completed ratio {completed_ratio:.3f} below "
            f"{limits.min_completed_ratio:.3f} "
            f"({completed_count}/{operation_count} runs completed)"
        )

    storage_per_run = float(evidence.get("storage_bytes_per_run", 0))
    if storage_per_run > limits.max_storage_bytes_per_run:
        breaches.append(
            f"storage_bytes_per_run {storage_per_run:,.0f} exceeds "
            f"{limits.max_storage_bytes_per_run:,} bytes"
        )

    p99 = float(evidence.get("p99_latency_ms", 0))
    if p99 > limits.max_p99_latency_ms:
        breaches.append(
            f"p99_latency_ms {p99:,.1f} exceeds {limits.max_p99_latency_ms:,.1f} ms"
        )

    throughput = float(evidence.get("throughput_ops_per_second", 0))
    if throughput < limits.min_throughput_ops_per_second:
        breaches.append(
            f"throughput_ops_per_second {throughput:,.2f} below "
            f"{limits.min_throughput_ops_per_second:,.2f}"
        )

    if completed_count > 0:
        spans_per_run = float(evidence.get("projection_span_count", 0)) / completed_count
        if spans_per_run < limits.min_spans_per_completed_run:
            breaches.append(
                f"projected spans per completed run {spans_per_run:.3f} below "
                f"{limits.min_spans_per_completed_run:.3f}"
            )

    metrics_completed = int(evidence.get("metrics_completed_count", -1))
    projection_completed = int(evidence.get("projection_metrics_completed_count", -2))
    if metrics_completed != projection_completed:
        breaches.append(
            f"observability summary completed_count {metrics_completed} does not "
            f"match projection completed_count {projection_completed}"
        )
    if metrics_completed != completed_count:
        breaches.append(
            f"observability summary completed_count {metrics_completed} does not "
            f"match executed completed runs {completed_count}"
        )

    breaches.extend(str(item) for item in evidence.get("redaction_violations", []))

    return breaches


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
