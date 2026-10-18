# OmniVia Core: Engineering Memory implementation plan

| Field | Value |
|---|---|
| Date | 2026-09-26 |
| Implements | `OmniVia-Core-Engineering-Memory-Technical-Spec-v1.0-2026-09-25.md` (SPEC-CORE-ENGMEM-001, candidate) |
| Baseline verified against | `~/Projects/omnivia-core` @ `bfc91b26` (branch `codex/decision-runtime-templates`; decision-runtime PR-3/7 merged, hardening in flight) |
| Status | Plan draft for owner review. Per AGENTS.md, Codex (PM) must ratify package boundaries, operation names and migration numbers before any implementation branch starts. |

## 1. Baseline reconciliation (spec §22.1 facts, verified 2026-09-26)

- Catalogue: 43 operations. Present: `memory.create/get/list/read/search/write`, `knowledge.propose/govern/read/search`, `graph.read/traverse`, `context_pack.build`, `evidence.capture/read/search/write`. **Absent:** all `engineering.*`, `continuity.*`, `context.priority.*` operations — they are new catalogue amendments.
- `context-pack.schema.json` recognises only `deterministic_view`; `immutable_snapshot` is deliberately refused. Matches spec [C2]. No change to v1.
- Migration head: **0046** (`decision_records`). Engineering-memory migrations start at 0047+. Do not reuse V06-3 numbering.
- Existing reusable modules: `storage/memory.py`, `storage/governed.py`, `storage/governance.py`, `storage/evidence` paths via `evidence.*` handlers, `storage/projections/` + projection ledger, `storage/jobs.py` (durable jobs/outbox), `service/pagination.py`, `service/authorization.py` + capability gateway, `service/mutation.py` (idempotency/preconditions), `storage/connectors.py` (source registration).
- Source registration for repositories should extend the existing SourceConnector flow (`storage/connectors.py`); confirm exact operation names in P0-00 before code.
- Concurrent work: decision-runtime hardening is uncommitted on this branch. Engineering-memory work must **not** branch until that lands or is parked; keep catalogue/migration changes serial under one integration owner (spec §24.2).

## 2. Work packages and sequence

Mirrors spec §24.1 with concrete repo touchpoints. P0 is one releasable slice; P1/P2 follow.

### P0-00 — Baseline and contract reconciliation
- Produce the reuse/extend/new mapping table for every proposed operation and field (below is the starting draft).
- Ratify through the established catalogue-change process: 10 proposed operations (§16.3), engineering content profile `engineering.observation` v1.0, checkpoint profile `EngineeringCheckpointV1`, pack format `engineering_context.v1`.
- Allocate migration numbers 0047+ and ADR identifiers from the live registry.
- Register the content profile in the existing semantic/schema registry.
- **Deliverable:** accepted mapping table + catalogue amendment + conformance fixtures. No handlers.

### P0-01 — Evidence and source identity
- Extend `storage/connectors.py`-backed registration with repository/checkout/worktree identity hierarchy (§6.1), resolution order and fail-closed ambiguity (`repository_ambiguous`).
- `RepositorySnapshotRef` + snapshot capture (git_commit / working_tree / source_archive), manifest digest, dirty-tree fidelity, bounded capture retry, `snapshot_incomplete`.
- Portable source locations: repository-relative paths, source spans, case/Unicode preservation, containment checks against a trusted registered checkout (no traversal, symlink, TOCTOU).
- Migrations: repository bindings, checkout/worktree installation-local mapping, snapshot manifest tables (mapped to existing source/evidence identity tables from 0040/0041 first).
- Evidence-dependency manifest storage (selector type, meaning) with reverse index projection.
- **Acceptance:** AC-009…AC-016.

### P0-02 — Contributions and continuity
- `EngineeringObservationContentV1` profile + validator, submitted through **existing** `memory.create` (proposed-only; no authority fields anywhere in input). Payload cap 64 KiB typed error.
- Optional `engineering.observation.revise` — only if no compatible existing revision/governance proposal operation (verify in P0-00).
- `continuity.session.register/close` (trusted adapter/SDK only, not model-facing), binding generation + lease checks against workspace writer generation.
- `continuity.checkpoint.append`: idempotency key, expected predecessor sequence, transactional metadata commit after durable blob staging, 256 KiB cap, durable receipt, competing-successor precondition failure.
- **Acceptance:** AC-005, AC-006, AC-017…AC-024, AC-026…AC-032.

### P0-03 — Progressive retrieval (parallelisable with P0-02 after P0-01)
- Projection: engineering preview (no body hydration), topic membership, evidence-dependency reverse index — inside the existing projection ledger with watermark/version/staged activation.
- `engineering.search` / `engineering.expand`: preview ≤480 cp / 2 KiB per item, 64 KiB response, default 20 / max 100; views `accepted` (default), `candidates`, `working_context`, `history` behind grants.
- Authorised frontier (`AuthorizedEngineeringFrontier`) computed before any scoring; permission-partitioned lexical scoring — audit FTS BM25 corpus-statistics leakage (AC-035) and use the qualified scorer approach.
- MAC'd opaque continuation tokens bound to query/scope/epoch (extend `service/pagination.py`).
- **Acceptance:** AC-033…AC-040.

### P0-04 — Applicability barrier
- `RepositoryChangeSet` ingestion, coverage barrier + watermark, invalidation worker joining changed source identities against dependency manifests, idempotent/out-of-order-safe.
- Applicability dimensions (governance / target / review schedule / evidence access / projection coverage) as separate attestation + serving projection; classification rules per §15.4 including dirty-worktree and ancestry conservatism.
- `applicability_pending` fail-closed for `current_safe` reads.
- **Acceptance:** AC-057…AC-062 (freshness subset).

### P0-05 — Engineering Context Pack
- New operation `engineering.context.build` + `EngineeringContextPackV1` (`format_version = "engineering_context.v1"`). Shares the existing builder/frontier/canonicalisation/tokeniser engine with `context_pack.build`; non-persisting read.
- Canonical JSON (RFC 8785) checksum: SHA-256 after removing root `pack_id` and nested `reproducibility.artifact_checksum`; pack_id == artifact_checksum; validator checks unique IDs, citation resolution, partitions, budget reconciliation, fail-closed capabilities.
- Budget contract (§12.4): simultaneous token + byte caps, reserved mandatory notice/citation overhead, no mid-codepoint truncation, `context_budget_insufficient`, `tokenizer_unavailable`.
- Deterministic builder: pinned BuildContext, no clock/ambient branch reads, bounded selection iterations, revocation re-check before release.
- **Acceptance:** AC-003 (v1 regression), AC-041…AC-048.

### P1-06 — Reconciliation and review
- Structural + bounded lexical conflict discovery (8→32 candidate budget), relation candidate vocabulary/status, governed resolution through the existing review service, supersession cycle rejection, stale-endpoint preconditions.
- `context.priority.set` (principal-scoped, capped 10% boost, never authority).
- `engineering.review.record` (attestation; cannot accept knowledge or clear `potentially_stale` without evidence).
- Semantic assessment stays **disabled**; adapter ships only in P2-08.
- **Acceptance:** AC-049…AC-056.

### P0-07 — Consumer and release qualification
- MCP exposure (`engineering_search`, `engineering_expand`, `engineering_context_build`, `continuity_checkpoint_append`, `continuity_handoff_read`) in `omnivia-core-mcp`, generated from the catalogue; Read/Contribute/Curate profiles; hidden tools also rejected service-side.
- CLI vertical in `omnivia-core-cli` proven through the installed entry point against a managed-start service (pattern from decision PR-7).
- Migration/restore, OS matrix (ubuntu/macos/windows — existing CI rule names), performance qualification lanes (10k/100k fixtures; targets §20.2), release evidence manifest per §22.5.
- **Acceptance:** AC-001…AC-008, AC-063, AC-064 + full matrix re-run.

### P2-08 — Optional assessor
- Only after provider/security approval: bounded-pair egress, strict verdict schema, no governance-write authority, budget/cancel tests. AC-051, AC-052 remain gates for the disabled-by-default refusals.

## 3. Operation mapping draft (starting point for P0-00)

| Proposed | Disposition |
|---|---|
| `memory.create` w/ `engineering.observation` profile | **Reuse** (no new op) |
| `memory.get/list/search`, `knowledge.search`, `graph.traverse`, `context_pack.build` | **Reuse unchanged** |
| `continuity.session.register/close`, `continuity.checkpoint.append`, `continuity.handoff.read` | **New** |
| `engineering.search/expand/context.build`, `engineering.observation.revise` | **New** (revise only if no compatible governance op) |
| `context.priority.set`, `engineering.review.record` | **New** |
| Source/repository registration | **Extend** SourceConnector flow; catalogue amendment if no suitable op |

## 4. Risks and open decisions

1. **Branch conflict with decision-runtime hardening** — uncommitted changes in `service/decision_runtime.py`; sequence work behind that merge.
2. **ACL-before-scoring (AC-034/035)** is the highest-uncertainty engineering item: FTS BM25 global statistics leak. Requires a permission-partitioned index or post-frontier lexical scoring; prototype and qualify before P0-03 finalisation.
3. **Deterministic pack checksum discipline** (self-reference exclusion, RFC 8785) — needs independent-oracle fixtures from day one, not retrofitted.
4. **Multi-principal honesty (§19.4):** ship labelled as Personal/single-principal unless distinct-principal revocation tests pass. Release note must say so.
5. **Performance targets are unmeasured** — treat §20.2 as qualification gates, not commitments; publish only with workload/config.
6. **Per repo AGENTS.md:** Claude builds, Codex manages; PRs only, preflight clean, four exact-named checks green on the merge commit. Spec's 64 AC scenarios should be mapped into the existing phase/conformance test structure rather than a parallel suite.

## 5. Suggested PR slicing

1. PR-A: P0-00 mapping doc + catalogue amendment + ADR + profiles/contracts + wire fixtures (no handlers).
2. PR-B: P0-01 repository identity/snapshots + migrations 0047–0049.
3. PR-C: P0-02 session binding + checkpoints (storage + handlers + MCP/CLI contribute profile).
4. PR-D: P0-03 preview projection + search/expand + frontier.
5. PR-E: P0-04 applicability barrier + invalidation worker.
6. PR-F: P0-05 engineering context pack builder + budgets + checksum validation.
7. PR-G: P1-06 reconciliation + priority + review.
8. PR-H: P0-07 qualification, migration/restore, release manifest.

## 6. PR-A status (2026-09-26, branch `codex/core-engineering-memory-contracts`)

Implemented on a worktree off `main` @ `6f812fbd`; the decision-lane dirty work in the primary checkout was not touched.

- **Catalogue amendment: 43 → 52 operations.** Nine new operations registered in `x-omnivia-operation-catalogue` and the frozen `FROZEN_OPERATIONS` table: `continuity.session.register`, `continuity.checkpoint.append`, `continuity.session.close`, `continuity.handoff.read`, `engineering.search` (paginated), `engineering.expand`, `engineering.context.build`, `context.priority.set`, `engineering.review.record`.
- **`engineering.observation.revise` deliberately NOT added**: the catalogue already owns `record.supersede` (exact-version supersession with preconditions), so the plan's own reuse-before-add rule applies. Observation revision goes through the existing governed-record operations.
- New source schema `contracts/application/v1/schemas/engineering.schema.json` (47 definitions). Digests reuse the published `jobs.ContentChecksum`; spans reuse `records.SourceSpan`/`SourceReference`; pagination reuses `common.PageMetadata`. No new patterned scalar — the pattern-baseline gate ("new published pattern needs an accepted contract decision") is not triggered.
- Registry mirror regenerated (357 defs), `x-omnivia-schema-sources` + `SOURCE_SCHEMAS` extended with `engineering`, generated Python/TS artifacts regenerated, oracle fixture regenerated from the freeze.
- Wire conformance corpus: 18 new cases (9 primary success, 5 mutations × replay/conflict, search two-page) → 141 total; byte-identity pin, corpus/mutation/pagination counts updated.
- Runtime: `handlers/engineering.py` (nine honest `dependency_unavailable` refusals — contracts first, producers in later packages), ninth authority family (`engineering_family_session`, `build_engineering_registry`, `build_engineering_application_dispatcher`), purposes/roles in `mutation.py`, dispatcher wiring in `main.py` (`compose_production_application_surface` now nine families).
- Error profiles: `_ENG_CONTINUITY_MUT` (create-mut + not_found + size_limit_exceeded), `_ENG_PRIORITY_MUT`; reads reuse POINT_READ / GRAPH_READ / CONTEXT_READ; close + review reuse GOV_MUT with the frozen precondition set extended to `continuity.session.close` and `engineering.review.record`.
- Migrations 0047–0049 **reserved** in `contracts/migrations/v1/allocations.json` (Engineering Memory; no files yet).
- Local evidence: `check-application-contracts.py` passed; migration allocation gate passed; runtime Phase 2 589 passed. Contract suite and remaining suites: see status doc.

### Next (PR-B onward)
Migrations 0047–0049 + repository/worktree/snapshot identity (`storage/`), then continuity persistence (P0-02) replacing the register/append/close refusals.
