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


def _pair(index: int) -> tuple[str, str]:
    """The (stem, component) bucket an index falls in, one of 160 evenly."""
    slot = index % _PAIRS
    return STEMS[slot % len(STEMS)], COMPONENTS[slot // len(STEMS)]


def _query(run: int) -> str:
    """The query naming exactly one (stem, component) pair."""
    stem, component = _pair(run)
    return f"{stem} {component}"


def _observation(index: int) -> dict[str, Any]:
    stem, component = _pair(index)
    return sc._observation(
        sc._manifest(SNAPSHOT),
        title=f"{stem} {component} finding {index}",
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
        # The baseline must attest every path `sc._manifest`'s dependencies name
        # (src/auth.py, src/util.py, README.md), not just the required one: a
        # baseline missing an attested digest leaves the evaluator `unknown`
        # for every seeded record, so no current_safe probe could ever match.
        ws.record(sc._source(1, SNAPSHOT, sc.FILES_A))

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
                {"query": _query(0), "view": "candidates"},
            )
        search_samples: list[float] = []
        for run in range(samples_search):
            started = time.perf_counter()
            result = ws.ok(
                "engineering.search",
                {"query": _query(run), "view": "candidates"},
            )
            search_samples.append((time.perf_counter() - started) * 1000.0)
            assert result["previews"], "a seeded query must match its corpus"
        print("search percentiles:", _percentiles(search_samples), flush=True)

        # --- preview search, current_safe -------------------------------------
        # Bounded by design: each query names one (stem, component) pair, at
        # most 625 matches at the 100 000 lane, inside CURRENT_SAFE_CANDIDATE_CAP.
        safe_search_samples: list[float] = []
        for run in range(samples_search):
            started = time.perf_counter()
            result = ws.ok(
                "engineering.search",
                {
                    "query": _query(run),
                    "view": "candidates",
                    "applicability_mode": "current_safe",
                    "repository_target": {
                        "repository_id": REPOSITORY,
                        "snapshot_id": SNAPSHOT,
                    },
                },
            )
            safe_search_samples.append((time.perf_counter() - started) * 1000.0)
            assert result["previews"], "a seeded query must match its corpus under current_safe"
            assert {p["applicability"] for p in result["previews"]} == {"matched"}
        print("current_safe search percentiles:", _percentiles(safe_search_samples), flush=True)

        # --- context pack ----------------------------------------------------
        pack_input = {
            "query": _query(0),
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
            pack_input["query"] = _query(run)
            started = time.perf_counter()
            result = ws.ok("engineering.context.build", pack_input)
            pack_samples.append((time.perf_counter() - started) * 1000.0)
            assert result["pack"]["fresh_authorization_required"] is True
        print("pack percentiles:", _percentiles(pack_samples), flush=True)

        # --- context pack, current_safe ---------------------------------------
        # Not asserted to pass at scale: unlike search, the pack builder still
        # hydrates its authorized frontier's bodies before any query filter
        # narrows it (a separate pending change), so this probe only records
        # whatever the current implementation does -- a pack or a refusal --
        # diagnostically, at this corpus size.
        safe_pack_input = {
            **pack_input,
            "query": _query(0),
            "applicability_mode": "current_safe",
        }
        safe_pack_started = time.perf_counter()
        safe_pack_response = ws.call("engineering.context.build", safe_pack_input)
        safe_pack_elapsed_ms = (time.perf_counter() - safe_pack_started) * 1000.0
        safe_pack_outcome = (
            "success"
            if isinstance(safe_pack_response, sc.SuccessResponseEnvelope)
            else safe_pack_response.error.code
        )
        print(
            f"current_safe pack probe: {safe_pack_outcome} in {safe_pack_elapsed_ms:.1f}ms",
            flush=True,
        )

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
                "engineering.search.current_safe": _percentiles(safe_search_samples),
                "engineering.context.build": _percentiles(pack_samples),
                "engineering.context.build.current_safe": {
                    "outcome": safe_pack_outcome,
                    "elapsed_ms": round(safe_pack_elapsed_ms, 3),
                },
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
