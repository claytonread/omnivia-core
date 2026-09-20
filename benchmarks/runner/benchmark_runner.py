"""Main benchmark runner CLI.

Provides the entry point for running benchmark scenarios. Can be executed as:
    python -m benchmarks.runner.benchmark_runner

Features:
- Run all scenarios or specific ones
- Profile selection (tiny, small, medium, large, stress)
- JSON/Markdown/CSV export
- Baseline comparison (if available)
"""

from __future__ import annotations

import argparse
import sys
import statistics
import time
import tracemalloc
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from benchmarks.dataset import PROFILE_SIZES, get_item_count
from benchmarks.report import export_csv_file, export_json_file, export_markdown_file
from benchmarks.registry import get_registry
from benchmarks.schema import BenchmarkRun, EnvironmentInfo, ScenarioResult, get_git_commit

# Import scenarios to register them
from benchmarks.scenarios import *  # noqa: F401, F403


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments.

    Args:
        args: Arguments to parse (defaults to sys.argv)

    Returns:
        Parsed arguments namespace
    """
    parser = argparse.ArgumentParser(
        description="Run OmniVia Core benchmark scenarios",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--profile",
        "-p",
        choices=list(PROFILE_SIZES.keys()),
        default="tiny",
        help="Benchmark profile size (default: tiny)",
    )
    parser.add_argument(
        "--scenario",
        "-s",
        action="append",
        dest="scenarios",
        help="Specific scenario to run (can be specified multiple times)",
    )
    parser.add_argument(
        "--list",
        "-l",
        action="store_true",
        help="List available scenarios and exit",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        type=Path,
        default=Path(__file__).parent.parent / "reports",
        help="Output directory for results (default: benchmarks/reports)",
    )
    parser.add_argument(
        "--format",
        "-f",
        choices=["json", "markdown", "csv", "all"],
        default="json",
        help="Output format (default: json)",
    )
    parser.add_argument(
        "--no-export",
        action="store_true",
        help="Don't export results to files",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress progress output",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show detailed output including scenario descriptions",
    )

    return parser.parse_args(args)


def list_scenarios(verbose: bool = False) -> None:
    """List all registered scenarios.

    Args:
        verbose: Show descriptions and tags if True
    """
    registry = get_registry()
    scenarios = registry.list()

    if not scenarios:
        print("No scenarios registered.")
        return

    print(f"Registered scenarios ({len(scenarios)}):")
    print()

    for info in scenarios:
        print(f"  {info.name}")
        if verbose:
            print(f"    {info.description}")
            if info.tags:
                print(f"    Tags: {', '.join(info.tags)}")


def run_scenario(
    scenario_name: str,
    item_count: int,
    db_path: str = ":memory:",
) -> ScenarioResult | None:
    """Run a single benchmark scenario.

    Args:
        scenario_name: Name of the scenario to run
        item_count: Number of items for the benchmark
        db_path: Database path (currently unused, scenarios create their own)

    Returns:
        ScenarioResult with timing and throughput data
    """
    registry = get_registry()
    info = registry.get(scenario_name)

    if info is None:
        print(f"Error: Unknown scenario '{scenario_name}'")
        return None

    try:
        result = info.func(db_path, item_count)
    except Exception as e:
        return ScenarioResult(
            name=scenario_name,
            status="failed",
            profile="unknown",
            operation_count=0,
            total_duration_ms=0,
            mean_latency_ms=0,
            median_latency_ms=0,
            p95_latency_ms=0,
            p99_latency_ms=0,
            throughput_ops_per_second=0,
            error_count=1,
            warnings=[str(e)],
            database_size_mb=None,
            memory_peak_mb=None,
        )

    # Extract metrics from result dict
    duration_seconds = float(result.get("duration", 0))
    total_duration_ms = duration_seconds * 1000
    ops_per_second = float(result.get("ops_per_second", 0))
    raw_operations_count = result.get("operations_count")
    if raw_operations_count is None:
        raw_operations_count = result.get(f"{scenario_name}_count", item_count)
    operation_count = int(result.get("operation_count") or raw_operations_count or item_count)
    warnings = list(result.get("warnings", []))
    if result.get("error"):
        warnings.append(str(result["error"]))
    latencies = result.get("latency_ms_samples") or []
    if latencies:
        sorted_latencies = sorted(float(value) for value in latencies)
        mean_latency = statistics.fmean(sorted_latencies)
        median_latency = statistics.median(sorted_latencies)
        p95_latency = _percentile(sorted_latencies, 95)
        p99_latency = _percentile(sorted_latencies, 99)
    elif operation_count > 0:
        mean_latency = total_duration_ms / operation_count
        median_latency = mean_latency
        p95_latency = mean_latency
        p99_latency = mean_latency
    else:
        mean_latency = median_latency = p95_latency = p99_latency = 0

    return ScenarioResult(
        name=scenario_name,
        status="failed" if result.get("error") else "pass",
        profile="tiny",
        operation_count=operation_count,
        total_duration_ms=total_duration_ms,
        mean_latency_ms=mean_latency,
        median_latency_ms=median_latency,
        p95_latency_ms=p95_latency,
        p99_latency_ms=p99_latency,
        throughput_ops_per_second=ops_per_second,
        memory_peak_mb=result.get("memory_peak_mb") or result.get("memory_mb"),
        database_size_mb=result.get("database_size_mb"),
        error_count=1 if result.get("error") else int(result.get("error_count", 0)),
        warnings=warnings,
    )


def _percentile(sorted_values: list[float], percentile: int) -> float:
    """Return a percentile from a sorted sample list."""
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, round((percentile / 100) * (len(sorted_values) - 1)))
    return sorted_values[index]


def run_benchmarks(
    profile: str = "tiny",
    scenario_names: list[str] | None = None,
    quiet: bool = False,
    verbose: bool = False,
) -> BenchmarkRun:
    """Run benchmark scenarios.

    Args:
        profile: Profile size to use
        scenario_names: Specific scenarios to run (None = all)
        quiet: Suppress progress output
        verbose: Show detailed output

    Returns:
        BenchmarkRun with all scenario results
    """
    registry = get_registry()

    # Determine which scenarios to run
    if scenario_names:
        scenarios_to_run = []
        for name in scenario_names:
            if name in registry:
                scenarios_to_run.append(name)
            else:
                print(f"Warning: Scenario '{name}' not found, skipping.")
    else:
        scenarios_to_run = registry.names()

    item_count = get_item_count(profile)

    if not quiet:
        print(f"Running benchmarks with profile '{profile}' ({item_count} items)")
        print(f"Scenarios: {len(scenarios_to_run)}")
        print()

    # Create benchmark run
    run = BenchmarkRun(profile=profile, environment=EnvironmentInfo(), git_commit=get_git_commit())

    start_time = time.perf_counter()

    for name in scenarios_to_run:
        if not quiet:
            status = "Running" if not verbose else f"Running: {name}"
            print(f"  {status}...", end=" ", flush=True)

        scenario_start = time.perf_counter()

        # Track memory if verbose
        if verbose:
            tracemalloc.start()

        result = run_scenario(name, item_count)

        if verbose:
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            if result:
                result.memory_peak_mb = peak / (1024 * 1024)

        scenario_duration = time.perf_counter() - scenario_start

        if result:
            result.profile = profile
            run.add_scenario_result(result)

            if not quiet:
                status_str = "OK" if result.status == "pass" else result.status.upper()
                ops_str = f"{result.throughput_ops_per_second:,.1f} ops/s"
                print(f"{ops_str} ({scenario_duration:.2f}s) - {status_str}")
        else:
            if not quiet:
                print("SKIPPED")

    run.comparison["total_duration_ms"] = (time.perf_counter() - start_time) * 1000

    if not quiet:
        print()
        print(f"Total duration: {run.comparison['total_duration_ms'] / 1000:.2f}s")

    return run


def export_results(
    run: BenchmarkRun,
    output_dir: Path,
    formats: list[str],
) -> list[Path]:
    """Export benchmark results to files.

    Args:
        run: BenchmarkRun to export
        output_dir: Directory for output files
        formats: List of formats to export (json, markdown, csv)

    Returns:
        List of exported file paths
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Generate filename with timestamp
    timestamp = run.timestamp.replace(":", "-").replace(".", "-")
    base_name = f"benchmark_{run.profile}_{timestamp}"

    exported = []

    if "json" in formats or "all" in formats:
        path = output_dir / f"{base_name}.json"
        export_json_file(run, path)
        exported.append(path)

    if "markdown" in formats or "all" in formats:
        path = output_dir / f"{base_name}.md"
        export_markdown_file(run, path)
        exported.append(path)

    if "csv" in formats or "all" in formats:
        path = output_dir / f"{base_name}.csv"
        export_csv_file(run, path)
        exported.append(path)

    return exported


def main(args: list[str] | None = None) -> int:
    """Main entry point for benchmark runner.

    Args:
        args: Command line arguments (defaults to sys.argv)

    Returns:
        Exit code (0 for success, 1 for error)
    """
    parsed = parse_args(args)

    # Handle list mode
    if parsed.list:
        list_scenarios(verbose=parsed.verbose)
        return 0

    # Run benchmarks
    try:
        run = run_benchmarks(
            profile=parsed.profile,
            scenario_names=parsed.scenarios,
            quiet=parsed.quiet,
            verbose=parsed.verbose,
        )
    except Exception as e:
        print(f"Error running benchmarks: {e}", file=sys.stderr)
        return 1

    # Export results
    if not parsed.no_export:
        formats = ["all"] if parsed.format == "all" else [parsed.format]
        exported = export_results(run, parsed.output_dir, formats)

        if not parsed.quiet:
            print()
            print("Exported results:")
            for path in exported:
                print(f"  {path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
