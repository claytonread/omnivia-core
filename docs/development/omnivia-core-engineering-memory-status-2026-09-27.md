# OmniVia Core — Engineering Memory: consolidated implementation status

**Date:** 2026-09-27 · **Spec:** `SPEC-CORE-ENGMEM-001` v1.0 (2026-09-25) · **Plan:** `omnivia-core-engineering-memory-implementation-plan-2026-09-26.md`

## Delivery sequence

| PR | Scope | Spec packages |
|---|---|---|
| #127 | Contracts: 9-engineering-operation catalogue slice, `engineering.schema.json`, wire corpus, migration allocation pins; continuity handlers | P0-00, P0-02 (contracts + continuity) |
| #128 | Repository identity (§6), observation ingestion, search/expand with frontier-frozen ranking, applicability families, priority + review, pack builder | P0-01, P0-03, P0-04, P0-05 |
| #129 | Follow-up correctness work absorbed from review | — |
| #131 | MCP exposure: engineering reads in the restricted manifest, checkpoint append in authoring; installed profiles + traceability | P0-07 (consumer half) |
| #132 | Context budget contract work | P0-05 |
| #133 | Pack correctness conformance | P0-05 |
| #135 | Immutable working-tree source capture | P0-01 (capture foundation) |
| #134 (merged) | Installed CLI search, expansion and context-pack qualification | P0-07 (consumer proof) |
| #136 (merged) | Trusted repository/checkout registration | P0-01 (registration) |
| #137 (this PR, open) | CLI continuity proof, migration/restore evidence, diagnostic scale measurements, SQL frontier chunking and query-bounded `current_safe` search | P0-07 (qualification work) |

## What exists on `main` today

- **Ten engineering operations** in the frozen catalogue: `continuity.session.register`, `continuity.checkpoint.append`, `continuity.session.close`, `continuity.handoff.read`, `engineering.search`, `engineering.expand`, `engineering.context.build`, `context.priority.set`, `engineering.review.record`, `engineering.source.record`.
- **Seven migrations** (0047–0053): repository identity, continuity, applicability, source coverage, dependency carry, dependency lookup, preview projection.
- **MCP exposure** through the curated manifest (restricted + authoring profiles), generated exposure schemas, traceability ledger.
- **CLI surface**: ten generic application commands over `omnivia-core-client`; PR #134 added the managed-start installed-CLI vertical test.
- **Engineering runtime suites**, MCP stdio e2e and architecture gates, plus the env-gated diagnostic qualification lane. This branch's engineering-focused run completed with 338 passed and 1 skipped.

## Captured-source branch delta

This branch adds the accepted `engineering.source.capture.commit` operation and the
0056 captured-source representation. The trusted capture path seals a rich-manifest
evidence row plus an immutable path-to-digest index, and the commit handler binds that
seal to one authenticated installation, registered checkout and source stream. The
installed service now polls registered local checkouts from the managed service tick,
independent of request traffic, and resumes a sealed-but-uncommitted capture after
restart. It fills a linked coverage gap before appending a head and skips a refused
oldest seal within each bounded batch. Migration 0058 adds the service-owned queue,
bounded retry timing and persisted lane, queue, checkout and legacy-seeding cursors.
Next-eligible and capture-seeding indexes keep ordinary scheduling independent of
captured-source history, and exact persisted frontier intent fails closed after a
competing append. The mutation remains absent from MCP and is available to trusted
clients as `engineering capture`.

## PR-H2 evidence (this change)

1. **CLI vertical** (`test_engineering_cli.py`): the spec §1.1 initial slice — register → append (fenced) → close-with-final-checkpoint → handoff → working-context search → resume pack — through the installed `omnivia` entry point against a real managed-start service; plus the AC-023 stale-predecessor typed refusal.
2. **Migration/restore** (`test_engineering_restore.py`): verified backup → restore → row-identical engineering tables (checkpoints by digest+sequence, sessions, snapshots) and the observation still readable at its exact version through the production surface (AC-063 storage half).
3. **Qualification lanes** (`test_engineering_qualification.py`, env-gated): 10k and 100k-observation synthetic corpora; p50/p95/p99 for `engineering.search`, `engineering.context.build`, `continuity.checkpoint.append`; reports written to `benchmarks/reports/engineering-memory/lane-<n>.json`.
4. **Release evidence manifest**: `omnivia-core-engineering-memory-release-evidence-2026-09-27.md`.

## Performance qualification (measured 2026-09-27, this machine)

Lane 10 000 observations (`benchmarks/reports/engineering-memory/lane-10000.json`, seed 78.6 s):

| Operation | p50 | p95 | p99 | §20.2 target (p95) | Verdict |
|---|---:|---:|---:|---:|---|
| `engineering.search` (preview, 100 samples) | 650 ms | 725 ms | 810 ms | ≤ 300 ms | **over target** |
| `engineering.context.build` (investigate, 30 samples) | 2.96 s | 3.30 s | 3.33 s | ≤ 1 s | **over target** |
| `continuity.checkpoint.append` (100 samples) | 1.05 ms | 1.5 ms | 8.6 ms | ≤ 200 ms | inside target |

The search gap is structural: the preview path scores the full admitted candidate set in Python per query (no SQL-side top-k), so latency scales with corpus size. Pack construction inherits the frontier scan. These are measurements, not release guarantees. There is no valid completed 100 000-observation report. The previous rerun used code that this branch has since changed and was stopped; a fresh run is required after the scale fixes are integrated. (Fixture cost datum for that fresh run: seeding 100 000 observations through the production writer took 3 567.7 s (~59.5 min) on this machine, before any measurement sample ran.) The fixture still needs the worktree, ACL, conflict, cache-state, concurrency and environment dimensions required by spec §20.2 before it can serve as release qualification.

## Scale-qualification finding: SQLite host-parameter ceiling (found and fixed)

The first 100 000-observation lane failed with `sqlite3.OperationalError: too many SQL variables`: `read_authorized_memory_frontier` folds evidence links, permission labels and governance transitions by `IN (...)` lists sized by the admitted frontier, and a workspace at 100k records crosses SQLite's host-parameter ceiling. Any workspace past tens of thousands of records would fail `memory.search` and `engineering.search` the same way — a genuine production correctness bug at scale, which is exactly the class of finding the §20.2 scale-qualification lane exists to produce. Fixed in `50f4fa7a` by issuing each fold in fixed 512-id chunks and re-sorting the merged rows by the statements' own ORDER BY keys, reproducing the unchunked statement's rows and order exactly; digest-sensitive suites (2 668 corpus/conformance tests, 701 memory/engineering tests) answer identically, and `test_memory_frontier_chunking.py` pins the boundary with a deterministic 540-record frontier. Commit `84d37b92` also spends the `current_safe` applicability cap only on authorised query matches and repairs the source fixture. A fresh 100k diagnostic run remains pending after the pack builder stops hydrating the full frontier.

## Honest limitations (carried into the release note)

- Lease expiry and binding-generation fencing are recorded but not enforced (§7.3).
- Single-principal Personal mode only; no validated organisational isolation (§19.4).
- Applicability is dependency-qualified against recorded source streams. Core has a bounded local polling producer, durable-seal crash recovery, restart-persistent scheduling fairness and indexed pending lookup. Platform filesystem notifications and Dev semantic parser/indexer adapters remain external integration work.
- Context packs do not yet emit known-conflict warnings; conflict discovery and governed reconciliation are incomplete release work.
- Semantic assessment (P2-08) is deliberately not implemented; it waits on the owner gates G-2 (Laya distribution pin) and G-3 (signed-manifest trust anchor).
- Performance numbers are lane measurements on the development machine that produced them, not qualified release guarantees (§20.2).

## Owner gates still open

- **G-2** — Laya distribution pin (publication decision).
- **G-3** — signed-manifest trust anchor (key-custody ruling).
- Handoff: `omnivia-core-decision-runtime-gates-g2-g3-resolution-handoff-2026-09-26.md`.
