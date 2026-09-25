# PR-A status: Engineering Memory contracts (`codex/core-engineering-memory-contracts`)

Date: 2026-09-26
Branch: `codex/core-engineering-memory-contracts` (worktree `~/Projects/worktree-omnivia-core-engmem`, not yet pushed)
Base: `main` at `6f812fbd` (PR #126). The decision-templates hardening lane in the primary checkout was not touched.
Implements: plan PR-A of `docs/development/omnivia-core-engineering-memory-implementation-plan-2026-09-26.md` (SPEC-CORE-ENGMEM-001 §24.1 P0-00).

---

## 1. What this PR changes

**Catalogue amendment 43 → 52 operations.** Nine new operations, appended in amendment order after the decision family:

| Operation | Posture |
|---|---|
| `continuity.session.register` | mutation, `engineering.write`, idempotency required |
| `continuity.checkpoint.append` | mutation, `engineering.write`, idempotency required |
| `continuity.session.close` | mutation, `engineering.write`, idempotency + mutation precondition |
| `continuity.handoff.read` | read, `engineering.read` |
| `engineering.search` | read, `engineering.read`, paginated (max_page_size 1000) |
| `engineering.expand` | read, `engineering.read` |
| `engineering.context.build` | read, `engineering.read` |
| `context.priority.set` | mutation, `engineering.write`, idempotency required |
| `engineering.review.record` | mutation, `engineering.curate`, idempotency + mutation precondition |

`engineering.observation.revise` is **deliberately not added**: the catalogue already owns `record.supersede` (exact-version supersession with preconditions), and the plan's reuse-before-add rule applies. Observation revision goes through the existing governed-record operations. `memory.create` with record type `engineering.observation` / domain scope `engineering.codebase` needs no contract change (both vocabularies are wire-open) — that is P0-02 work, not this PR.

## 2. Contract layer

- New source schema `contracts/application/v1/schemas/engineering.schema.json` — 47 definitions. Reuse before adding: `jobs.ContentChecksum` (digests), `records.SourceSpan`/`SourceReference` (anchors/sources), `common.Identifier`/`Timestamp`/`PageMetadata`/`JsonObject`. **No new patterned scalar**, so the pattern-baseline major/manual-review gate is not triggered.
- `x-omnivia-operation-catalogue` regenerated from the extended freeze in `scripts/check-application-contracts.py`; `SOURCE_SCHEMAS` extended with `engineering` in both the checker and the generator; registry mirror regenerated (357 defs, `x-omnivia-schema-sources` + engineering URI); Python/TypeScript artifacts regenerated; oracle fixture (`tests/contracts/fixtures/operation-catalogue-v1.json`) regenerated from the freeze.
- Error profiles: new `_ENG_CONTINUITY_MUT` (create-mut + `not_found` + `size_limit_exceeded`) and `_ENG_PRIORITY_MUT`; reads reuse POINT_READ/GRAPH_READ/CONTEXT_READ; `continuity.session.close` and `engineering.review.record` reuse GOV_MUT via `FROZEN_PRECONDITION_OPERATIONS`.
- Wire conformance corpus: **121 → 141 cases** (20 new: 5 mutations × {primary-success, honest-replay, idempotency-conflict}, 4 read primaries, 1 search page-2; close and review request cases carry the required `mutation_precondition` metadata). Byte-identity pin, corpus count, mutation count 21→26, paginated count 8→9 updated in `test_adapter_conformance.py`.

## 3. Runtime layer

- `service/handlers/engineering.py`: nine handlers, each the contracts-first honest refusal (`dependency_unavailable`, frozen message constants). The durable producers land in later plan packages; §28.4 makes an intentionally unavailable state correct until then.
- Ninth authority family: `ENGINEERING_FAMILY_OPERATIONS`/purposes, `engineering_family_session` (contributor + reviewer roles), `build_engineering_registry`, `build_engineering_application_dispatcher`; `compose_production_application_surface` now nine families and refuses to start otherwise.
- `service/mutation.py`: purposes `continuity_session`, `continuity_checkpoint`, `context_priority`, `engineering_review`; roles contributor×4, `knowledge_reviewer` for `engineering.review.record` (recording an attestation accepts no knowledge).
- `service/main.py`: engineering dispatcher composed into the production surface between decision and probe.

## 4. Consumer surfaces

- CLI: nine generic application commands (`continuity register/checkpoint/close/handoff`, `engineering search/expand/context/review`, `context priority`) — the surface validator's catalogue bijection requires them; dispatch is the existing generic `--input-json` path.
- MCP: exposure unchanged (10 restricted / 15 authoring tools); the traceability ledger's `omitted` list regenerated with derived reasons (42 entries).
- README catalogue listing updated to 52.
- Architecture-gate fixture: the five derived gates (whole-catalogue ×3, mutations ×2) regenerated from the same derivation the test uses.

## 5. Migration allocation

`contracts/migrations/v1/allocations.json`: **0047–0049 reserved** (Engineering Memory; `repository_identity`, `continuity`, `applicability`). No SQL files yet — reservation only, so PR-B cannot collide.

## 6. Local verification (this worktree, all commands actually run)

| Gate | Result |
|---|---|
| `scripts/check-application-contracts.py` | **passed** |
| `scripts/check-migration-allocations.py` | **passed** |
| `tests/contracts` + `tests/compatibility` | **11,346 passed**, 19 skipped |
| `packages/omnivia-core-runtime/tests` (full) | see §7 — Phase 2 589 passed mid-implementation; full rerun recorded at commit time |
| `packages/omnivia-core-cli/tests` | 835 passed (3 `test_lifecycle.py` failures are pre-existing: they fail identically on the untouched primary checkout — the documented managed-start socket flake) |
| `packages/omnivia-core-mcp/tests` | 1,840 passed (exposure manifest, import-job and standalone-authoring acceptance reruns green) |
| `tests/` remaining (incl. `service_conformance`, `test_migration_allocations`, architecture gate) | **3,315 passed** total across sweeps |
| `ruff check .` | **clean** |
| `mypy src/omnivia_core` / `mypy packages/omnivia-core-runtime/src` | **clean** (148 / 125 files) |
| `git diff --check` | clean |

## 7. Remaining before merge

1. Full runtime suite rerun on the final tree (in flight when this doc was written; Phase 2 and the earlier full run surfaced only the CLI-surface import error, since fixed).
2. `./scripts/preflight` (needs `OMNIVIA_ACCEPTED_CONTRACT_CHECKPOINT` from repository configuration, as CI reads it).
3. Commit, push, open PR; hosted checks (`Core acceptance` + three platform jobs) must be green on the merge commit before merge (AGENTS.md).

## 8. Deliberate scope boundaries of this PR

- No handlers with durable effects: every engineering operation answers the honest refusal. Nothing in the release notes may claim engineering persistence from this PR.
- No MCP exposure change, no semantic registry ontology publication, no engineering content profile in the registry yet (P0-00/P0-02 items that need the owner's ratification of the mapping table).
- No ADR number allocated — per the spec's closeout rule, no ADR identifiers are invented; the catalogue amendment's rationale lives in this document and the plan doc until the owner allocates one.
