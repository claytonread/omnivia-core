# Engineering memory: remaining work register

Date: 2026-09-28  
Reconciled: 2026-10-04

Current governing closeout sources are
`engineering-memory-acceptance-evidence-2026-10-03.md`,
`omnivia-core-engineering-memory-release-evidence-2026-09-27.md`, and
`omnivia-core-engineering-memory-status-2026-09-27.md`. The two 29 September
filenames previously named here do not exist in this checkout and are not evidence.

## Current closeout state (2026-10-03)

- AC-001–AC-063 have direct production-path evidence in the acceptance register.
- The preview path narrows candidates by query before authorization/ranking; the
  format-2 10k and 100k qualification reports are complete.
- AC-050 trusted-checkout scope classification is implemented by migration 0062.
- AC-063 portable export/restore excludes installation-local authority and mappings,
  preserves permitted stable history, and proves immediate revoked-evidence blocking.
- AC-064 remains open until full preflight and supported-OS CI complete. The 100k
  report records search p95 2,505.244 ms and context-build p95 3,801.514 ms as
  target misses; checkpoint p95 21.636 ms is inside target. An OS-controlled
  system-cold lane remains explicit release evidence debt.

Sections 1–4 below are retained as the historical 28 September register. Their
individual state labels are not current status; use the acceptance register above.

## Corrections applied 2026-09-29

1. **AC mapping**: this register's §3.1 labelled preview-hydration,
   per-principal authorisation and citation-revocation as an "AC-058/061
   family". The recovered v1.0 specification maps those to AC-033, AC-034 and
   AC-040 respectively; AC-058 covers rename/delete/dependency change and
   AC-061 covers out-of-order/duplicate source events. Codex must confirm
   against the accepted scenario register before relying on either mapping.
2. **Golden fixtures**: `codex/udl-wp02-golden-fixtures` was reported merged
   (PR #140) and was wrongly listed as in-flight; it belongs with delivered
   work.
3. **Release scope**: deterministic engineering-memory P0 is independent of
   Laya publication; PR-4/PR-5/P2-08 remain separately gated on G-2/G-3/G-5.
   G-5 is not defined by any source in this tree and needs a retrieval task.

Scope: the engineering-memory spec (SPEC-CORE-ENGMEM-001, AC-001–AC-064), the
applicability follow-up of 2026-09-26, and the source-coverage slice merged as
PR #129. Work already merged, and work in flight on named branches, is listed
separately so this register states only what remains.

Sources: `engineering-memory-applicability-followup-2026-09-26.md`,
`engineering-memory-applicability-followup-closure-2026-09-28.md`,
`engineering-source-coverage.md` (2026-09-27, on
`codex/engineering-memory-invalidation`),
`omnivia-core-engineering-memory-completion-plan-2026-09-26.md`,
and `omnivia-core-decision-runtime-gates-g2-g3-resolution-handoff-2026-09-26.md`.

## 1. Already delivered (for orientation, not new work)

- Conservative registry-only assessment; evidence-gated `matched`
  (PR #129 base, 2026-09-26 slice).
- Source-coverage vertical: `engineering.source.record`, whole-file SHA-256
  dependency manifests, migrations 0050–0052, qualified `current_safe` read
  (PR #129, 2026-09-27 slice).
- Full clean preflight including the macOS companion gate, in the corrected
  environment, on `codex/engineering-acceptance-edge-cases` @ `88100e53`
  (closure record, 2026-09-28).

## 2. In flight on branches (verify, do not duplicate)

| Branch | Carries |
|---|---|
| `codex/engineering-memory-invalidation` / `-0059` | Bounded source invalidation, migration 0054 invalidation worker, SQLite-gate serialization |
| `codex/engineering-source-producer`, `-durable-queue` | Change-event producer side (PR-E worker half) and durable source-event queue |
| `codex/engineering-conflict-warnings`, `-assessment`, `-pre-0060` | Known-conflict warnings and conflict assessment for context packs |
| `codex/continuity-lifecycle-0060`, `-session-binding`, `-registration-client`, `codex/engineering-continuity-handoff`, `codex/engineering-continuity-expiry` | Continuity lifecycle, binding-generation enforcement, handoff, expiry/refresh |
| `codex/engineering-pack-budget-enforcement`, `codex/engineering-acceptance-edge-cases` | Pack budget gates and acceptance edge cases |
| `codex/engineering-validation-receipts`, `codex/engineering-legacy-import` | Validation receipts; legacy Engineering Memory import (AC-032) |

Sequencing rule: rebase each on current `origin/main` and run
`PATH="$PWD/.venv/bin:$PATH" ./scripts/preflight` before opening its PR.

## 3. Outstanding implementation work

### 3.1 Applicability qualification (from the 2026-09-26 acceptance scope)

| Item | Spec | State |
|---|---|---|
| Full `current_safe` context and event-barrier qualification (atomic source-event coverage barriers, ordered invalidation recovery) | AC-057 | Partial: invalidation worker on `…-invalidation`; barrier + ordered recovery not merged |
| Target-specific qualification: full version, branch, dirty-tree and revert handling | AC-059 | Not started; only target/record separation exists |
| Temporal resolver qualification (dependency coverage over time) | AC-060 | Partial: coverage exists at a recorded snapshot; no temporal resolution |
| Per-principal authorization before ranking; preview retrieval without body hydration; revocation on citation follow-up | AC-058/061 family | Not started |

### 3.2 Selector and lineage gaps (source-coverage "Deferred and unsupported")

- Non-digest selectors: `symbol`, `config_key`, `source_span`,
  `schema_contract`, `external_evidence` — recorded, never evaluated.
- Renames: no rename field; a renamed required file reads as absent.
- Transitions that do not carry a sealed set (`record.supersede`,
  `candidate.reject`, content-changing transitions) mint versions that stay
  `unknown`; no path records a set for them.
- No lineage reasoning: no cross-stream equivalence, ancestry or merge-base.
- `current_safe` search omissions are not counted (contract has no field).
- Continuity is same-principal only; no sharing grant.

### 3.3 Consumer and release-gate work (completion plan Phase 1)

- CLI vertical proof over the installed entry point against managed start
  (partial: `test_engineering_memory_cli` covers the sequence; expand to
  release evidence).
- Migration/restore evidence: backup with engineering tables populated,
  restore into a fresh installation, digest assertions.
- Performance qualification lanes (§20.2): synthetic corpora at 10k/100k
  observations; p95 preview search, p95 4k-token pack build, p95 checkpoint
  commit; hardware/corpus recorded. Targets stay labelled targets until
  measured. The synthetic-corpus builder is its own deliverable.
- §22.5 release evidence manifest: per-capability producer/consumer map,
  supported OS combinations, known exclusions.
- Consolidated PR-A…PR-H closeout status doc.

### 3.4 Engineering debt found during the 2026-09-28 closure

- Separate `assert_guards_intact` from `verify_fingerprint` in
  `runner.py`'s readiness `try`: a guard failure currently reports as
  `exact_schema_and_trigger_fingerprint`, which cost a root-cause session.
- Pin the subprocess `PATH` (or prefer the beside-`sys.executable` script) in
  CLI-spawning test helpers, so a foreign installed `omnivia-core-service`
  cannot silently serve a different schema.
- The IPC 2-second worst-case frame deadline test is a rare contention flake;
  adopt rerun-once-before-investigating, as for the hosted journey flake.

### 3.5 Decision-runtime prerequisites (different spec, same release path)

- PR-4 model catalogue / installation / lifecycle — blocked on G-2 + G-3.
- PR-5 worker packaging + Laya adapter — blocked on G-2 (+G-5).
- Optional inert pre-build: model-manifest verification on the existing
  runtime-payload trust format with a dev/test anchor behind a flag.

## 4. Not agent-reachable

See the decisions handoff (`engineering-memory-decisions-handoff-2026-09-28.md`):
the Laya distribution publication (G-2), the Ed25519 trust-anchor rulings
(G-3a–d), and the repository-registration ratification (§16.3) are owner or
cross-repo items.
