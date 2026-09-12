# OmniVia Core Benchmarks

Performance benchmarking system for OmniVia Core memory operations.

## Overview

This package provides a comprehensive benchmarking framework for measuring and tracking the performance of OmniVia Core memory operations. It includes:

- **Benchmark Runner**: CLI tool to execute benchmark scenarios
- **Scenario Registry**: Centralized management of benchmark scenarios
- **Result Schema**: Structured data model for benchmark results
- **Comparison Tools**: Compare results against baselines with configurable thresholds
- **Export Formats**: JSON, Markdown, and CSV export options

## Quick Start

### List Available Scenarios

```bash
scripts/run-core-benchmarks.sh --list
```

### Run Benchmarks

Run all scenarios with the tiny profile (100 items):

```bash
scripts/run-core-benchmarks.sh
```

Run with a specific profile:

```bash
scripts/run-core-benchmarks.sh --profile small
```

Run specific scenarios:

```bash
scripts/run-core-benchmarks.sh --scenario create_memory --scenario retrieve_memory
```

The helper prepends `services/omnivia-memory/src` to `PYTHONPATH` before
executing the Python runner. This avoids accidentally importing `omnivia_memory`
from an editable install or a stale worktree.

### Export Results

Export to different formats:

```bash
# JSON (default)
scripts/run-core-benchmarks.sh

# Markdown
scripts/run-core-benchmarks.sh --format markdown

# CSV
scripts/run-core-benchmarks.sh --format csv

# All formats
scripts/run-core-benchmarks.sh --format all
```

### Compare Against Baseline

```bash
python -m benchmarks.runner.benchmark_compare --profile tiny
```

Set a baseline from a previous run:

```bash
python -m benchmarks.runner.benchmark_compare --latest benchmarks/reports/benchmark_tiny_20240101.json --set-baseline
```

## Profiles

| Profile | Item Count | Use Case |
|---------|------------|----------|
| tiny | 100 | Quick smoke tests |
| small | 1,000 | Development testing |
| medium | 10,000 | CI/CD validation |
| large | 100,000 | Full validation |
| stress | 1,000,000 | Stress testing |

## Scenarios

### Memory Operations
- `create_memory` - Create memories in batch
- `retrieve_memory` - Retrieve memories by ID
- `update_memory` - Update existing memories
- `delete_memory` - Delete memories

### Search Operations
- `keyword_search` - Keyword-based search
- `tag_filter` - Tag-based filtering (placeholder)
- `source_filter` - Source-based filtering

### Graph Operations
- `graph_linking` - Entity creation and relationship linking
- `graph_traversal_1_hop` - Direct neighbor queries
- `graph_traversal_2_hop` - Multi-hop traversal (placeholder)

### Import/Export
- `import_json` - JSON import operations
- `export_json` - JSON export operations

### Mixed Workloads
- `mixed_workload` - Combined read/write operations

## Result Schema

Benchmark results are stored as structured JSON with the following top-level fields:

- `run_id`: Unique identifier for this run
- `timestamp`: ISO 8601 timestamp
- `git_commit`: Git commit hash when available
- `profile`: Profile size used
- `scenarios`: Array of scenario results
- `environment`: System environment info
- `comparison`: Comparison and run metadata, including `total_duration_ms`

Each scenario result includes:
- `name`: Name of the scenario
- `status`: `pass`, `warning`, `regression`, `failed`, `improved`, or `no_baseline`
- `operation_count`: Number of operations measured
- `total_duration_ms`: Scenario wall-clock duration
- `mean_latency_ms`, `median_latency_ms`, `p95_latency_ms`, `p99_latency_ms`
- `throughput_ops_per_second`: Throughput metric
- `memory_peak_mb`: Peak memory usage when tracked
- `database_size_mb`: Database size when tracked
- `error_count`: Number of errors
- `warnings`: Scenario limitations or non-fatal warnings

## Directory Structure

```
benchmarks/
├── __init__.py           # Package exports
├── schema.py             # Result data models
├── dataset.py            # Deterministic data generation
├── registry.py           # Scenario registry
├── thresholds.py         # Comparison thresholds
├── report.py             # Export utilities
├── scenarios/            # Benchmark scenario implementations
│   └── __init__.py
├── runner/               # CLI entrypoints
│   ├── __init__.py
│   ├── benchmark_runner.py
│   └── benchmark_compare.py
├── tests/                # Test suite
│   ├── __init__.py
│   ├── test_benchmark_runner.py
│   └── test_benchmark_scenarios.py
├── reports/              # Generated reports (gitkeep)
└── baselines/            # Baseline results for comparison
    └── README.md
```

## Placeholder Scenarios

Some scenarios are marked as placeholders because the underlying Core API is not yet available:

- `tag_filter` - No dedicated tag filter API exists
- `graph_traversal_2_hop` - No native multi-hop traversal API

These scenarios use workaround implementations and emit warnings. They remain operational to avoid breaking the benchmark runner.

## Writing Custom Scenarios

Register a new scenario using the `@scenario` decorator:

```python
from benchmarks.registry import scenario

@scenario(
    "my_scenario",
    "Description of what this benchmarks",
    tags=["custom", "example"],
    estimated_time_per_item_ms=0.5,
)
def my_scenario(db_path: str, item_count: int) -> dict:
    # Your benchmark code here
    return {
        "item_count": item_count,
        "duration": elapsed_time,
        "ops_per_second": throughput,
        "error": None,  # or error message string
    }
```

## Threshold Configuration

The comparison tool uses warning and fail thresholds:

- **Warning threshold**: 10% performance regression
- **Fail threshold**: 25% performance regression

Configure custom thresholds:

```bash
python -m benchmarks.runner.benchmark_compare \
    --warning-threshold 15 \
    --fail-threshold 30
```

## Local Regression Check

Use the helper script for the standard local Core performance check:

```bash
scripts/check-core-performance.sh
```

By default it:

- runs the `tiny` benchmark profile
- writes a transient JSON report under `benchmarks/reports`
- compares that report against the latest `tiny` baseline under `benchmarks/baselines`
- writes comparison JSON and Markdown under `benchmarks/reports`
- exits `2` when the fail threshold is exceeded

Warnings use the default comparison behavior: they are reported but do not fail
the command unless `OMNIVIA_BENCHMARK_FAIL_ON_WARNING=1` is set.

Configuration:

```bash
OMNIVIA_BENCHMARK_PROFILE=tiny \
OMNIVIA_BENCHMARK_WARNING_THRESHOLD=10 \
OMNIVIA_BENCHMARK_FAIL_THRESHOLD=25 \
scripts/check-core-performance.sh
```

Generated files in `benchmarks/reports` are temporary working evidence. Keep
baselines in `benchmarks/baselines` when a result is intentionally promoted.

Baseline promotion and CI gate rules are defined in
[`benchmarks/baselines/GOVERNANCE.md`](baselines/GOVERNANCE.md). Until a
reference environment and variance policy are defined, local check failures
should trigger review rather than automatic baseline replacement.

## Informational CI Report

The repository includes a non-blocking GitHub Actions workflow:

```text
.github/workflows/core-performance-report.yml
```

It runs on manual dispatch and on pull requests that touch Core benchmark,
benchmark helper, or memory-service paths. The workflow runs benchmark tests,
executes `scripts/check-core-performance.sh` for the `tiny` profile, uploads
generated reports as the `core-performance-reports` artifact, and writes a
short job summary.

The performance comparison step uses `continue-on-error`, so regression results
are visible without blocking merges. Do not convert this workflow into a hard
gate until the baseline governance policy defines the reference runner, repeat
count, variance tolerance, and promotion rules for CI.

## Testing

Run the test suite:

```bash
pytest benchmarks/tests/ -v
```

Run specific tests:

```bash
pytest benchmarks/tests/test_benchmark_runner.py -v
pytest benchmarks/tests/test_benchmark_scenarios.py -v
```
