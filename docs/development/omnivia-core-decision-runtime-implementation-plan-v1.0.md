# Implementation plan — SPEC-CORE-DEC-001 Decision Runtime / Local Decisions

Version 1.0 · 22 September 2026 · Bound to `omnivia-core` main `8fb284f0` (post PR #117)
Owner: Clayton Read · Implementation model: Codex PM / Claude build, one worktree per task packet

This plan turns §27 of the specification into a repository-bound, PR-sliced execution order. Every file path below was verified against the current checkout today. The specification remains the normative contract; this document only sequences and binds it.

---

## 0. Binding summary (verified today, not assumed)

| Spec seam | Confirmed current location | Notes |
|---|---|---|
| Operation catalogue (frozen, 28 v1 ops) | `contracts/application/v1/schemas/operations.schema.json` → `x-omnivia-operation-catalogue` | Single source; Python + TS catalogues are generated. Amendment = edit this file + regenerate. |
| Catalogue registry / derivation | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/operations.py` | `OPERATION_CATALOGUE` import; `APPLICATION_OPERATIONS` frozenset; runtime probes deliberately separate |
| Service composition | `service/main.py` | Compose the optional decision service here; `admission.py`, `authorization.py`, `capability_gateway.py` already exist for the authorisation path |
| Handlers | `service/handlers/` | Decision handlers follow the existing handler pattern |
| Workspace migrations | `storage/migrations.py` + `storage/migration_files/` | Latest is `0042_runtime_stop_progress.sql`; Decision migrations start at **0043** |
| Installation-owned state | `storage/installation_migrations.py` + `storage/installation_store.py` | Model installation state belongs here — matches §13 exactly; no new catalogue |
| Durable jobs | `storage/jobs.py` + `service/jobs.py` | Reuse for evaluation jobs; do not add a competing API |
| Outbox/events | `storage/semantic_events.py` pattern | Decision outbox follows the same transactional pattern (§14.5) |
| Governance/candidates | `storage/governance.py`, `storage/governed.py` | Candidate-classification path for derived annotations (§14.3) |
| Contracts Python | `src/omnivia_core/contracts/v1/generated.py` (+ semantics modules) | Decision DTOs/errors as a new semantics module + regenerated types |
| CLI / MCP / client | `packages/omnivia-core-cli`, `omnivia-core-mcp` (`server.py`, `manifest.py`), `omnivia-core-client` | Thin transports only (D-02) |
| Settings | `apps/core-status-menu-macos/` `WebSettingsWindowController` + `SettingsBridge` | Extend Processing pane via the existing bridge allowlist (C3/C4, D-10) |

**Migration numbering: next free workspace migration is 0043.** Installation-state migrations number separately inside `installation_migrations.py`.

---

## 1. Decision gates before any code (owner/ADR, from §27.1)

These block T-01 onward. Nothing else in the plan can start the public-contract work until they close; isolated experimental work (worker spike, preparer) may proceed in parallel.

| Gate | Decision needed | Decision owner |
|---|---|---|
| G-1 | **Catalogue amendment**: approve the 12 proposed `decision.*` operations, payload schema `decision.1`, purposes, grants (`decision:invoke/read/configure/feedback/experimental`) and error posture into the frozen catalogue | Owner via ADR (ADR-042 proposal) |
| G-2 | **Laya dependency source**: pin upstream commit `4619e048` and build an approved internal distribution, or verify a public 0.1.1 wheel matches (spec flags this is unverified) | Owner + packaging |
| G-3 | **Model manifest signing**: the signed model manifest needs a signing mechanism — this touches the release-owned Ed25519 trust-anchor material | Release owner |
| G-4 | **Model licence BOM**: complete redistribution review for model weights, tokeniser and dependency licences (Apache-2.0 code alone is insufficient) | Owner |
| G-5 | **Worker sandbox mechanism**: confirm the qualified production sandbox/packaging mechanism that can demonstrably deny worker network access and contain the filesystem (macOS-specific; Windows/Linux hosts report unavailable regardless) | Owner + packaging |

---

## 2. PR slicing (each PR = one reviewable, preflight-passing increment)

### PR-1 — Binding report + catalogue amendment proposal (T-00)
Docs + ADR only. No behaviour change, so it is safe to land while other gates are open.
- `docs/adr/042-decision-runtime-catalogue-extension.md` (proposed): the 12 operations, `decision.1` payload, grants, error taxonomy mapping (§23 → canonical error catalogue).
- `docs/development/omnivia-core-decision-runtime-binding-report.md`: this plan's §0 table plus the §5.1 binding confirmations and the LOCAL-IPC-PEER-IDENTITY-DEFERRED carry-forward.
- Records G-1…G-5 as explicitly open.
- **Exit:** ADR accepted by owner (G-1); G-2/G-3/G-4 owners named with target dates.

### PR-2 — Contracts: DTOs, errors, grants, generation (T-01)
- `operations.schema.json`: append the approved catalogue entries (only after G-1).
- Regenerate Python + TypeScript catalogues via `scripts/generate-application-contracts.py`; update `baseline/inventories/public-exports.json`.
- `src/omnivia_core/contracts/v1/semantics_decisions.py`: `DecisionDefinition`, `DecisionPolicy`, typed payload/record/quality/disposition DTOs, reason-code enum (§23), confidence-field separation (§11.2), abstention codes.
- Grants: `decision:invoke/read/configure/feedback/experimental` in the capability model.
- Tests: catalogue conformance regeneration, older-client compatibility (AT-05/06), grant refusals (AT-08/09).
- **Exit:** every new operation statically present in the generated catalogue; existing 28 operations untouched; typecheck/mypy/preflight green.

### PR-3 — Durable records, admission, deterministic route (T-02, T-03)
- Migrations **0043–0045**: `decision_definition_versions`, `decision_evaluations`, `decision_attempts`, `decision_results`, `decision_outcomes`, `decision_qualifications`, `decision_subscriptions` per §24 (unique idempotency key `(workspace_id, principal_id, operation, idempotency_key)`; append-only outcomes; immutable digests).
- `service/handlers/decisions.py` + a `decision_runtime/` module beside it: admission (§8 steps 1–6), idempotency + conflict (AT-42/43), deterministic-route resolution, fail-closed unavailable-provider path (D-09, AT-36), policy composition with most-restrictive-wins (§7.2), cancellation/fencing rechecks (§8 step 7, AT-44/45).
- Outbox events via the `semantic_events.py` transactional pattern: `decision.completed/abstained/failed/outcome_recorded` (§14.5).
- Source resolution + minimisation against the caller's grant (AT-10/11), context-recipe v1 (purpose-bound snapshot per D4).
- **Exit:** `decision.evaluate` with a deterministic provider returns a complete durable record end-to-end via CLI; abstention/failure/cancellation paths terminalise correctly (AT-46–48); no Laya dependency anywhere.

### PR-4 — Model catalogue, installation, lifecycle (T-04)
- `storage/installation_migrations.py`: model-installation state machine (§13.4 five dimensions).
- `service/decisions_model_manager.py` (or handler-local): signed-manifest verification (reuse the release Ed25519 mechanism from PR #72/c07c-signing — needs G-3), staging → verify → atomic activation, removal with drain, rollback retention (§13.2/13.5).
- Path containment: `<installation_root>/models/decisions/<profile>/<revision>/`; traversal/symlink/executable rejection (AT-29); digest verification of *all* files including host tensors (AT-27/28); one serialised installation authority via `installation_store.py`.
- `decision.model.*` handlers + `decision.settings.get/update` with compare-and-swap.
- **Exit:** install → verify → activate → remove lifecycle works with a fixture manifest on macOS; AT-27–34.

### PR-5 — Worker packaging + Laya adapter + loss-aware preparer (T-05, T-06) — macOS-only optional extra
- **Isolated locked runtime**: pinned Laya distribution per G-2, Python 3.12 managed runtime, packaged outside the default cross-platform install (D-02). Phase 0 packaging spike proves `pip`-free, checkout-free launch.
- `decision_provider/` worker: `DecisionProvider` protocol (§10.1), framed-JSON protocol over inherited pipes (§10.3), handshake with adapter build digest + verified manifest digest, one model loaded, one forward pass.
- **Diagnostic preparer** (§9.3): tokenises the *untruncated* instruction/options/state against the pinned tokeniser, emits `PreparedDecisionReport` (§9.2), parity test against upstream `agent.prepare()` for lossless inputs (AT-13/14), loss detection for prefix/option/state overflow (AT-15–17).
- Output normalisation per §11.1: rounding tolerance `K × 0.00005 + 0.000001`, kind-specific confidence semantics preserved (AT-20–22, 24–26).
- Compute metadata: `configured_compute_units` separate from observed evidence (§10.5, AT-31).
- **Exit:** offline real-Mac inference on the general FP16 profile through the worker with durable records; worker network-denied (AT-35/37 — needs G-5 sandbox evidence); general profile only; fast FP16 deferred until templates fit losslessly.

### PR-6 — Supervision, limits, circuit, recovery (T-07)
- Watchdogs: 120 s init, per-attempt deadline (§15.2), hard-timeout worker termination + late-result discard (AT-39/45).
- Limits: queue 100, rate ceiling 5 fps, daily 10k, 64 KiB payload, 256 KiB resolved text, memory ceiling, idle unload 10 min — all as configured backpressure, never silently disabled.
- Circuit breaker (3 failures/5 min → 60 s pause), crash recovery, restart reconciliation of durable jobs (AT-47), sleep/wake revalidation.
- **Exit:** fault-injection suite passes; Core remains responsive and healthy under worker crash/hang/pressure (AT-39/41); no orphan evaluations.

### PR-7 — CLI, SDK, curated MCP (T-08)
- CLI: `omnivia decisions status | models list/install/activate/remove | evaluate | inspect | disable` (§17.1), machine-readable JSON, existing error classes.
- SDK: `decide.boolean/choice/ordinal` typed wrappers over the service operation.
- MCP: curated-catalogue tools for evaluate + record/status inspection only; side-effect description in tool text; no management tools by default (§17.3, AT-07/08).
- **Exit:** same definition/input through CLI, SDK and MCP produces identical validation, grants, records and semantics (AT-07).

### PR-8 — Core Settings → Processing: Local Decisions (T-09)
- Host-mode page: new Local Decisions section in the Processing pane of the shared bundle — status card (§16.2 truthful labels), install/remove/activate actions, advisory enable toggle, subscription pause, qualification summary.
- `SettingsBridge`: extend the frozen allowlist with the typed management actions (`decision.model.install` etc. as native actions with opaque profile IDs), generation/host/workspace fencing (AT-58), bridge origin check (AT-57).
- Passive-only on open/refresh (AT-55); native revalidation of actor/authority before each management call — the safe-status surface is *not* administrative authority, so management calls go through the authenticated Core management API (the §27.1 required binding).
- macOS arm64-only availability of install actions; other hosts show the truthful unsupported label (AT-03).
- **Exit:** install → activate → evaluate → inspect → disable vertical slice fully from the settings window (AT-56, AT-60, AT-72); no passive-read side effects.

### PR-9 — Templates, subscription, qualification tooling (T-10)
- The three built-in templates (`core.document_category`, `core.message_intent`, `core.follow_up_requested`) as versioned definitions with digests.
- One bounded opt-in subscription (new-document candidate classification), cursor + daily budget + recursion exclusion (AT-61/62/63).
- Evaluation tooling for Q0→Q1: deterministic baseline comparison, held-out datasets, confusion/abstention/coverage reporting, qualification profiles (§20.2).
- Cost/latency ledger per §21.2 (measured counters, labelled estimates).
- **Exit:** Q1 advisory preview evidence for the templates actually shipped as validated.

### PR-10 — Hardening + release evidence (T-11, T-12)
- Signed packaging runs, hardware matrix (§20.4), migration rehearsal, offline/sandbox/rollback/kill-switch tests (AT-66–72), accessibility pass, redacted qualification report + model BOM.
- **Exit:** §28.1 checklist satisfied; the completion statement (§28.4) lists exactly what passed.

---

## 3. Dependency graph

```text
G-1…G-5 gates
   └─ PR-1 (docs/ADR)
        └─ PR-2 (contracts)
             ├─ PR-3 (records/admission/deterministic)  ← no Laya needed
             │    ├─ PR-6 (supervision) ─┐
             │    └─ PR-7 (CLI/SDK/MCP)  │
             └─ PR-4 (model lifecycle) ──┤
                  └─ PR-5 (worker/preparer, needs G-2/G-3/G-5)
                       └──────┴─ PR-8 (settings) ─ PR-9 (templates/subscriptions) ─ PR-10 (hardening)
```

PR-3 and PR-4 are independent once PR-2 lands and can proceed as parallel worktrees. PR-5 is the highest-risk/longest-lead item (packaging spike + G-2/G-5) and should start its Phase 0 spike immediately after PR-1.

## 4. Where each AT test lands

| PR | Acceptance tests |
|---|---|
| PR-2 | AT-05, AT-06 |
| PR-3 | AT-07 (partial), AT-09–12, AT-18, AT-19, AT-42–48, AT-50–54 |
| PR-4 | AT-27–30, AT-32–34 |
| PR-5 | AT-13–17, AT-20–26, AT-31, AT-35–38 |
| PR-6 | AT-39–41, AT-45–47 |
| PR-7 | AT-07, AT-08 |
| PR-8 | AT-55–60, AT-72 |
| PR-9 | AT-48–49, AT-61–65 |
| PR-10 | AT-01–04, AT-66–71 |

## 5. Risks and stop conditions (from §10, plus session lessons)

1. **Catalogue amendment is a hard gate.** Do not register decision operations in the runtime registry without the accepted schema amendment — `operations.py` derives from the generated catalogue, so a registry-only change would be rejected by conformance anyway.
2. **G-3 (manifest signing) touches release-owned material.** If trust-anchor reuse is not authorised, PR-4 can implement verification but cannot ship signed manifests — record as a blocker rather than shipping unsigned.
3. **Worker sandbox (G-5) is a release blocker if undemonstrable** — §10.4 says record it, never relax the privacy claim.
4. **Laya 0.1.1 wheel is unverified** (G-2). The packaging spike must resolve this before PR-5, not during.
5. **Windows/Linux hosts never claim the feature** — the availability matrix (§4.4) is enforced by host detection in `decision.status`, not by build flags.
6. **Settings bridge discipline**: every new management action extends the frozen allowlist with native revalidation; page script still cannot execute anything unlisted (PR #117's established pattern, AT-57).
7. **Repo-process lessons from PR #117**: hosted `git diff --check` rejects whitespace the local gate ignores — run it before pushing; the standard-candidate journey has a known flake (#121) that may cost rerun cycles; macOS Phase 2 and Core acceptance are the long poles (~50 min each), so merge promptly when `CLEAN`.

## 6. What can start today, before G-1 closes

- PR-5's Phase 0 packaging spike (isolated Laya runtime build, worker protocol prototype) in an isolated experimental area — no public contract exposed.
- PR-1's binding report and ADR draft (this document is most of it).
- Preparatory test fixtures for the preparer parity and loss-detection cases (AT-13–19) against the pinned upstream tokeniser.

Everything else waits on the gates, per the spec's own stop conditions.
