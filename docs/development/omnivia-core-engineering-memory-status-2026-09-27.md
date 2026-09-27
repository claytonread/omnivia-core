# OmniVia Core — Engineering Memory: consolidated implementation status

**Date:** 2026-09-27 · **Spec:** `SPEC-CORE-ENGMEM-001` v1.0 (2026-09-25) · **Plan:** `omnivia-core-engineering-memory-implementation-plan-2026-09-26.md`

## Delivery sequence (all merged to `main`)

| PR | Scope | Spec packages |
|---|---|---|
| #127 | Contracts: 9-engineering-operation catalogue slice, `engineering.schema.json`, wire corpus, migration allocation pins; continuity handlers | P0-00, P0-02 (contracts + continuity) |
| #128 | Repository identity (§6), observation ingestion, search/expand with frontier-frozen ranking, applicability families, priority + review, pack builder | P0-01, P0-03, P0-04, P0-05 |
| #129 | Follow-up correctness work absorbed from review | — |
| #131 | MCP exposure: engineering reads in the restricted manifest, checkpoint append in authoring; installed profiles + traceability | P0-07 (consumer half) |
| #132 | Context budget contract work | P0-05 |
| #133 | Pack correctness conformance | P0-05 |
| PR-H2 (this change) | CLI vertical proof, migration/restore evidence, performance qualification lanes, release evidence manifest | P0-07 (closeout) |

## What exists on `main` today

- **Ten engineering operations** in the frozen catalogue: `continuity.session.register`, `continuity.checkpoint.append`, `continuity.session.close`, `continuity.handoff.read`, `engineering.search`, `engineering.expand`, `engineering.context.build`, `context.priority.set`, `engineering.review.record`, `engineering.source.record`.
- **Seven migrations** (0047–0053): repository identity, continuity, applicability, source coverage, dependency carry, dependency lookup, preview projection.
- **MCP exposure** through the curated manifest (restricted + authoring profiles), generated exposure schemas, traceability ledger.
- **CLI surface**: ten generic application commands over `omnivia-core-client`, proven end-to-end by the new vertical test against a managed-start service.
- **199 engineering tests** across 12 runtime test files, plus MCP stdio e2e + architecture gates, plus the env-gated qualification lane.

## PR-H2 evidence (this change)

1. **CLI vertical** (`test_engineering_cli.py`): the spec §1.1 initial slice — register → append (fenced) → close-with-final-checkpoint → handoff → working-context search → resume pack — through the installed `omnivia` entry point against a real managed-start service; plus the AC-023 stale-predecessor typed refusal.
2. **Migration/restore** (`test_engineering_restore.py`): verified backup → restore → row-identical engineering tables (checkpoints by digest+sequence, sessions, snapshots) and the observation still readable at its exact version through the production surface (AC-063 storage half).
3. **Qualification lanes** (`test_engineering_qualification.py`, env-gated): 10k and 100k-observation synthetic corpora; p50/p95/p99 for `engineering.search`, `engineering.context.build`, `continuity.checkpoint.append`; reports written to `benchmarks/reports/engineering-memory/lane-<n>.json`.
4. **Release evidence manifest**: `omnivia-core-engineering-memory-release-evidence-2026-09-27.md`.

## Honest limitations (carried into the release note)

- Lease expiry and binding-generation fencing are recorded but not enforced (§7.3).
- Single-principal Personal mode only; no validated organisational isolation (§19.4).
- Applicability is dependency-qualified against recorded source streams; there is no external change-event producer feeding `RepositoryChangeSet` automatically — targets are matched/pending per the recorded coverage, and a real indexer integration remains future work.
- Semantic assessment (P2-08) is deliberately not implemented; it waits on the owner gates G-2 (Laya distribution pin) and G-3 (signed-manifest trust anchor).
- Performance numbers are lane measurements on the development machine that produced them, not qualified release guarantees (§20.2).

## Owner gates still open

- **G-2** — Laya distribution pin (publication decision).
- **G-3** — signed-manifest trust anchor (key-custody ruling).
- Handoff: `omnivia-core-decision-runtime-gates-g2-g3-resolution-handoff-2026-09-26.md`.
