# OmniVia Core — Engineering Memory: consolidated implementation status

**Date:** 2026-09-27 · **Spec:** `SPEC-CORE-ENGMEM-001` v1.0 (2026-09-25) · **Plan:** `omnivia-core-engineering-memory-implementation-plan-2026-09-26.md`

## Final closeout (verified 2026-10-04)

The Engineering Memory implementation is complete on `main` through PR #169
(merge `8863c7d6`). The final migration head is 0063. Acceptance criteria
AC-001 through AC-064 are verified in
`engineering-memory-acceptance-evidence-2026-10-03.md`. The delivery sequence and
PR-H2 sections below preserve the implementation history; later closeout evidence
and the committed format-2 qualification reports supersede their branch-era status.

## Follow-on slice (branch `codex/engineering-memory-followons`, 2026-10-05, uncommitted)

Three Core-side follow-ons, each described in `engineering-source-coverage.md`, extend the
closeout above. Migration head moves from 0063 to 0065.

- **Cross-principal continuity handoff grants (migration 0064).** `continuity.handoff.grant`
  and `continuity.handoff.revoke` let the owner of one exact checkpoint, pinned by its digest,
  let one other existing principal read its redacted `continuity_handoff.v1` view by checkpoint
  id, for a bounded time. Grants are append-only, audited, revocable on the next read,
  expiring and non-delegable; session-and-sequence lookup stays owner-only, and a session
  close does not revoke. Both are trusted, non-MCP mutations.
- **Trusted selector attestations (migration 0065).** `engineering.selector.attest` ingests an
  installed Dev adapter's `symbol` and `source_span` coverage as the authenticated stream
  owner, and the evaluator compares attested selector digests (`matched`, `potentially_stale`,
  `invalid`, otherwise `unknown`). Core never parses source; `whole_file` behaviour is
  unchanged and every other selector type stays `unknown`. No installed adapter yet emits
  attestations.
- **Search/context performance.** See the "Search and context narrowing" section of
  `engineering-source-coverage.md`; it adds no migration.

Still excluded, unchanged: **G-2** (Laya distribution pin), **G-3** (signed-manifest trust
anchor) and the semantic conflict assessor (P2-08) that depends on both. None of them is
implemented, pinned or simulated by this slice.

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
| #137 (merged; historical milestone) | CLI continuity proof, migration/restore evidence, diagnostic scale measurements, SQL frontier chunking and query-bounded `current_safe` search | P0-07 (qualification work) |

## Historical `main` snapshot at PR #137

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
3. **Qualification lanes** (`test_engineering_qualification.py`, env-gated): 10k and 100k-observation synthetic corpora; reports written to `benchmarks/reports/engineering-memory/lane-<n>.json`. The harness produces the §20.2 report contract (`engineering-memory-qualification/2`: worktrees, ACL, long code spans, conflict groups, three checkpoint size classes, cold/warm/concurrent lanes, environment, source and policy identity, resource observations); the contract is validated by ordinary-suite tests at a tiny corpus. The format-2 10k and 100k lanes are complete. The cold lane is SQLite connection/page-cache cold; the OS page cache is uncontrolled, so no system-cold lane is claimed. See the release-evidence document.
4. **Release evidence manifest**: `omnivia-core-engineering-memory-release-evidence-2026-09-27.md`.

## Performance qualification (measured 2026-09-27/29, this machine)

Lane 10 000 observations (`benchmarks/reports/engineering-memory/lane-10000.json`, seed 78.6 s):

| Operation | p50 | p95 | p99 | §20.2 target (p95) | Verdict |
|---|---:|---:|---:|---:|---|
| `engineering.search` (preview, 100 samples) | 650 ms | 725 ms | 810 ms | ≤ 300 ms | **over target** |
| `engineering.context.build` (investigate, 30 samples) | 2.96 s | 3.30 s | 3.33 s | ≤ 1 s | **over target** |
| `continuity.checkpoint.append` (100 samples) | 1.05 ms | 1.5 ms | 8.6 ms | ≤ 200 ms | inside target |

**Lane 100 000 observations** (`benchmarks/reports/engineering-memory/lane-100000.json`, seed 3 054.4 s ≈ 50.9 min, macOS arm64 / 18-core / Python 3.11.15) — **completed end to end for the first time** after #143's hydration chunking, on main at `50d1274f`:

| Operation | p50 | p95 | p99 | Verdict |
|---|---:|---:|---:|---|
| `engineering.search` (diagnostic, 30 samples) | 3.32 s | 3.57 s | 3.78 s | over target, scales with corpus |
| `engineering.search` (`current_safe`, 30 samples) | 3.17 s | 3.50 s | 3.54 s | over target, same curve |
| `engineering.context.build` (investigate, 10 samples) | 29.3 s | 31.2 s | 31.2 s | over target by ~30× |
| `continuity.checkpoint.append` (100 samples) | 1.10 ms | 1.65 ms | 10.5 ms | inside target |

The scaling curve confirmed the 10k finding: the preview path scored the full admitted candidate set in Python per query (no SQL-side top-k), so search latency grew roughly linearly with corpus size, and pack construction — which hydrated, rendered and checksummed over the same frontier — grew super-linearly past it (2.96 s at 10k → 29.3 s at 100k). The chunked folds answered correctly at every scale, so these were **latency** gaps, not correctness gaps: §20.3's resource-correctness gates held (bounded hydration, no cap disabled, no ACL shortcut). This run identified §11.3's SQL-side top-k / permission-partitioned scoring lane as the production follow-up; the format-2 update below records the later query-narrowing implementation and its current measurements.

**Update (format-2 qualification, 2026-10-03 UTC).** Search and pack build now narrow the record-id space by query in SQLite (identity only, before authorization) and rank only authorised matches. The committed format-2 10k lane passed every advisory warm target (worst search p95 195.774 ms, context p95 251.832 ms, checkpoint p95 20.699 ms). The format-2 100k reference lane completed end to end at source `f3de24f7`: worst search p95 2,505.244 ms and context p95 3,801.514 ms missed their 300 ms and 1,000 ms targets; checkpoint p95 21.636 ms passed. The remaining scan cost is measured and explicit.

These are measurements, not release guarantees. The older tables above remain format-1 history; the committed 10k and 100k reports carry the worktree, ACL, conflict, cache-state, concurrency, environment, source and policy dimensions required by spec §20.2. The OS page cache is not controlled, so no system-cold lane is claimed.

## Honest limitations (carried into the release note)

- Lease expiry and binding generation are enforced at settlement. Cross-principal continuity is available only through an explicit, trusted handoff grant (migration 0064; see the follow-on slice above); it remains outside the supported v1 Personal-mode profile (§19.4), and the local service authenticates no second non-MCP principal, so using it needs that principal and credential decision.
- Applicability is dependency-qualified against recorded source streams. Core has a bounded local polling producer, durable-seal crash recovery, restart-persistent scheduling fairness and indexed pending lookup. Platform filesystem notifications and Dev semantic parser/indexer adapters remain external integration work.
- Context packs emit structural conflict warnings, including `unresolved_overlap`, and governed endpoint checks are enforced.
- Semantic assessment (P2-08) is deliberately not implemented; it waits on the owner gates G-2 (Laya distribution pin) and G-3 (signed-manifest trust anchor).
- The v1 renderer uses a deterministic named-tokenizer count that is not a host-model tokenizer. The negotiated byte-only v2 representation omits token and tokenizer fields, and unsupported exact-tokenizer requests fail closed before storage.
- Whole-file digest selectors are evaluated from the snapshot's file digests, and `symbol` and `source_span` selectors from trusted adapter attestations (migration 0065). Other stored selector shapes fail closed in the v1 profile.
- Performance numbers are lane measurements on the development machine that produced them, not qualified release guarantees (§20.2).

## Owner gates still open

- **G-2** — Laya distribution pin (publication decision).
- **G-3** — signed-manifest trust anchor (key-custody ruling).
- Handoff: `omnivia-core-decision-runtime-gates-g2-g3-resolution-handoff-2026-09-26.md`.
