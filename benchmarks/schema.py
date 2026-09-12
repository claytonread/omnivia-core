"""Benchmark result schema for OmniVia Core performance evidence."""

from __future__ import annotations

import platform
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class EnvironmentInfo:
    """Environment information captured at benchmark run time."""

    os: str = field(default_factory=lambda: platform.platform())
    cpu: str = field(default_factory=lambda: platform.processor() or platform.machine())
    memory_gb: float | None = None
    python_version: str = field(default_factory=lambda: platform.python_version())
    node_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EnvironmentInfo:
        """Create from dictionary."""
        return cls(**data)


@dataclass
class ScenarioResult:
    """Result for a single benchmark scenario run."""

    name: str
    status: str
    profile: str
    operation_count: int
    total_duration_ms: float
    mean_latency_ms: float
    median_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    throughput_ops_per_second: float
    memory_peak_mb: float | None = None
    database_size_mb: float | None = None
    error_count: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScenarioResult:
        """Create from dictionary."""
        return cls(**data)


@dataclass
class BenchmarkRun:
    """Complete benchmark run result with all scenarios."""

    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    git_commit: str | None = None
    profile: str = "tiny"
    scenarios: list[ScenarioResult] = field(default_factory=list)
    environment: EnvironmentInfo = field(default_factory=EnvironmentInfo)
    comparison: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "git_commit": self.git_commit,
            "profile": self.profile,
            "scenarios": [s.to_dict() for s in self.scenarios],
            "environment": self.environment.to_dict(),
            "comparison": self.comparison,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BenchmarkRun:
        """Create from dictionary."""
        return cls(
            run_id=data["run_id"],
            timestamp=data["timestamp"],
            git_commit=data.get("git_commit"),
            profile=data["profile"],
            scenarios=[ScenarioResult.from_dict(s) for s in data["scenarios"]],
            environment=EnvironmentInfo.from_dict(data["environment"]),
            comparison=data.get("comparison", {}),
        )

    def add_scenario_result(self, result: ScenarioResult) -> None:
        """Add a scenario result to this run."""
        self.scenarios.append(result)

    def get_scenario_result(self, scenario_name: str) -> ScenarioResult | None:
        """Get a specific scenario result by name."""
        for result in self.scenarios:
            if result.name == scenario_name:
                return result
        return None


@dataclass
class ComparisonResult:
    """Comparison between baseline and latest benchmark results."""

    baseline_run: BenchmarkRun
    latest_run: BenchmarkRun
    profile: str
    comparisons: list[ScenarioComparison] = field(default_factory=list)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "baseline_run": self.baseline_run.to_dict(),
            "latest_run": self.latest_run.to_dict(),
            "profile": self.profile,
            "comparisons": [c.to_dict() for c in self.comparisons],
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ComparisonResult:
        """Create from dictionary."""
        return cls(
            baseline_run=BenchmarkRun.from_dict(data["baseline_run"]),
            latest_run=BenchmarkRun.from_dict(data["latest_run"]),
            profile=data["profile"],
            comparisons=[
                ScenarioComparison.from_dict(c) for c in data["comparisons"]
            ],
            timestamp=data["timestamp"],
        )


@dataclass
class ScenarioComparison:
    """Comparison between baseline and latest for a single scenario."""

    scenario_name: str
    metric: str
    baseline_value: float
    latest_value: float
    percentage_change: float
    status: str

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScenarioComparison:
        """Create from dictionary."""
        return cls(**data)


def get_git_commit() -> str | None:
    """Return the current git commit hash when available."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None
    return result.stdout.strip() or None
