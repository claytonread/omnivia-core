"""Engineering-memory performance qualification (SPEC-CORE-ENGMEM-001 §20.2; plan PR-H2).

The harness is `run_lane`; `validate_report` is the report contract. Ordinary-suite
tests exercise both at a tiny corpus and never assert a wall-clock value. The 10k and
100k lanes are opt-in: `test_engineering_performance_qualification_lane` is skipped
unless ``OMNIVIA_ENGINEERING_QUALIFICATION=1``, because a lane seeds a synthetic
corpus through the production writer before it measures anything.

One lane per invocation, corpus size from ``OMNIVIA_QUALIFICATION_CORPUS`` (default
10 000). Further inputs, all optional: ``OMNIVIA_QUALIFICATION_WORKTREES`` (default 3),
``..._CONFLICT_GROUPS``, ``..._SEARCH_SAMPLES``, ``..._PACK_SAMPLES``,
``..._CHECKPOINT_SAMPLES`` (per payload size class), ``..._COLD_SAMPLES``,
``..._READERS``, ``..._READER_REQUESTS``, ``..._WRITER_CHECKPOINTS``,
``..._STORAGE_CLASS`` (the operator's declaration, e.g. ``local-ssd``: a storage class
cannot be detected safely from the standard library) and ``..._REPORT_DIR``.

The corpus is deterministic (generator version below). It is partitioned across several
real Git worktrees of one logical repository, each registered through
``engineering.repository.register``, sealed by the production working-tree capture and
committed through ``engineering.source.capture.commit``. Conflict groups are produced by
the production discovery executor. Measured behaviour runs through the production
application surface; latencies are recorded, never asserted. Correctness gates
(authorization partition, applicability, conflict visibility, budgets) are asserted on
every sample, in every lane.

What the lanes do and do not control is stated in the report's ``cache`` block. In
particular the cold lane is *connection-cold* (a fresh SQLite connection, page cache and
dispatcher per sample); the operating-system page cache is not controlled, so no lane
here is ever called system-cold.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import test_blobs_staged_sources_and_evidence_migration as m2
import test_engineering_source_coverage as sc
from omnivia_core_runtime.ownership.identity import SystemClock
from omnivia_core_runtime.service import source_capture
from omnivia_core_runtime.service.application import engineering_family_session
from omnivia_core_runtime.service.engineering_conflict_execution import (
    DEFAULT_EXECUTION_BUDGET,
    EngineeringConflictExecutor,
)
from omnivia_core_runtime.service.engineering_pack import (
    BUILDER_VERSION,
    CONFLICT_BUILDER_VERSION,
    CONFLICT_RENDERER_VERSION,
    RENDERER_VERSION,
)
from omnivia_core_runtime.service.handlers import engineering as engineering_handlers
from omnivia_core_runtime.service.mutation import DEFAULT_GRANT_LIFETIME_US
from omnivia_core_runtime.service.source_capture import (
    capture_working_tree_snapshot_owned,
)
from omnivia_core_runtime.service.versions import SERVER_VERSION
from omnivia_core_runtime.storage import engineering_conflicts, engineering_preview
from omnivia_core_runtime.storage.continuity import (
    CHECKPOINT_PAYLOAD_CAP_BYTES,
    canonical_document,
)
from omnivia_core_runtime.storage.migrations import applied_migrations, load_migrations
from omnivia_core_runtime.workspace.layout import WorkspaceLayout

from omnivia_core.contracts.v1 import (
    CONTRACT_VERSION,
    MutationPrecondition,
    SuccessResponseEnvelope,
)

WORKSPACE_ID = sc.WORKSPACE_ID
REPOSITORY = sc.REPOSITORY

REPORT_FORMAT = "engineering-memory-qualification/2"
GENERATOR = "engineering-memory-qualification-corpus"
GENERATOR_VERSION = "2"

#: Twenty topic stems, crossed with eight component buckets below: each
#: (stem, component) pair names roughly a 160th of the corpus, so a query
#: naming one pair ranks a bounded candidate set at any qualification scale --
#: at 100 000 observations, at most 625 per query, well inside
#: `CURRENT_SAFE_CANDIDATE_CAP` -- rather than a whole stem's worth (a
#: twentieth, which at 100 000 would be 5 000 and would itself exceed the cap).
STEMS = [
    "authentication",
    "session-restoration",
    "credential-retry",
    "token-refresh",
    "database-pooling",
    "migration-locking",
    "index-fragmentation",
    "queue-backpressure",
    "cache-invalidation",
    "schema-evolution",
    "api-pagination",
    "rate-limiting",
    "websocket-reconnect",
    "background-jobs",
    "config-loading",
    "feature-flags",
    "error-taxonomy",
    "logging-redaction",
    "dependency-upgrades",
    "test-flakiness",
]

#: Eight deterministic component buckets, crossed with the twenty stems above.
COMPONENTS = [f"component-{index:02d}" for index in range(8)]

_PAIRS = len(STEMS) * len(COMPONENTS)

#: Every tenth non-conflict record is evidence-backed by an *open* artifact; the rest
#: carry the owner-held `group.engineering` label.
_OPEN_EVERY = 10
#: Every fourth non-conflict record carries long code spans in its summary and `what`.
_LONG_SPAN_EVERY = 4
_LONG_SPAN_CHARS = 1900

#: The payload-size classes the checkpoint lane measures. `None` is the natural size
#: of the short payload; the others are canonical-byte targets.
CHECKPOINT_CLASSES: dict[str, int | None] = {
    "short": None,
    "medium": 16 * 1024,
    "near_limit": CHECKPOINT_PAYLOAD_CAP_BYTES - 8 * 1024,
}

READ_OPERATIONS = (
    "engineering.search",
    "engineering.search.current_safe",
    "engineering.search.acl_partitioned_reader",
    "engineering.context.build",
    "engineering.context.build.current_safe",
    "engineering.context.build.conflict_groups",
)
CHECKPOINT_OPERATIONS = tuple(
    f"continuity.checkpoint.append.{name}" for name in CHECKPOINT_CLASSES
)

#: §20.2's reference warm targets on 4 cores / 16 GiB / local SSD / 100k observations.
REFERENCE_PROFILE = {
    "cpu_logical_cores": 4,
    "memory_gib": 16,
    "storage_class": "local-ssd",
    "observations": 100_000,
}
REFERENCE_TARGETS: dict[str, dict[str, Any]] = {
    "preview_search": {
        "p95_ms": 300,
        "operations": [
            "engineering.search",
            "engineering.search.current_safe",
            "engineering.search.acl_partitioned_reader",
        ],
    },
    "context_build_4k_tokens_16kib": {
        "p95_ms": 1000,
        "operations": [
            "engineering.context.build",
            "engineering.context.build.current_safe",
            "engineering.context.build.conflict_groups",
        ],
    },
    "checkpoint_commit": {"p95_ms": 200, "operations": list(CHECKPOINT_OPERATIONS)},
}


# --- configuration ---------------------------------------------------------------


@dataclass(frozen=True)
class LaneConfig:
    corpus: int = 10_000
    worktrees: int = 3
    conflict_groups: int = 0  # 0: derived from the corpus size
    search_samples: int = 100
    pack_samples: int = 30
    checkpoint_samples: int = 100  # per payload size class
    cold_samples: int = 10  # per operation, each after a connection restart
    warmup: int = 5  # discarded requests per operation before the warm samples
    readers: int = 4  # concurrent reader threads
    reader_requests: int = 25  # per reader thread
    writer_checkpoints: int = 25  # one concurrent checkpoint writer
    storage_class: str | None = None

    def __post_init__(self) -> None:
        if self.conflict_groups == 0:
            object.__setattr__(
                self, "conflict_groups", max(2, min(self.corpus // 1000, 200))
            )
        if self.worktrees < 2:
            raise ValueError("the corpus needs at least two worktrees")
        if self.conflict_groups < 1:
            raise ValueError("the corpus needs at least one conflict group")
        # Every (stem, component) bucket must keep a non-conflict member and the
        # open partition must reach every tenth bucket.
        if self.corpus < 2 * self.conflict_groups + _PAIRS:
            raise ValueError(
                f"corpus {self.corpus} is below {2 * self.conflict_groups + _PAIRS}, "
                "the least that populates every bucket"
            )
        for name in (
            "search_samples",
            "pack_samples",
            "checkpoint_samples",
            "cold_samples",
            "readers",
            "reader_requests",
            "writer_checkpoints",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")

    @property
    def conflict_members(self) -> int:
        return 2 * self.conflict_groups

    @classmethod
    def from_environment(cls) -> LaneConfig:
        def integer(name: str, default: int) -> int:
            return int(os.environ.get(f"OMNIVIA_QUALIFICATION_{name}", default))

        defaults = cls()
        return cls(
            corpus=integer("CORPUS", defaults.corpus),
            worktrees=integer("WORKTREES", defaults.worktrees),
            conflict_groups=integer("CONFLICT_GROUPS", 0),
            search_samples=integer("SEARCH_SAMPLES", defaults.search_samples),
            pack_samples=integer("PACK_SAMPLES", defaults.pack_samples),
            checkpoint_samples=integer("CHECKPOINT_SAMPLES", defaults.checkpoint_samples),
            cold_samples=integer("COLD_SAMPLES", defaults.cold_samples),
            readers=integer("READERS", defaults.readers),
            reader_requests=integer("READER_REQUESTS", defaults.reader_requests),
            writer_checkpoints=integer("WRITER_CHECKPOINTS", defaults.writer_checkpoints),
            storage_class=os.environ.get("OMNIVIA_QUALIFICATION_STORAGE_CLASS") or None,
        )


# --- the deterministic corpus ----------------------------------------------------


def _pair(index: int) -> tuple[str, str]:
    """The (stem, component) bucket an index falls in, one of 160 evenly."""
    slot = index % _PAIRS
    return STEMS[slot % len(STEMS)], COMPONENTS[slot // len(STEMS)]


def _query(run: int) -> str:
    """The query naming exactly one (stem, component) pair."""
    stem, component = _pair(run)
    return f"{stem} {component}"


def _worktree_ids(worktree: int) -> tuple[str, str]:
    """The (snapshot id, stream id) the corpus names for one worktree."""
    return f"esnap-qual-wt{worktree}", f"estream-qual-wt{worktree}"


def _code_span(index: int, salt: str, chars: int) -> str:
    """Deterministic code-like text that shares no token with any query."""
    lines: list[str] = []
    size = 0
    while size < chars:
        digest = hashlib.sha256(f"{index}:{salt}:{len(lines)}".encode()).hexdigest()
        line = (
            f"    v{digest[:6]} = buf[0x{digest[6:10]}] ^ k{digest[10:14]}"
            f"  # L{len(lines) + 1}"
        )
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines)[:chars]


@dataclass(frozen=True)
class RecordPlan:
    index: int
    key: str
    kind: str  # conflict_member | open | labeled
    worktree: int
    group: int | None
    long_span: bool
    source_id: str | None
    payload: dict[str, Any]


def _plan(index: int, config: LaneConfig) -> RecordPlan:
    """One corpus record. A pure function of (index, config): no database, no clock.

    Indexes below `conflict_members` are the conflict groups (pairs 2g, 2g+1), seeded
    first so their discovery runs are the oldest queued; the pair shares a worktree,
    because a cross-checkout pair is a proven `scoped_difference`, not a conflict.
    Every record's worktree is `(index // 2) % worktrees`.
    """
    worktree = (index // 2) % config.worktrees
    snapshot_id, stream_id = _worktree_ids(worktree)
    key = f"qual-seed-{index}"
    if index < config.conflict_members:
        group, role = index // 2, "ab"[index % 2]
        marker = f"qcg{group:05d}"
        payload = sc._observation(None, title=f"{marker}{role}", evidence=False)
        payload["content"].update(
            {
                "summary": marker,
                "what": f"{marker}{role}",
                "topic_ref": {"proposed_key": marker},
                "applicability": {"repository_id": REPOSITORY, "snapshot_id": snapshot_id},
            }
        )
        return RecordPlan(index, key, "conflict_member", worktree, group, False, None, payload)
    stem, component = _pair(index)
    is_open = index % _OPEN_EVERY == 0
    source_id = f"doc-open-{index}" if is_open else None
    # An explicit source: labeled records carry the default evidence source, open
    # records the ACL partition's own artifact.
    source = {**sc.EVIDENCE_SOURCE, "source_id": source_id} if is_open else sc.EVIDENCE_SOURCE
    payload = sc._observation(
        sc._manifest(snapshot_id, stream=stream_id),
        title=f"{stem} {component} finding {index}",
        source=source,
    )
    payload["content"]["applicability"] = {
        "repository_id": REPOSITORY,
        "snapshot_id": snapshot_id,
    }
    long_span = index % _LONG_SPAN_EVERY == 1
    if long_span:
        payload["content"]["summary"] = _code_span(index, "summary", _LONG_SPAN_CHARS)
        payload["content"]["what"] = _code_span(index, "what", _LONG_SPAN_CHARS)
    return RecordPlan(
        index, key, "open" if is_open else "labeled", worktree, None, long_span, source_id, payload
    )


def _bucket_worktrees(config: LaneConfig) -> list[list[int]]:
    """For each (stem, component) bucket, the worktrees holding one of its records.

    A record is `matched` only at a target inside its own worktree's source stream, so
    a `current_safe` request must target a worktree that holds a member of the bucket
    it queries. Pure arithmetic over the plan's own placement rule.
    """
    found: list[set[int]] = [set() for _ in range(_PAIRS)]
    for index in range(config.conflict_members, config.corpus):
        found[index % _PAIRS].add((index // 2) % config.worktrees)
    return [sorted(worktrees) for worktrees in found]


def _plan_bytes(plan: RecordPlan) -> bytes:
    """The canonical identity of one record, the unit the corpus digest folds."""
    return json.dumps(
        [plan.index, plan.key, plan.kind, plan.worktree, plan.source_id, plan.payload],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def corpus_identity(config: LaneConfig) -> dict[str, Any]:
    """The reproducible description of a corpus: no database is touched."""
    digest = hashlib.sha256()
    counts: dict[str, int] = defaultdict(int)
    per_worktree: dict[int, int] = defaultdict(int)
    long_spans = 0
    for index in range(config.corpus):
        plan = _plan(index, config)
        digest.update(_plan_bytes(plan))
        counts[plan.kind] += 1
        per_worktree[plan.worktree] += 1
        long_spans += plan.long_span
    return {
        "digest": digest.hexdigest(),
        "kinds": dict(counts),
        "per_worktree": dict(per_worktree),
        "long_span_records": long_spans,
    }


# --- statistics -------------------------------------------------------------------


def _percentiles(samples: list[float]) -> dict[str, Any]:
    ordered = sorted(samples)

    def pct(fraction: float) -> float:
        if not ordered:
            return 0.0
        rank = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
        return ordered[rank]

    return {
        "n": len(ordered),
        "min_ms": round(ordered[0], 3) if ordered else 0.0,
        "p50_ms": round(pct(0.50), 3),
        "p95_ms": round(pct(0.95), 3),
        "p99_ms": round(pct(0.99), 3),
        "max_ms": round(ordered[-1], 3) if ordered else 0.0,
        "mean_ms": round(statistics.fmean(ordered), 3) if ordered else 0.0,
    }


# --- checkpoint payloads -----------------------------------------------------------


def _checkpoint_payload(size_class: str, run: int) -> dict[str, Any]:
    """A payload of the named size class, measured by the production canonicalizer.

    Items are the contract's maximum 2 000-character strings of hash-derived text, so
    JSON escaping never inflates them; each payload is unique to its `run`.
    """
    target = CHECKPOINT_CLASSES[size_class]
    payload: dict[str, Any] = {
        "objective": f"{STEMS[run % len(STEMS)]} investigation step {run}",
        "checkpoint_kind": "periodic",
        "unresolved_work": [f"open question {run}"],
    }
    if target is None:
        return payload
    items: list[str] = payload["unresolved_work"]
    while True:
        size = len(canonical_document(payload).encode())
        gap = target - size
        if gap <= 0:
            return payload
        seed = hashlib.sha256(f"{run}:{len(items)}".encode()).hexdigest()
        # Two JSON bytes of quoting and one of separator per item.
        length = min(2000, gap - 3)
        if length < 1:
            return payload
        items.append((seed * (length // len(seed) + 1))[:length])


def _checkpoint_bytes(payload: dict[str, Any]) -> int:
    return len(canonical_document(payload).encode())


# --- environment, source and resource evidence ------------------------------------


def _run_text(argv: list[str], *, cwd: Path | None = None) -> str | None:
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=30, check=False, cwd=cwd
        )
        if done.returncode == 0:
            return done.stdout.strip() or None
    return None


def _cpu_record() -> dict[str, Any]:
    model: str | None = None
    physical: int | None = None
    if sys.platform == "darwin":
        model = _run_text(["sysctl", "-n", "machdep.cpu.brand_string"])
        count = _run_text(["sysctl", "-n", "hw.physicalcpu"])
        physical = int(count) if count and count.isdigit() else None
    elif sys.platform.startswith("linux"):
        with contextlib.suppress(OSError):
            cores: set[tuple[str, str]] = set()
            physical_id = ""
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                key, _, value = line.partition(":")
                key, value = key.strip(), value.strip()
                if key == "model name" and model is None:
                    model = value
                elif key == "physical id":
                    physical_id = value
                elif key == "core id":
                    cores.add((physical_id, value))
            physical = len(cores) or None
    elif sys.platform == "win32":
        model = os.environ.get("PROCESSOR_IDENTIFIER")
    model = model or platform.processor() or None
    return {
        "model": model,
        "logical_count": os.cpu_count(),
        "physical_count": physical,
        "source": "sysctl" if sys.platform == "darwin" else "platform/proc",
    }


def _memory_record() -> dict[str, Any]:
    total: int | None = None
    source = "unavailable"
    with contextlib.suppress(AttributeError, OSError, ValueError):
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        source = "os.sysconf"
    if total is None and sys.platform == "win32":  # pragma: no cover - Windows only
        with contextlib.suppress(Exception):
            import ctypes

            class _Status(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("load", ctypes.c_ulong),
                    ("total_phys", ctypes.c_ulonglong),
                    ("avail_phys", ctypes.c_ulonglong),
                    ("total_page", ctypes.c_ulonglong),
                    ("avail_page", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong),
                    ("avail_virtual", ctypes.c_ulonglong),
                    ("avail_extended", ctypes.c_ulonglong),
                ]

            status = _Status()
            status.length = ctypes.sizeof(_Status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
                total, source = int(status.total_phys), "GlobalMemoryStatusEx"
    return {"physical_bytes": total, "source": source}


def _os_record() -> dict[str, Any]:
    name = platform.system()
    version: str | None = platform.release()
    build: str | None = platform.version()
    if sys.platform == "darwin":
        version = platform.mac_ver()[0] or version
        build = _run_text(["sw_vers", "-buildVersion"]) or build
    elif sys.platform.startswith("linux"):
        with contextlib.suppress(OSError):
            release = platform.freedesktop_os_release()
            name = release.get("NAME", name)
            version = release.get("VERSION_ID", version)
            build = release.get("BUILD_ID") or f"kernel {platform.release()} {platform.version()}"
    elif sys.platform == "win32":  # pragma: no cover - Windows only
        version, build = platform.win32_ver()[0], platform.win32_ver()[1]
    return {
        "name": name,
        "version": version,
        "build": build,
        "architecture": platform.machine(),
        "platform": platform.platform(),
    }


def _package_version(distribution: str) -> str | None:
    with contextlib.suppress(metadata.PackageNotFoundError):
        return metadata.version(distribution)
    return None


def environment_record(config: LaneConfig) -> dict[str, Any]:
    storage = config.storage_class
    return {
        "cpu": _cpu_record(),
        "memory": _memory_record(),
        "storage": {
            "class": storage or "undeclared",
            "source": "operator declaration" if storage else "not detected",
            "database_location": "pytest tmp_path of the run",
        },
        "os": _os_record(),
        "runtimes": {
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "sqlite": sqlite3.sqlite_version,
            "core_runtime": SERVER_VERSION,
            "core_runtime_distribution": _package_version("omnivia-core-runtime"),
            "contract": CONTRACT_VERSION,
        },
    }


def _git(*args: str, binary: bool = False) -> Any:
    root = Path(__file__).resolve().parent
    if binary:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            done = subprocess.run(
                ["git", *args], capture_output=True, timeout=60, check=False, cwd=root
            )
            return done.stdout if done.returncode == 0 else None
        return None
    return _run_text(["git", *args], cwd=root)


def source_record() -> dict[str, Any]:
    """The exact source commit and dirty state; unavailable fields say why."""
    commit = _git("rev-parse", "HEAD")
    if commit is None:
        return {"commit": None, "unavailable": "git is unavailable or this is not a checkout"}
    porcelain = _git("status", "--porcelain=v1", "--untracked-files=all", "-z", binary=True)
    dirty = bool(porcelain)
    record: dict[str, Any] = {
        "commit": commit,
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": dirty,
        "dirty_path_count": 0,
        "dirty_digest": None,
    }
    if dirty and porcelain is not None:
        # The digest covers the status list, the tracked diff and every untracked
        # file's bytes, so a dirty run is still reproducible from commit + patch.
        digest = hashlib.sha256(porcelain)
        digest.update(_git("diff", "HEAD", "--binary", binary=True) or b"")
        untracked = _git("ls-files", "--others", "--exclude-standard", "-z", binary=True) or b""
        top = _git("rev-parse", "--show-toplevel")
        for name in sorted(filter(None, untracked.decode(errors="replace").split("\0"))):
            digest.update(name.encode())
            with contextlib.suppress(OSError):
                digest.update((Path(top or ".") / name).read_bytes())
        record["dirty_path_count"] = len(list(filter(None, porcelain.split(b"\0"))))
        record["dirty_digest"] = digest.hexdigest()
    return record


def _peak_rss_bytes() -> int | None:
    with contextlib.suppress(ImportError, OSError, ValueError):
        import resource  # POSIX only

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak if sys.platform == "darwin" else peak * 1024)
    return None


def _current_rss_bytes() -> int | None:
    if sys.platform.startswith("linux"):
        with contextlib.suppress(OSError, ValueError, IndexError):
            for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    return None


def _file_bytes(path: Path) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(OSError):
            total += (path.parent / (path.name + suffix)).stat().st_size
    return total


class _Resources:
    """Process-level observations at lane boundaries, from the standard library only.

    The peak is the *process* high-water mark at sample time (it includes the whole
    pytest process and the seeded run so far), never a per-lane peak; CPU seconds are
    this process's own. Fields the platform cannot supply are `None`.
    """

    def __init__(self, database: Path) -> None:
        self.database = database
        self.started = time.perf_counter()
        self.cpu_started = time.process_time()
        self.points: list[dict[str, Any]] = []

    def mark(self, label: str) -> dict[str, Any]:
        point = {
            "at": label,
            "wall_seconds": round(time.perf_counter() - self.started, 3),
            "cpu_seconds": round(time.process_time() - self.cpu_started, 3),
            "peak_rss_bytes": _peak_rss_bytes(),
            "current_rss_bytes": _current_rss_bytes(),
            "database_bytes": _file_bytes(self.database),
        }
        self.points.append(point)
        return point

    def summary(self) -> dict[str, Any]:
        peaks = [p["peak_rss_bytes"] for p in self.points if p["peak_rss_bytes"] is not None]
        return {
            "peak_rss_bytes": max(peaks) if peaks else None,
            "peak_rss_scope": "process high-water mark (ru_maxrss), whole pytest process",
            "peak_rss_unavailable": None if peaks else "resource module unavailable",
            "current_rss_unavailable": None
            if any(p["current_rss_bytes"] is not None for p in self.points)
            else "current RSS needs /proc (Linux); no portable standard-library source",
            "working_set": "not captured: no portable standard-library source",
            "points": self.points,
        }


def _lane_resources(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    wall = after["wall_seconds"] - before["wall_seconds"]
    cpu = after["cpu_seconds"] - before["cpu_seconds"]
    return {
        "wall_seconds": round(wall, 3),
        "cpu_seconds": round(cpu, 3),
        "cpu_utilisation": round(cpu / wall, 3) if wall > 0 else None,
        "peak_rss_bytes_at_end": after["peak_rss_bytes"],
        "database_bytes_at_end": after["database_bytes"],
    }


def policy_record(config: LaneConfig) -> dict[str, Any]:
    """The policy/config snapshot in force, read from the production modules."""
    snapshot = {
        "current_safe_candidate_cap": engineering_handlers.CURRENT_SAFE_CANDIDATE_CAP,
        "context_budget": {
            "tokens": engineering_handlers.BUDGET_DEFAULT_TOKENS,
            "bytes": engineering_handlers.BUDGET_DEFAULT_BYTES,
            "hydrations": engineering_handlers.BUDGET_DEFAULT_HYDRATIONS,
            "ceiling_tokens": engineering_handlers.BUDGET_CEILING_TOKENS,
            "ceiling_bytes": engineering_handlers.BUDGET_CEILING_BYTES,
            "ceiling_hydrations": engineering_handlers.BUDGET_CEILING_HYDRATIONS,
        },
        "pack_versions": {
            "renderer": RENDERER_VERSION,
            "builder": BUILDER_VERSION,
            "conflict_renderer": CONFLICT_RENDERER_VERSION,
            "conflict_builder": CONFLICT_BUILDER_VERSION,
        },
        "preview": {
            "projection_version": engineering_preview.PROJECTION_VERSION,
            "preview_max_codepoints": engineering_preview.PREVIEW_MAX_CODEPOINTS,
            "preview_max_bytes": engineering_preview.PREVIEW_MAX_BYTES,
        },
        "discovery": {
            "candidate_budget": engineering_conflicts.DEFAULT_CANDIDATE_BUDGET,
            "scan_record_budget": engineering_conflicts.DEFAULT_SCAN_RECORD_BUDGET,
            "execution_budget": DEFAULT_EXECUTION_BUDGET,
            "semantic_assessment": "no provider configured",
        },
        "checkpoint_payload_cap_bytes": CHECKPOINT_PAYLOAD_CAP_BYTES,
        "authorization": {
            "owner": "local owner session: holds every evidence label",
            "reader": "engineering-family session without the owner-held `group.engineering` label",
            "label_partition": f"every {_OPEN_EVERY}th non-conflict record is open",
        },
        "workload": {
            "worktrees": config.worktrees,
            "conflict_groups": config.conflict_groups,
            "checkpoint_classes": {
                name: target for name, target in CHECKPOINT_CLASSES.items()
            },
        },
    }
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return {"digest": hashlib.sha256(canonical.encode()).hexdigest(), "snapshot": snapshot}


def _database_config(connection: sqlite3.Connection) -> dict[str, Any]:
    names = (
        "journal_mode",
        "synchronous",
        "page_size",
        "cache_size",
        "mmap_size",
        "locking_mode",
        "wal_autocheckpoint",
        "foreign_keys",
    )
    return {name: connection.execute(f"PRAGMA {name}").fetchone()[0] for name in names}


# --- production-path fixture states ------------------------------------------------

_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "qualification",
    "GIT_AUTHOR_EMAIL": "qualification@example.invalid",
    "GIT_COMMITTER_NAME": "qualification",
    "GIT_COMMITTER_EMAIL": "qualification@example.invalid",
    # Fixed dates make the commit, and so the captured manifest, reproducible.
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


def _git_fixture(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=root, env=_GIT_ENV, check=True, capture_output=True
    )


def _provision_worktrees(
    ws: sc.Workspace, gate: threading.RLock, tmp_path: Path, config: LaneConfig
) -> list[dict[str, Any]]:
    """Real Git worktrees of one repository, registered, sealed and committed.

    Registration is `engineering.repository.register`, the seal is the production
    `capture_working_tree_snapshot_owned` (driven with the workspace's own connection,
    identity and gate, as the installed service drives it), and coverage is
    `engineering.source.capture.commit`. Each worktree gets one untracked marker file,
    so its sealed manifest and snapshot identity differ while the attested dependency
    files are identical.
    """
    base = (tmp_path / "checkouts").resolve()
    base.mkdir()
    roots = [base / f"wt-{n}" for n in range(config.worktrees)]
    roots[0].mkdir()
    (roots[0] / "src").mkdir()
    for path, text in (
        ("src/auth.py", "auth v1"),
        ("src/util.py", "util v1"),
        ("README.md", "readme v1"),
    ):
        (roots[0] / path).write_bytes(text.encode())
    _git_fixture(roots[0], "init", "-q")
    _git_fixture(roots[0], "add", ".")
    _git_fixture(roots[0], "-c", "commit.gpgsign=false", "commit", "-q", "-m", "qualification")
    for root in roots[1:]:
        _git_fixture(roots[0], "worktree", "add", "-q", "--detach", os.fspath(root))
    layout = WorkspaceLayout(root=tmp_path / "blob-root")
    # The blob store `initialise_workspace` creates for an installed workspace.
    (layout.blobs_path / "sha256").mkdir(mode=0o700, parents=True)
    runner = SimpleNamespace(
        connection=ws.holder.connection,
        identity=ws.holder.identity,
        workspace_id=WORKSPACE_ID,
        generation=ws.holder.generation,
        sqlite_gate=gate,
        layout=layout,
    )
    provisioned: list[dict[str, Any]] = []
    for worktree, root in enumerate(roots):
        snapshot_id, stream_id = _worktree_ids(worktree)
        (root / "WORKTREE").write_bytes(f"worktree {worktree}\n".encode())
        registered = ws.ok(
            "engineering.repository.register",
            {
                "repository_id": REPOSITORY,
                "display_name": "qualification",
                "checkout_root": os.fspath(root),
            },
        )
        sealed = capture_working_tree_snapshot_owned(
            cast(Any, runner),
            repository_id=REPOSITORY,
            checkout_root=root,
            snapshot_id=snapshot_id,
        )
        committed = ws.ok(
            "engineering.source.capture.commit",
            {
                "repository_id": REPOSITORY,
                "stream_id": stream_id,
                "sequence": 1,
                "snapshot_id": snapshot_id,
                "expected_manifest_digest": sealed.manifest_digest,
            },
        )
        provisioned.append(
            {
                "worktree": worktree,
                "checkout_id": registered["checkout_id"],
                "snapshot_id": snapshot_id,
                "snapshot_kind": "working_tree",
                "stream_id": stream_id,
                "sequence": 1,
                "capture_status": sealed.capture_status,
                "file_count": sealed.file_count,
                "manifest_digest": sealed.manifest_digest,
                "coverage": committed["coverage"],
                "head_commit": _run_text(["git", "rev-parse", "HEAD"], cwd=root),
            }
        )
    return provisioned


# --- measured operations -------------------------------------------------------------


class _Probe:
    """Issues one request through the production surface, behind the SQLite gate.

    The production socket and HTTP transports hold the workspace's `sqlite_gate` around
    every dispatch; the harness does the same, so concurrent requests queue exactly as
    production requests do. `wait` is the time queued on the gate, `service` the
    dispatch itself; the unthreaded lanes report `service`.
    """

    def __init__(self, ws: sc.Workspace, gate: threading.RLock) -> None:
        self.ws = ws
        self.gate = gate

    def call(
        self, operation: str, payload: dict[str, Any], **kwargs: Any
    ) -> tuple[dict[str, Any], float, float]:
        queued = time.perf_counter()
        with self.gate:
            started = time.perf_counter()
            result = self.ws.ok(operation, payload, **kwargs)
            finished = time.perf_counter()
        return result, (started - queued) * 1000.0, (finished - started) * 1000.0


@dataclass
class _Corpus:
    config: LaneConfig
    worktrees: list[dict[str, Any]]
    open_ids: set[str]
    groups: list[tuple[str, str]]  # (record ids of the two members), by group
    reader: Any
    bucket_worktrees: list[list[int]]


class _Operations:
    """The measured read operations, each with its correctness gate."""

    def __init__(self, probe: _Probe, corpus: _Corpus) -> None:
        self.probe = probe
        self.corpus = corpus
        self.effective_budget: dict[str, Any] = {}

    def _target(self, run: int) -> dict[str, Any]:
        """A worktree that holds a member of run's queried bucket, rotating through them."""
        options = self.corpus.bucket_worktrees[run % _PAIRS]
        return self.corpus.worktrees[options[(run // _PAIRS) % len(options)]]

    def run(self, name: str, run: int) -> tuple[float, float]:
        corpus = self.corpus
        if name == "engineering.search":
            result, wait, service = self.probe.call(
                "engineering.search", {"query": _query(run), "view": "candidates"}
            )
            assert result["previews"], "a seeded query must match its corpus"
        elif name == "engineering.search.current_safe":
            target = self._target(run)
            result, wait, service = self.probe.call(
                "engineering.search",
                {
                    "query": _query(run),
                    "view": "candidates",
                    "applicability_mode": "current_safe",
                    "repository_target": {
                        "repository_id": REPOSITORY,
                        "snapshot_id": target["snapshot_id"],
                    },
                },
            )
            assert result["previews"], "a seeded query must match its corpus under current_safe"
            assert {p["applicability"] for p in result["previews"]} == {"matched"}
        elif name == "engineering.search.acl_partitioned_reader":
            # The open partition is every tenth index, so only a bucket whose slot is
            # a multiple of ten (160 slots, ten divides it) has an open member to find.
            result, wait, service = self.probe.call(
                "engineering.search",
                {"query": _query(run * _OPEN_EVERY), "view": "candidates"},
                session=corpus.reader,
            )
            assert result["previews"], "the open partition must match its corpus"
            assert {p["record_id"] for p in result["previews"]} <= corpus.open_ids, (
                "a restricted reader was served a label-denied record"
            )
        elif name == "engineering.context.build":
            result, wait, service = self.probe.call(
                "engineering.context.build", self._pack_input(run, safe=False)
            )
            self._check_pack(result["pack"])
        elif name == "engineering.context.build.current_safe":
            result, wait, service = self.probe.call(
                "engineering.context.build", self._pack_input(run, safe=True)
            )
            self._check_pack(result["pack"])
        elif name == "engineering.context.build.conflict_groups":
            group = run % len(corpus.groups)
            result, wait, service = self.probe.call(
                "engineering.context.build",
                {"query": f"qcg{group:05d}", "targets": [], "profile": "investigate"},
            )
            self._check_pack(result["pack"])
            conflicts = result["pack"]["conflicts"]
            assert len(conflicts) == 1, "a conflict group must be visible to context build"
            assert {r["record_id"] for r in conflicts[0]["records"]} == set(
                corpus.groups[group]
            )
        else:  # pragma: no cover - a harness typo
            raise AssertionError(f"unknown operation {name}")
        return wait, service

    def _pack_input(self, run: int, *, safe: bool) -> dict[str, Any]:
        # The pack's authorized-candidate budget is caller-requestable up to the
        # server ceiling (10 000): the qualification corpus must be admitted in full
        # or the bounded frontier read refuses the build. The token and byte budgets
        # stay at the server's 4 000-token / 16 KiB default.
        target = self._target(run)
        pack_input: dict[str, Any] = {
            "query": _query(run),
            "targets": [
                {
                    "repository_id": REPOSITORY,
                    "snapshot_id": target["snapshot_id"],
                    "snapshot_kind": target["snapshot_kind"],
                }
            ],
            "profile": "investigate",
            "budget": {"authorized_candidates": min(self.corpus.config.corpus, 10_000)},
        }
        if safe:
            pack_input["applicability_mode"] = "current_safe"
        return pack_input

    def _check_pack(self, pack: dict[str, Any]) -> None:
        assert pack["format_version"] == "engineering_context.v1"
        assert pack["fresh_authorization_required"] is True
        budget = pack["budget"]
        effective = budget["effective"]
        # The budget gates hold on every sample: a pack never exceeds what it was
        # granted, so a fast answer cannot be one that skipped the budget.
        assert budget["rendered_tokens"] <= effective["model_tokens"]
        assert budget["rendered_bytes"] <= effective["model_bytes"]
        assert budget["hydrations"] <= effective["hydrations"]
        assert budget["source_bytes_read"] <= effective["evidence_bytes"]
        self.effective_budget = dict(effective)


class _Checkpointer:
    """One continuity session appended to in payload-size classes."""

    def __init__(self, probe: _Probe) -> None:
        self.probe = probe
        self.session_id = ""
        self.appended = 0
        self.sizes: dict[str, list[int]] = defaultdict(list)

    def begin(self) -> None:
        registered = self.probe.ws.ok(
            "continuity.session.register",
            {
                "schema_version": "engineering.1",
                "checkout_hint": "/home/dev/qual",
                "host_session_ref": f"qual-conv-{time.time_ns()}",
            },
        )
        self.session_id = registered["session"]["session_id"]
        self.appended = 0

    def append(self, size_class: str) -> tuple[float, float]:
        payload: dict[str, Any] = {
            "session_id": self.session_id,
            "payload": _checkpoint_payload(size_class, self.appended),
        }
        self.sizes[size_class].append(_checkpoint_bytes(payload["payload"]))
        if self.appended > 0:
            payload["expected_parent_sequence"] = self.appended
        result, wait, service = self.probe.call(
            "continuity.checkpoint.append",
            payload,
            mutation_precondition=MutationPrecondition(record_version=f"seq-{self.appended}"),
        )
        self.appended += 1
        assert result["receipt"]["sequence"] == self.appended
        return wait, service


def _checkpoint_summary(checkpointer: _Checkpointer, size_class: str, samples: list[float]) -> dict[str, Any]:
    sizes = checkpointer.sizes[size_class]
    summary = _percentiles(samples)
    summary["payload_bytes"] = {"min": min(sizes), "max": max(sizes)} if sizes else None
    return summary


# --- lanes ---------------------------------------------------------------------------


def _warm_lane(ops: _Operations, probe: _Probe, config: LaneConfig) -> dict[str, Any]:
    started = datetime.now(UTC)
    operations: dict[str, Any] = {}
    for name in READ_OPERATIONS:
        count = config.pack_samples if "context.build" in name else config.search_samples
        for run in range(config.warmup):
            ops.run(name, run)
        samples = [ops.run(name, config.warmup + run)[1] for run in range(count)]
        operations[name] = _percentiles(samples)
    checkpointer = _Checkpointer(probe)
    checkpointer.begin()
    for _ in range(config.warmup):
        checkpointer.append("short")
    checkpointer.sizes.clear()
    samples_by_class: dict[str, list[float]] = {name: [] for name in CHECKPOINT_CLASSES}
    for _ in range(config.checkpoint_samples):
        for size_class in CHECKPOINT_CLASSES:
            samples_by_class[size_class].append(checkpointer.append(size_class)[1])
    for size_class, samples in samples_by_class.items():
        operations[f"continuity.checkpoint.append.{size_class}"] = _checkpoint_summary(
            checkpointer, size_class, samples
        )
    return {
        "started_at": started.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "warmup_requests_per_operation": config.warmup,
        "operations": operations,
    }


def _cold_lane(ops: _Operations, probe: _Probe, config: LaneConfig) -> dict[str, Any]:
    """Each sample follows a restart: a fresh connection, page cache and dispatcher."""
    started = datetime.now(UTC)
    operations: dict[str, Any] = {}
    for name in READ_OPERATIONS:
        samples: list[float] = []
        for run in range(config.cold_samples):
            probe.ws.restart()
            samples.append(ops.run(name, run)[1])
        operations[name] = _percentiles(samples)
    checkpointer = _Checkpointer(probe)
    checkpointer.begin()
    samples_by_class: dict[str, list[float]] = {name: [] for name in CHECKPOINT_CLASSES}
    for _ in range(config.cold_samples):
        for size_class in CHECKPOINT_CLASSES:
            probe.ws.restart()
            samples_by_class[size_class].append(checkpointer.append(size_class)[1])
    for size_class, samples in samples_by_class.items():
        operations[f"continuity.checkpoint.append.{size_class}"] = _checkpoint_summary(
            checkpointer, size_class, samples
        )
    return {
        "started_at": started.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "samples_per_operation": config.cold_samples,
        "operations": operations,
    }


def _concurrent_lane(ops: _Operations, probe: _Probe, config: LaneConfig) -> dict[str, Any]:
    """Reader threads cycle every read operation while one writer appends checkpoints.

    Every request runs the same correctness gates as the unthreaded lanes. Requests
    queue on the one SQLite gate, so this measures end-to-end latency under a bounded
    client load, not parallel execution inside Core.
    """
    started = datetime.now(UTC)
    checkpointer = _Checkpointer(probe)
    checkpointer.begin()
    barrier = threading.Barrier(config.readers + 1)

    def reader(index: int) -> dict[str, list[tuple[float, float]]]:
        seen: dict[str, list[tuple[float, float]]] = defaultdict(list)
        barrier.wait()
        for request in range(config.reader_requests):
            name = READ_OPERATIONS[(index + request) % len(READ_OPERATIONS)]
            seen[name].append(ops.run(name, 1_000 * (index + 1) + request))
        return seen

    def writer() -> dict[str, list[tuple[float, float]]]:
        seen: dict[str, list[tuple[float, float]]] = defaultdict(list)
        classes = list(CHECKPOINT_CLASSES)
        barrier.wait()
        for request in range(config.writer_checkpoints):
            size_class = classes[request % len(classes)]
            seen[f"continuity.checkpoint.append.{size_class}"].append(
                checkpointer.append(size_class)
            )
        return seen

    wall_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=config.readers + 1) as pool:
        futures = [pool.submit(reader, index) for index in range(config.readers)]
        futures.append(pool.submit(writer))
        gathered = [future.result() for future in futures]  # re-raises any gate failure
    wall = time.perf_counter() - wall_started
    merged: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for seen in gathered:
        for name, pairs in seen.items():
            merged[name].extend(pairs)
    requests = sum(len(pairs) for pairs in merged.values())
    return {
        "started_at": started.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "declared_workload": {
            "reader_threads": config.readers,
            "requests_per_reader": config.reader_requests,
            "reader_operations": list(READ_OPERATIONS),
            "writer_threads": 1,
            "checkpoints_by_writer": config.writer_checkpoints,
            "serialization": (
                "one shared SQLite gate around every dispatch, as the production socket "
                "and HTTP transports hold it; concurrency is queued, not parallel"
            ),
        },
        "total_requests": requests,
        "wall_seconds": round(wall, 3),
        "throughput_requests_per_second": round(requests / wall, 2) if wall > 0 else None,
        "operations": {
            name: {
                "end_to_end": _percentiles([wait + service for wait, service in pairs]),
                "service": _percentiles([service for _wait, service in pairs]),
                "gate_wait": _percentiles([wait for wait, _service in pairs]),
            }
            for name, pairs in sorted(merged.items())
        },
    }


# --- seeding and conflict production ------------------------------------------------


def _discovery_counts(connection: sqlite3.Connection) -> dict[str, int]:
    runs = int(connection.execute("SELECT COUNT(*) FROM omnivia_engineering_discovery_runs").fetchone()[0])
    terminal = int(
        connection.execute(
            "SELECT COUNT(*) FROM omnivia_engineering_discovery_run_events "
            "WHERE event_sequence = 2"
        ).fetchone()[0]
    )
    return {"runs": runs, "terminal": terminal, "backlog": runs - terminal}


def _drain_conflict_runs(ws: sc.Workspace, member_ids: set[str]) -> dict[str, Any]:
    """Advance the production executor until the queue head is no longer a conflict run.

    Conflict members are seeded first, so their runs are the oldest queued; one page
    per pass keeps the drain from reaching past them into the rest of the backlog.
    """
    holder = ws.holder
    executor = EngineeringConflictExecutor(
        connection=holder.connection,
        identity=holder.identity,
        workspace_id=WORKSPACE_ID,
        fencing_generation=holder.generation,
        clock=SystemClock(),
    )
    started = time.perf_counter()
    passes = 0
    while True:
        head = engineering_conflicts.read_oldest_queued_run(
            holder.connection, workspace_id=WORKSPACE_ID
        )
        if head is None or head.anchor_record_id not in member_ids:
            break
        advanced = executor.run_pending(budget=1)
        assert advanced, "the conflict executor stalled on a member run"
        passes += 1
    return {"passes": passes, "seconds": round(time.perf_counter() - started, 1)}


def _seed(ws: sc.Workspace, config: LaneConfig, *, progress: bool) -> dict[str, Any]:
    digest = hashlib.sha256()
    open_ids: set[str] = set()
    member_ids: dict[int, list[str]] = defaultdict(list)
    started = time.perf_counter()
    for index in range(config.corpus):
        plan = _plan(index, config)
        if plan.kind == "open":
            # The open ACL partition: its evidence artifact carries no reader-held
            # label, so any engineering-family reader admits it. The rest carry the
            # owner-held `group.engineering` label, which the owner session holds and
            # a restricted reader does not.
            m2.write(
                ws.holder,
                m2.EVIDENCE,
                evidence_id=f"evd-open-{index}",
                source_native_id=str(plan.source_id),
            )
        observed = ws.observe(plan.payload, key=plan.key)
        digest.update(_plan_bytes(plan))
        if plan.kind != "labeled":
            # Conflict members carry no evidence, so every reader admits them.
            open_ids.add(observed["record_id"])
        if plan.group is not None:
            member_ids[plan.group].append(observed["record_id"])
        if progress and (index + 1) % 1000 == 0:
            print(f"seeded {index + 1}/{config.corpus}", flush=True)
    return {
        "seed_seconds": round(time.perf_counter() - started, 1),
        "digest": digest.hexdigest(),
        "open_ids": open_ids,
        "groups": [(ids[0], ids[1]) for _group, ids in sorted(member_ids.items())],
    }


# --- the qualification session ---------------------------------------------------------


class QualificationClock:
    """The deterministic request clock of the qualification session.

    Every mutation grant is issued per request, with the production 60 s window, and
    judged on the service's monotonic clock. On the real clock a request that straddles
    a stall longer than the window (an I/O-contention pause, a suspended process) is
    refused as outside its validity window, however short the request itself is. Here
    the monotonic reading advances a fixed step per reading and never with wall time,
    so a grant stays valid for `GRANT_WINDOW_READINGS` readings: far more than one
    request makes, and independent of how long the lane has run or been paused.
    Wall time stays real, since it is only recorded. Nothing is refreshed inside a
    measured request, and production keeps `SystemClock` and its default lifetime.
    """

    STEP_US = 1_000

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._readings = 0

    def monotonic(self) -> float:
        with self._lock:
            self._readings += 1
            return self._readings * self.STEP_US / 1_000_000

    def wall_time(self) -> datetime:
        return datetime.now(UTC)


#: What the format-2 report states about the session's grant validity. The validator
#: demands exactly this, so a run cannot silently change the policy it measured under.
SESSION_POLICY: dict[str, Any] = {
    "clock": "deterministic-reading-step",
    "monotonic_step_us": QualificationClock.STEP_US,
    "grant_lifetime_us": DEFAULT_GRANT_LIFETIME_US,
    "grant_window_readings": DEFAULT_GRANT_LIFETIME_US // QualificationClock.STEP_US,
    "grant_scope": "one request: a grant is issued and settled inside a single dispatch",
    "wall_clock_independent": True,
    "grants_refreshed_in_measured_requests": False,
    "production_default_lifetime_us": DEFAULT_GRANT_LIFETIME_US,
}


# --- the lane ---------------------------------------------------------------------


def run_lane(tmp_path: Path, config: LaneConfig, *, progress: bool = False) -> dict[str, Any]:
    """Seed one corpus, run the cold, warm and concurrent lanes, return the report."""
    run_started = datetime.now(UTC)
    ws = sc.Workspace(tmp_path, clock=QualificationClock())
    gate = threading.RLock()
    try:
        resources = _Resources(ws.holder.path)
        worktrees = _provision_worktrees(ws, gate, tmp_path, config)
        resources.mark("after_worktree_provisioning")

        seeded = _seed(ws, config, progress=progress)
        resources.mark("after_seed")
        identity = corpus_identity(config)
        assert identity["digest"] == seeded["digest"], "the corpus plan is not pure"
        member_ids = {record for pair in seeded["groups"] for record in pair}
        backlog_before = _discovery_counts(ws.holder.connection)
        drained = _drain_conflict_runs(ws, member_ids)
        discovery = _discovery_counts(ws.holder.connection)
        relations = ws.holder.connection.execute(
            "SELECT scope_classification, COUNT(*) FROM omnivia_engineering_relation_candidates "
            "GROUP BY scope_classification"
        ).fetchall()
        assert sum(count for _name, count in relations) == config.conflict_groups, (
            "the production executor must produce exactly one relation per conflict group"
        )
        resources.mark("after_conflict_discovery")
        if progress:
            print(f"seed {config.corpus} in {seeded['seed_seconds']}s; {drained}", flush=True)

        reader = engineering_family_session(
            principal_id=f"reader-{WORKSPACE_ID}",
            installation_id=sc.s0.INSTALLATION_ID,
            workspace_id=WORKSPACE_ID,
        )
        corpus = _Corpus(
            config,
            worktrees,
            seeded["open_ids"],
            seeded["groups"],
            reader,
            _bucket_worktrees(config),
        )
        probe = _Probe(ws, gate)
        ops = _Operations(probe, corpus)

        cold_before = resources.mark("before_cold_lane")
        cold = _cold_lane(ops, probe, config)
        cold_after = resources.mark("after_cold_lane")
        warm = _warm_lane(ops, probe, config)
        warm_after = resources.mark("after_warm_lane")
        concurrent = _concurrent_lane(ops, probe, config)
        concurrent_after = resources.mark("after_concurrent_lane")
        cold["resources"] = _lane_resources(cold_before, cold_after)
        warm["resources"] = _lane_resources(cold_after, warm_after)
        concurrent["resources"] = _lane_resources(warm_after, concurrent_after)

        connection = ws.holder.connection
        migrations = applied_migrations(connection)
        head = max(migrations)
        head_name = next(m.name for m in load_migrations() if m.version == head)
        report = _assemble_report(
            config=config,
            run_started=run_started,
            seeded=seeded,
            identity=identity,
            worktrees=worktrees,
            discovery={
                "before_drain": backlog_before,
                "after_drain": discovery,
                "drain": drained,
                "relation_candidates": {name: count for name, count in relations},
            },
            migration={"head": head, "head_name": head_name, "applied": len(migrations)},
            database=_database_config(connection),
            effective_budget=ops.effective_budget,
            lanes={"cold": cold, "warm": warm, "concurrent": concurrent},
            resources=resources.summary(),
        )
    finally:
        with contextlib.suppress(Exception):
            ws.holder.connection.close()
    validate_report(report)
    return report


def _assemble_report(
    *,
    config: LaneConfig,
    run_started: datetime,
    seeded: dict[str, Any],
    identity: dict[str, Any],
    worktrees: list[dict[str, Any]],
    discovery: dict[str, Any],
    migration: dict[str, Any],
    database: dict[str, Any],
    effective_budget: dict[str, Any],
    lanes: dict[str, Any],
    resources: dict[str, Any],
) -> dict[str, Any]:
    environment = environment_record(config)
    source = source_record()
    policy = policy_record(config)
    snapshots_digest = hashlib.sha256(
        json.dumps(
            [[w["snapshot_id"], w["stream_id"], w["manifest_digest"]] for w in worktrees],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    for item in worktrees:
        item["observations"] = identity["per_worktree"].get(item["worktree"], 0)
    warm = lanes["warm"]["operations"]
    targets = {
        name: {
            "target_p95_ms": spec["p95_ms"],
            "operations": spec["operations"],
            "observed_worst_p95_ms": max(warm[op]["p95_ms"] for op in spec["operations"]),
            "within_target": max(warm[op]["p95_ms"] for op in spec["operations"])
            <= spec["p95_ms"],
        }
        for name, spec in REFERENCE_TARGETS.items()
    }
    memory = environment["memory"]["physical_bytes"]
    cores = environment["cpu"]["logical_count"]
    blockers = []
    if config.corpus < REFERENCE_PROFILE["observations"]:
        blockers.append(f"corpus {config.corpus} is below the {REFERENCE_PROFILE['observations']} reference")
    if cores is None or cores < REFERENCE_PROFILE["cpu_logical_cores"]:
        blockers.append("logical cores are below the 4-core reference or unknown")
    if memory is None or memory < REFERENCE_PROFILE["memory_gib"] * 2**30:
        blockers.append("physical RAM is below the 16 GiB reference or unknown")
    if config.storage_class != REFERENCE_PROFILE["storage_class"]:
        blockers.append("storage class is not declared as local-ssd")
    if source.get("commit") is None:
        blockers.append("the source commit is unavailable")
    elif source["dirty"]:
        blockers.append("the working tree is dirty")
    blockers.append("system-cold lane not executed: the OS page cache is not controlled")
    return {
        "report_format": REPORT_FORMAT,
        "spec": "SPEC-CORE-ENGMEM-001 section 20.2",
        "lane": "engineering-memory-qualification",
        "run": {
            "started_at": run_started.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "seed_seconds": seeded["seed_seconds"],
        },
        "environment": environment,
        "session_policy": deepcopy(SESSION_POLICY),
        "source": source,
        "migration": migration,
        "database": database,
        "corpus": {
            "generator": GENERATOR,
            "generator_version": GENERATOR_VERSION,
            "observations": config.corpus,
            "digest": seeded["digest"],
            "kinds": identity["kinds"],
            "acl": {
                "open_partition_records": len(seeded["open_ids"]),
                "open_rule": f"every {_OPEN_EVERY}th non-conflict record, plus the unlabeled "
                "conflict members",
                "labeled_records": identity["kinds"].get("labeled", 0),
                "label": "group.engineering (held by the owner session, not by the reader)",
            },
            "code_spans": {
                "long_span_records": identity["long_span_records"],
                "chars_per_field": _LONG_SPAN_CHARS,
                "rule": f"every {_LONG_SPAN_EVERY}th non-conflict record, summary and what",
            },
            "source_snapshots": {
                "repository_id": REPOSITORY,
                "digest": snapshots_digest,
                "worktrees": worktrees,
                "method": (
                    "real Git worktrees of one repository; engineering.repository.register, "
                    "production working-tree capture seal, engineering.source.capture.commit"
                ),
            },
            "conflict_groups": {
                "count": config.conflict_groups,
                "members_per_group": 2,
                "members_share_a_worktree": True,
                "method": (
                    "pairs sharing a topic key, seeded first; produced by the production "
                    "EngineeringConflictExecutor draining their own discovery runs. No "
                    "semantic assessment provider ran, so each group is a structural "
                    "unresolved_overlap, never an assessed material conflict"
                ),
                "discovery": discovery,
            },
            "checkpoint_payloads": {
                name: {"target_canonical_bytes": target}
                for name, target in CHECKPOINT_CLASSES.items()
            },
        },
        "policy": policy,
        "effective_context_budget": effective_budget,
        "cache": {
            "os_page_cache": {
                "controlled": False,
                "note": "not dropped or measured; a system-cold lane is pending",
            },
            "cold": {
                "label": "connection-cold",
                "procedure": (
                    "before every cold sample the workspace connection is closed and the "
                    "workspace adopted again, as a service restart does: a fresh SQLite "
                    "connection, page cache and prepared statements, and a rebuilt "
                    "dispatcher; the operation then runs once with no warm-up"
                ),
                "controlled": ["sqlite connection", "sqlite page cache", "dispatcher state"],
                "not_controlled": [
                    "operating-system page cache",
                    "CPU caches",
                    "process-level Python caches and module state",
                ],
            },
            "warm": {
                "label": "warm",
                "procedure": (
                    f"{config.warmup} discarded requests per operation on one connection, "
                    "then the measured samples on the same connection"
                ),
            },
        },
        "concurrency": {
            "declared": lanes["concurrent"]["declared_workload"],
            "cold_and_warm_lanes": "one request at a time",
        },
        "sampling": {
            "search_samples": config.search_samples,
            "pack_samples": config.pack_samples,
            "checkpoint_samples_per_size_class": config.checkpoint_samples,
            "cold_samples_per_operation": config.cold_samples,
            "warmup_requests": config.warmup,
            "concurrent_requests": lanes["concurrent"]["total_requests"],
        },
        "lanes": lanes,
        "resources": resources,
        "reference": {
            "profile": REFERENCE_PROFILE,
            "warm_targets": targets,
            "warm_targets_note": (
                "within_target is advisory unless release_blockers is empty: the targets "
                "are defined only on the reference profile"
            ),
            "release_eligible": not blockers,
            "release_blockers": blockers,
        },
    }


# --- the report contract ---------------------------------------------------------------

_DIGEST_HEX = frozenset("0123456789abcdef")


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _DIGEST_HEX


def _is_iso(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def validate_report(report: dict[str, Any]) -> None:
    """Assert the report carries every §20.2 dimension. Never inspects a latency value."""
    problems: list[str] = []

    def need(condition: bool, message: str) -> None:
        if not condition:
            problems.append(message)

    def section(parent: dict[str, Any], key: str) -> dict[str, Any]:
        value = parent.get(key)
        need(isinstance(value, dict), f"{key} is missing or not an object")
        return value if isinstance(value, dict) else {}

    need(report.get("report_format") == REPORT_FORMAT, "report_format")
    run = section(report, "run")
    need(_is_iso(run.get("started_at")) and _is_iso(run.get("finished_at")), "run timestamps")

    need(section(report, "session_policy") == SESSION_POLICY, "session_policy")

    env = section(report, "environment")
    cpu, memory, storage = section(env, "cpu"), section(env, "memory"), section(env, "storage")
    os_record, runtimes = section(env, "os"), section(env, "runtimes")
    need("model" in cpu and isinstance(cpu.get("logical_count"), int), "cpu model/count")
    need("physical_count" in cpu, "cpu physical_count (null when unavailable)")
    need(
        isinstance(memory.get("physical_bytes"), int) or bool(memory.get("source")),
        "physical RAM or its source",
    )
    need(isinstance(storage.get("class"), str) and storage.get("source"), "storage class + source")
    for key in ("name", "version", "build", "architecture"):
        need(bool(os_record.get(key)), f"os.{key}")
    for key in ("python", "sqlite", "core_runtime", "contract"):
        need(bool(runtimes.get(key)), f"runtimes.{key}")

    source = section(report, "source")
    need(
        (isinstance(source.get("commit"), str) and len(source["commit"]) == 40
         and isinstance(source.get("dirty"), bool))
        or bool(source.get("unavailable")),
        "source commit + dirty state, or an explicit unavailable reason",
    )
    if source.get("dirty"):
        need(_is_digest(source.get("dirty_digest")), "dirty_digest of a dirty tree")
    migration = section(report, "migration")
    need(isinstance(migration.get("head"), int) and bool(migration.get("head_name")), "migration head")
    need(bool(section(report, "database")), "database configuration")

    corpus = section(report, "corpus")
    need(bool(corpus.get("generator")) and bool(corpus.get("generator_version")), "corpus generator")
    need(_is_digest(corpus.get("digest")), "corpus digest")
    acl = section(corpus, "acl")
    need(acl.get("open_partition_records", 0) > 0 and acl.get("labeled_records", 0) > 0, "ACL partitions")
    need(section(corpus, "code_spans").get("long_span_records", 0) > 0, "long code spans")
    snapshots = section(corpus, "source_snapshots")
    worktrees = snapshots.get("worktrees", [])
    need(_is_digest(snapshots.get("digest")), "source snapshot digest")
    need(len(worktrees) >= 2, "at least two worktrees")
    need(len({w.get("checkout_id") for w in worktrees}) == len(worktrees), "distinct checkout identities")
    need(
        all(
            _is_digest(str(w.get("manifest_digest", "")).removeprefix("sha256:"))
            and w.get("snapshot_id")
            and w.get("observations", 0) > 0
            for w in worktrees
        ),
        "every worktree names its snapshot, manifest digest and observations",
    )
    groups = section(corpus, "conflict_groups")
    need(groups.get("count", 0) >= 1 and bool(groups.get("method")), "conflict groups + method")
    need(
        sum(section(groups, "discovery").get("relation_candidates", {}).values()) == groups.get("count"),
        "one relation per conflict group",
    )
    sizes = section(corpus, "checkpoint_payloads")
    need(set(sizes) == set(CHECKPOINT_CLASSES), "checkpoint size classes")

    policy = section(report, "policy")
    need(_is_digest(policy.get("digest")) and bool(policy.get("snapshot")), "policy snapshot + digest")
    need(bool(report.get("effective_context_budget")), "effective context budget")

    cache = section(report, "cache")
    need(section(cache, "os_page_cache").get("controlled") is False, "OS page cache honesty")
    need(
        bool(section(cache, "cold").get("procedure")) and bool(section(cache, "warm").get("procedure")),
        "cache-state procedures",
    )
    need(bool(section(report, "concurrency").get("declared")), "declared concurrency")
    need(bool(section(report, "sampling")), "sample counts")

    lanes = section(report, "lanes")
    need({"cold", "warm", "concurrent"} <= set(lanes), "cold, warm and concurrent lanes")
    expected = set(READ_OPERATIONS) | set(CHECKPOINT_OPERATIONS)
    for lane in ("cold", "warm"):
        operations = section(section(lanes, lane), "operations")
        need(set(operations) == expected, f"{lane} lane operations")
        for name, stats in operations.items():
            need(isinstance(stats, dict) and stats.get("n", 0) > 0, f"{lane}/{name} has samples")
        for name in CHECKPOINT_OPERATIONS:
            need(bool(operations.get(name, {}).get("payload_bytes")), f"{lane}/{name} payload bytes")
    concurrent = section(lanes, "concurrent")
    need(concurrent.get("total_requests", 0) > 0 and bool(concurrent.get("operations")), "concurrent samples")
    for lane in ("cold", "warm", "concurrent"):
        need(bool(section(lanes, lane).get("resources")), f"{lane} resources")

    resources = section(report, "resources")
    need(len(resources.get("points", [])) >= 2, "resource points")
    need(
        resources.get("peak_rss_bytes") is not None or bool(resources.get("peak_rss_unavailable")),
        "peak RSS or its unavailable label",
    )
    need(bool(resources.get("working_set")), "working-set label")
    reference = section(report, "reference")
    need(set(reference.get("warm_targets", {})) == set(REFERENCE_TARGETS), "reference warm targets")
    need(isinstance(reference.get("release_blockers"), list), "release blockers")
    assert not problems, "qualification report contract violations: " + "; ".join(problems)


# --- tests ------------------------------------------------------------------------------

#: The smallest corpus the config accepts, and one sample per lane: the ordinary-suite
#: smoke checks the report's shape and the harness's correctness gates, never a latency.
SMOKE = LaneConfig(
    corpus=2 * 2 + _PAIRS,
    worktrees=2,
    conflict_groups=2,
    search_samples=2,
    pack_samples=1,
    checkpoint_samples=1,
    cold_samples=1,
    warmup=1,
    readers=2,
    reader_requests=6,
    writer_checkpoints=3,
    storage_class="local-ssd",
)


#: The production capture seal walks checkouts without following links; a host that
#: lacks that primitive cannot run the lane, as for every other capture test.
requires_checkout_walk = pytest.mark.skipif(
    not source_capture._NO_FOLLOW_WALK, reason="host lacks no-follow checkout walk"
)


@pytest.fixture(scope="module")
def smoke_report(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return run_lane(tmp_path_factory.mktemp("qualification-smoke"), SMOKE)


@requires_checkout_walk
def test_smoke_lane_report_satisfies_the_contract(smoke_report: dict[str, Any]) -> None:
    validate_report(smoke_report)
    report = smoke_report
    assert report["corpus"]["observations"] == SMOKE.corpus
    assert len(report["corpus"]["source_snapshots"]["worktrees"]) == 2
    assert report["corpus"]["conflict_groups"]["count"] == 2
    cold, warm = report["lanes"]["cold"], report["lanes"]["warm"]
    assert cold["operations"].keys() == warm["operations"].keys()
    assert cold["samples_per_operation"] == SMOKE.cold_samples
    assert warm["warmup_requests_per_operation"] == SMOKE.warmup
    # Near-limit payloads really approach the production cap without crossing it.
    near = warm["operations"]["continuity.checkpoint.append.near_limit"]["payload_bytes"]
    short = warm["operations"]["continuity.checkpoint.append.short"]["payload_bytes"]
    medium = warm["operations"]["continuity.checkpoint.append.medium"]["payload_bytes"]
    assert short["max"] < medium["min"] < near["min"] <= near["max"] <= CHECKPOINT_PAYLOAD_CAP_BYTES
    assert near["min"] >= int(CHECKPOINT_PAYLOAD_CAP_BYTES * 0.95)
    assert report["reference"]["release_eligible"] is False
    json.dumps(report)  # the report serializes


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["environment"]["cpu"].pop("model"),
        lambda r: r["environment"].pop("memory"),
        lambda r: r["source"].pop("commit"),
        lambda r: r["corpus"].pop("digest"),
        lambda r: r["corpus"]["source_snapshots"]["worktrees"].pop(),
        lambda r: r["corpus"]["conflict_groups"].pop("method"),
        lambda r: r["lanes"].pop("cold"),
        lambda r: r["lanes"]["warm"]["operations"].pop("continuity.checkpoint.append.near_limit"),
        lambda r: r["lanes"].pop("concurrent"),
        lambda r: r["cache"]["cold"].pop("procedure"),
        lambda r: r["cache"]["os_page_cache"].update(controlled=True),
        lambda r: r["resources"].update(peak_rss_bytes=None, peak_rss_unavailable=None),
        lambda r: r["policy"].pop("digest"),
        lambda r: r.pop("migration"),
        lambda r: r.pop("session_policy"),
        lambda r: r["session_policy"].update(grant_lifetime_us=10**15),
        lambda r: r["session_policy"].update(grants_refreshed_in_measured_requests=True),
        lambda r: r["session_policy"].update(unrecorded_privilege=True),
    ],
)
@requires_checkout_walk
def test_report_contract_rejects_a_missing_dimension(
    smoke_report: dict[str, Any], mutate: Callable[[dict[str, Any]], object]
) -> None:
    broken = deepcopy(smoke_report)
    mutate(broken)
    with pytest.raises(AssertionError, match="contract violations"):
        validate_report(broken)


#: The failing 100k run died at this request count and elapsed time (47 102 requests,
#: 1 764 s) with the grant "outside its validity window".
_FORMER_BOUNDARY_REQUESTS = 47_102
_FORMER_BOUNDARY_SECONDS = 1_764
_EXPIRED = "the grant presented is outside its validity window"


def _stalled_monotonic(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    """Every real monotonic reading lands `seconds` after the last: a stalled process."""
    real, readings = time.monotonic, [0]

    def stalled() -> float:
        readings[0] += 1
        return real() + readings[0] * seconds

    monkeypatch.setattr(time, "monotonic", stalled)


def _create(ws: sc.Workspace, key: str) -> Any:
    return ws.call("memory.create", _plan(0, SMOKE).payload, key=key)


def test_qualification_session_outlives_the_former_failing_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old harness refuses a request that straddles a stall; the lane's session does not.

    Same stall for both (each real monotonic reading `_FORMER_BOUNDARY_SECONDS` after the
    last, the request counter at the former failing count), no corpus seeded.
    """
    _stalled_monotonic(monkeypatch, _FORMER_BOUNDARY_SECONDS)
    (tmp_path / "ordinary").mkdir()
    (tmp_path / "qualification").mkdir()
    ordinary = sc.Workspace(tmp_path / "ordinary")
    ordinary._requests = _FORMER_BOUNDARY_REQUESTS
    assert _create(ordinary, "old-harness").error.message == _EXPIRED
    ordinary.holder.connection.close()

    qualification = sc.Workspace(tmp_path / "qualification", clock=QualificationClock())
    qualification._requests = _FORMER_BOUNDARY_REQUESTS
    for index in range(3):
        assert isinstance(_create(qualification, f"lane-{index}"), SuccessResponseEnvelope)


def test_qualification_session_still_enforces_its_grant_window(tmp_path: Path) -> None:
    clock = QualificationClock()
    ws = sc.Workspace(tmp_path, clock=clock)
    assert isinstance(_create(ws, "within-window"), SuccessResponseEnvelope)
    # A reading that moves further than the window per step expires the grant mid-request.
    clock.STEP_US = DEFAULT_GRANT_LIFETIME_US + 1
    assert _create(ws, "beyond-window").error.message == _EXPIRED


def test_session_policy_matches_the_clock_and_the_production_default() -> None:
    clock = QualificationClock()
    first, second = clock.monotonic(), clock.monotonic()
    assert second - first == QualificationClock.STEP_US / 1_000_000
    assert SESSION_POLICY["grant_lifetime_us"] == DEFAULT_GRANT_LIFETIME_US == 60_000_000
    assert SESSION_POLICY["grant_window_readings"] * QualificationClock.STEP_US == (
        DEFAULT_GRANT_LIFETIME_US
    )


def test_corpus_identity_is_reproducible_and_sensitive_to_its_configuration() -> None:
    config = replace(SMOKE, corpus=2_000, worktrees=3, conflict_groups=5)
    first = corpus_identity(config)
    assert first == corpus_identity(config)
    assert corpus_identity(replace(config, worktrees=2))["digest"] != first["digest"]
    assert corpus_identity(replace(config, conflict_groups=6))["digest"] != first["digest"]
    assert corpus_identity(replace(config, corpus=2_001))["digest"] != first["digest"]
    assert first["kinds"]["conflict_member"] == 10
    assert sum(first["kinds"].values()) == 2_000
    assert set(first["per_worktree"]) == {0, 1, 2} and sum(first["per_worktree"].values()) == 2_000
    assert 0 < first["long_span_records"] < 2_000
    # Conflict pairs share a worktree: a cross-checkout pair would be a scoped difference.
    for group in range(5):
        pair = [_plan(2 * group, config), _plan(2 * group + 1, config)]
        assert pair[0].worktree == pair[1].worktree
        assert pair[0].payload["content"]["topic_ref"] == pair[1].payload["content"]["topic_ref"]


@pytest.mark.parametrize("config", [SMOKE, LaneConfig(), LaneConfig(corpus=100_000)])
def test_every_bucket_has_a_worktree_to_target(config: LaneConfig) -> None:
    buckets = _bucket_worktrees(config)
    assert len(buckets) == _PAIRS and all(buckets)


def test_checkpoint_classes_are_measured_by_the_production_canonicalizer() -> None:
    sizes = {name: _checkpoint_bytes(_checkpoint_payload(name, 7)) for name in CHECKPOINT_CLASSES}
    assert sizes["short"] < 1_024
    assert 16 * 1024 - 8 <= sizes["medium"] <= 16 * 1024
    assert CHECKPOINT_PAYLOAD_CAP_BYTES - 8 * 1024 - 8 <= sizes["near_limit"] < CHECKPOINT_PAYLOAD_CAP_BYTES
    assert _checkpoint_payload("medium", 7) != _checkpoint_payload("medium", 8)


def test_percentiles_shape() -> None:
    assert _percentiles([]) == {
        "n": 0, "min_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0, "max_ms": 0.0, "mean_ms": 0.0,
    }
    stats = _percentiles([1.0, 2.0, 3.0, 4.0, 100.0])
    assert (stats["n"], stats["min_ms"], stats["max_ms"], stats["p50_ms"]) == (5, 1.0, 100.0, 3.0)


@requires_checkout_walk
@pytest.mark.skipif(
    os.environ.get("OMNIVIA_ENGINEERING_QUALIFICATION") != "1",
    reason="qualification lanes run only under OMNIVIA_ENGINEERING_QUALIFICATION=1",
)
def test_engineering_performance_qualification_lane(tmp_path: Path) -> None:
    config = LaneConfig.from_environment()
    report = run_lane(tmp_path, config, progress=True)
    out_dir = Path(
        os.environ.get("OMNIVIA_QUALIFICATION_REPORT_DIR", "benchmarks/reports/engineering-memory")
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"lane-{config.corpus}.json"
    out_file.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"report written to {out_file}", flush=True)
