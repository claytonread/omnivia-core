"""Engineering-memory performance qualification lanes (SPEC §20.2; plan PR-H2).

Deliberately excluded from the ordinary suite: every test here is skipped
unless ``OMNIVIA_ENGINEERING_QUALIFICATION`` is set, because a lane seeds a
synthetic corpus of observations through the production writer before it
measures anything.

One lane per invocation, corpus size from ``OMNIVIA_QUALIFICATION_CORPUS``
(default 10 000). The lane seeds, then measures three operations through the
production application surface -- preview search, context-pack build and
checkpoint metadata commit -- and writes p50/p95/p99 samples with the
environment record to ``benchmarks/reports/engineering-memory/``. Latencies
are recorded, never asserted: §20.2's targets are qualification inputs, not
test gates, and correctness gates (bounded results, honest coverage) are still
asserted on every sample.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import test_engineering_source_coverage as sc

from omnivia_core.contracts.v1 import MutationPrecondition

WORKSPACE_ID = sc.WORKSPACE_ID
REPOSITORY = sc.REPOSITORY
SNAPSHOT = "esnap-qual"

#: Twenty topic stems; each query matches roughly a twentieth of the corpus,
#: so the measured searches rank a realistic candidate set rather than one
#: all-matching frontier.
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


def _observation(index: int) -> dict[str, Any]:
    stem = STEMS[index % len(STEMS)]
    return sc._observation(
        sc._manifest(SNAPSHOT),
        title=f"{stem} finding {index}",
    )


def _percentiles(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    def pct(fraction: float) -> float:
        if not ordered:
            return 0.0
        rank = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
        return ordered[rank]
    return {
        "n": len(ordered),
        "p50_ms": round(pct(0.50), 3),
        "p95_ms": round(pct(0.95), 3),
        "p99_ms": round(pct(0.99), 3),
        "mean_ms": round(statistics.fmean(ordered), 3) if ordered else 0.0,
    }


@pytest.mark.skipif(
    os.environ.get("OMNIVIA_ENGINEERING_QUALIFICATION") != "1",
    reason="qualification lanes run only under OMNIVIA_ENGINEERING_QUALIFICATION=1",
)
def test_engineering_performance_qualification_lane(tmp_path: Path) -> None:
    corpus = int(os.environ.get("OMNIVIA_QUALIFICATION_CORPUS", "10000"))
    samples_search = int(os.environ.get("OMNIVIA_QUALIFICATION_SEARCH_SAMPLES", "100"))
    samples_pack = int(os.environ.get("OMNIVIA_QUALIFICATION_PACK_SAMPLES", "30"))
    samples_checkpoint = int(
        os.environ.get("OMNIVIA_QUALIFICATION_CHECKPOINT_SAMPLES", "100")
    )

    ws = sc.Workspace(tmp_path)
    try:
        ws.record(sc._source(1, SNAPSHOT, {"src/auth.py": sc.AUTH_V1}))

        seed_started = time.perf_counter()
        for index in range(corpus):
            ws.observe(_observation(index), key=f"qual-seed-{index}")
            if (index + 1) % 1000 == 0:
                print(f"seeded {index + 1}/{corpus}", flush=True)
        seed_seconds = time.perf_counter() - seed_started
        print(f"seed {corpus} in {seed_seconds:.1f}s", flush=True)

        # --- preview search --------------------------------------------------
        for _ in range(5):
            ws.ok(
                "engineering.search",
                {"query": "authentication finding", "view": "candidates"},
            )
        search_samples: list[float] = []
        for run in range(samples_search):
            query = f"{STEMS[run % len(STEMS)]} finding"
            started = time.perf_counter()
            result = ws.ok(
                "engineering.search",
                {"query": query, "view": "candidates"},
            )
            search_samples.append((time.perf_counter() - started) * 1000.0)
            assert result["previews"], "a seeded query must match its corpus"
        print("search percentiles:", _percentiles(search_samples), flush=True)

        # --- context pack ----------------------------------------------------
        pack_input = {
            "query": "authentication finding",
            "targets": [
                {
                    "repository_id": REPOSITORY,
                    "snapshot_id": SNAPSHOT,
                    "snapshot_kind": "git_commit",
                }
            ],
            "profile": "investigate",
        }
        built = ws.ok("engineering.context.build", pack_input)
        assert built["pack"]["format_version"] == "engineering_context.v1"
        pack_samples: list[float] = []
        for run in range(samples_pack):
            pack_input["query"] = f"{STEMS[run % len(STEMS)]} finding"
            started = time.perf_counter()
            result = ws.ok("engineering.context.build", pack_input)
            pack_samples.append((time.perf_counter() - started) * 1000.0)
            assert result["pack"]["fresh_authorization_required"] is True
        print("pack percentiles:", _percentiles(pack_samples), flush=True)

        # --- checkpoint commit -----------------------------------------------
        registered = ws.ok(
            "continuity.session.register",
            {
                "schema_version": "engineering.1",
                "checkout_hint": "/home/dev/qual",
                "host_session_ref": "qual-conv-1",
            },
        )
        session_id = registered["session"]["session_id"]
        checkpoint_samples: list[float] = []
        for run in range(samples_checkpoint):
            payload: dict[str, Any] = {
                "session_id": session_id,
                "payload": {
                    "objective": f"{STEMS[run % len(STEMS)]} investigation step {run}",
                    "checkpoint_kind": "periodic",
                    "unresolved_work": [f"open question {run}"],
                },
            }
            if run > 0:
                payload["expected_parent_sequence"] = run
            started = time.perf_counter()
            result = ws.ok(
                "continuity.checkpoint.append",
                payload,
                mutation_precondition=MutationPrecondition(record_version=f"seq-{run}"),
            )
            checkpoint_samples.append((time.perf_counter() - started) * 1000.0)
            assert result["receipt"]["sequence"] == run + 1
        print("checkpoint percentiles:", _percentiles(checkpoint_samples), flush=True)

        # --- report ----------------------------------------------------------
        report = {
            "lane": "engineering-memory-qualification",
            "recorded_at": datetime.now(UTC).isoformat(),
            "environment": {
                "platform": platform.platform(),
                "machine": platform.machine(),
                "python": platform.python_version(),
                "cpu_count": os.cpu_count(),
            },
            "corpus_observations": corpus,
            "seed_seconds": round(seed_seconds, 1),
            "operations": {
                "engineering.search": _percentiles(search_samples),
                "engineering.context.build": _percentiles(pack_samples),
                "continuity.checkpoint.append": _percentiles(checkpoint_samples),
            },
        }
        out_dir = Path("benchmarks/reports/engineering-memory")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"lane-{corpus}.json"
        out_file.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"report written to {out_file}", flush=True)
    finally:
        with contextlib.suppress(Exception):
            ws.holder.connection.close()
