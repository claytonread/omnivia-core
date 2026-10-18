# TECH-CORE-RETRIEVAL-CODE-001: integrated P1-P10 implementation closeout

Recorded 2026-10-18 by the P10 packet. This replaces the P1-only closeout. It is a ledger and a set of
release decisions for independent Codex review. It is not a release, a merge or a push, and it changes no
product code.

Facts come from `WORK-STATUS.json` (the ledger) and `QUALIFICATION-PLAN.json` (the runs); where they and
this file disagree, they win. A complete `./scripts/preflight` started by the P10 packet on the pristine accepted tree was stopped
before completion after the documentation changed; it has no result and does not satisfy final-tip
verification. **No preflight result is claimed here.** Codex will run and report one complete preflight
after the closeout commit, against the exact immutable final tip.

## 1. Headline

- Nothing in this branch is release-qualified. No capability has L5 (packaged) or L6 (release) evidence.
- The strongest delivered vertical is Python code intelligence: producer, governed import and graph
  through the shipped transports at L4, accepted by Codex at plan gate G8. TypeScript has no production
  route.
- Semantic retrieval is foundation only: a model-free exact scorer, a vector projection store and an
  internal hybrid seam with **no production caller** and no embedder.
- Semantic validation is not implemented. Optional Spotlight is not implemented.
- Settings v2 is a six-pane window whose Models, Search and embeddings, Code analysis, Local Decisions,
  Background processing and Activity details are explicit "not built" text. W17 (C-01) and W18 (indexing
  controls) are not implemented.
- No clean plain preflight exists. The AT09 harness defect that forced the overlay's deselect was repaired
  on 2026-10-18 (section 17); no complete preflight has run since, so none is claimed.

Ledger counts (from the JSON): work items 19 (14 PARTIAL, 5 NOT_RUN, none PASS); properties 38 (27 PASS,
5 PARTIAL, 4 NOT_RUN, 1 BLOCKED, 1 FAIL); acceptance entries 256 = 138 technical cases + 116 UI scenarios
+ 2 unnumbered groups (8 PASS, 37 PARTIAL, 209 NOT_RUN, 2 BLOCKED, 0 FAIL); qualification runs 11
(3 PASS, 5 NOT_RUN, 3 BLOCKED); release decisions 7.

## 2. Accepted branches and commits

| Repo | Branch | Commit | Notes |
|---|---|---|---|
| Core | `codex/core-tech-settings-integration` | `4d453fe614225c0aff4cdd55dcd6fd1695686728` (tree `b672ddf4...`) | Product-code tip `a6924504`; accepted at plan gate G8 (Python route, L4) |
| Dev | `codex/core-code-intelligence-integration` | `50a12d285d212eb138930ea1c8b7df08862b37e1` | Commits `32faf17`, `015da63`, `50a12d2` on `e33cd5b21d51`; 8 files, +3342 -606; evidence supplied by Codex |
| Platform | `voice-parakeet-parakeet-v2` | `0242519315d144c178078eaead7c18bfc2baf5fd` | A 2026-09-28 docs commit; no Platform product change for this programme |
| PM plan | `codex/chat-c5-c11-closeout-decisions` | `dff76a050cdeb0c5cd43f5a1a0d238c65bca4c8e` | Adds one file; plan sha256 `0d2383a9...c433` |

### Branch-topology integration risk

- `origin/main` is `c9f58915`; the Core branch is **306 commits ahead** (491 files, +249,660 -26,245). Local
  `main` is `b75096ff`, stale and 128 commits behind `origin/main`. A pull request carries all 306.
- Only the top 36 commits (`32c2015d..4d453fe6`, 193 files, +42,438 -841) are this programme's lane: semantic
  foundations, update trust, static-gate repair, Settings v2, the P7 producers and the P8 import, query,
  context and production vertical. The other 270 are the Waves 1-4 document-intake lanes (29 merges in the
  range). A reviewer approving "the tech-specs work" is also approving those.
- Lanes were cherry-picked across diverged branches and numbers renumbered (semantic projection 0069 to
  0073); literal head pins in tests and the catalogue annotation order (appended 87-92, not alphabetical)
  are integration-sensitive. The Settings v2 window replaces the previous pane layout for everyone who
  merges the branch, including its unbuilt placeholders.
- Dev's commits sit on a different branch from the dirty Dev checkout; Platform has no commit in this
  programme. Cross-repo ordering (contract first, then clients) is not enforced by anything in git.
- Every migration 0066-0074 is `candidate` with `accepted_commit` null; none is accepted.

## 3. Scope: advertisable, disabled, component-only, not implemented

| Capability | State |
|---|---|
| Lexical evidence search, Context Pack, graph traverse (pre-existing) | In the tree; applicable clauses mapped to tests, four pending (section 17) |
| Native document intake (Waves 1-4) | In the tree; repository-level gates only (68 of 130 passed) |
| Python code intelligence (`code.policy.register`, `code.import`, `code.status`, `code.query`) | Implemented, opt-in by owner policy, **not advertised** |
| TypeScript/JavaScript code intelligence | Producer is component-only (L3); production route unavailable |
| Semantic vector projection, hybrid seam | Component-only; no operation, no transport, no caller |
| Settings v2 window; `settings.list`/`settings.set` | Window built; operations published but refused `dependency_unavailable` |
| Application update contract, trust, staging | Contract/component only; production verification refuses every envelope |
| Embedding models, vector backend, build jobs, C-01, indexing controls, update UI, shortcut | Not implemented |
| Semantic validation (P4) | Not implemented |
| Optional Spotlight, ANN, reranking, multimodal, arbitrary languages, cloud hosting, theorem prover | Deferred (section 12) |

## 4. User-visible behaviour (verified in source and records)

- **Settings window.** General: Appearance and Notifications rows are real; Find in Core reads "A global
  shortcut needs an owner that registers it with macOS and reports conflicts. This build has none.";
  Check for updates reads "...This build has none."; Open code in is likewise unbuilt; the login-item toggle
  is disabled. Sources: the lists are empty and say Core does not advertise a source manager; Add buttons
  are disabled (the Waves 1-4 Files and folders manager is reachable only from the status-menu item).
  Capture defaults show four rows, all "Not reported". Processing: Document processing is real
  (`document.settings.get`/`update`); the other five destinations and the activity view say what they wait
  for, e.g. Activity: "Core does not report activity to this window, so there is nothing to show."
- **Status menu.** No indexing row, no Pause/Resume, no Updates row, no Activity route exists.
- **Lexical retrieval/context/evidence.** Existing operations (`evidence.search`, `context_pack.build`,
  `graph.traverse`, task-context) are unchanged in contract; `context_pack_build` was split so
  `code_context` shares the one Context Pack authority.
- **Semantic validation/retrieval.** Nothing user-visible. No operation takes a keyword/semantic/combined mode.
- **Code intelligence.** An owner registers a versioned policy (`code.policy.register`); `code.import` takes a
  sealed snapshot through the Python producer into an active analysis (synchronous, idempotent, fenced);
  `code.status` discovers availability, readiness, binding, freshness (`working_tree: not_observed`) and
  coverage; `code.query` answers definition, references, callers, callees, dependencies and `code_context`.
  An analysis that is not ready is a typed refusal, never an empty answer.
- **Dev Project Map.** Read-only consumer of `code status`/`code query` through the Core CLI client; an
  explicitly selected repository and snapshot; shows `Code analysis not ready for this revision` rather
  than an empty graph. Open captured source, Open current working file and Add evidence are disabled
  descriptors.

## 5. Capability and status discovery

Discoverable: `code.status` (per producer profile: TypeScript `unavailable` / `runtime_not_configured`;
missing or old analysis; coverage; unsupported suffix groups), `code.query` not-ready refusal
(`projection_unavailable`, `details.readiness`), required `graph_expansion` field, and
`settings.*` refusals. **Not discoverable through any contract:** semantic/hybrid retrieval, indexing state,
models, application updates. The Settings window's placeholders are static text, not a contract (P37 PARTIAL).

## 6. Authority invariants

- One Core authority. Imported code facts are ordinary 0009 governed records under 0009's seal trigger; no
  second graph database, server or API; no second updater, scheduler or settings store.
- Workers and producers return values; they write nothing and accept nothing. Policy authoriser comes from
  the executor's settlement, never from the policy document. The import payload carries no path, principal,
  workspace or profile body.
- Dev reaches Core only through the Core CLI client (no private database, locks, leases or analyser paths).
  Platform's evidence is its shared adapter against a stub.
- `search_authorized_frontier` has no production caller; `engineering.code` is structurally excluded from the
  memory and sharing reads.
- Limits: visibility is per captured file, not finer; the grant model is bounded by
  EVIDENCE-LABEL-GRANT-DEFERRED (owner sees all, others see unlabelled).

## 7. Transport and client evidence

| Path | Level | Gap |
|---|---|---|
| Code operations, in-process, local IPC, HTTP, generic CLI | L4 (Python route) | MCP deliberately unexposed; adapters `pending_candidate` |
| Dev Project Map against real Core | L4, supplied by Codex, not re-run | Source-open controls disabled; Python only |
| **Platform adapter** | **L2 simulated, not L4**: a real adapter driven through an injected stub transport returning a canned result (temp test sha256 `fd31e5bc...b69f2`, not retained in any repo) | No real-Core run; no code-query UI |
| Hybrid search over any transport | none | no contract decision |
| MCP real host | none current | records from 2026-10-04 predate the manifest and `settings.*`/`code.*` |

## 8. Migrations, catalogue, changed files

- Migration 0073 `semantic_vector_projection` (sha256 `a21aea92...ab52`, renumbered from 0069) and 0074
  `code_analysis_applicability` (sha256 `62c2bfc2...0997`; six append-only fenced tables; replaces 0009's seal
  trigger only after a predecessor gate) are new in the lane; 0069-0072 are Waves 1-4. All `candidate`.
- Catalogue is 92 operations; this lane appended 87 `settings.list`, 88 `settings.set`, 89 `code.query`,
  90 `code.policy.register`, 91 `code.import`, 92 `code.status` in frozen insertion order.
- Lane diff by area (193 files): contracts/generated 71, tests 52, runtime source 24, docs 14,
  scripts/CI/build 13, cli/client/mcp 10, native Swift 4, migrations 2, other 3. Reproduce:
  `git diff --name-status --no-renames 32c2015d 4d453fe6`.
- Dev: 8 files (`core_authority.py`, Project Map view, screen inventory, README, four test files).
  Platform: none. PM: one plan file.

## 9. Compatibility, recovery, rollback, upgrade

- Fresh install, upgrade 0073 to 0074 with graph kept, tampered-trigger refusal with rollback, and backup/
  restore carrying the applicable graph: L3 tests (P31). Not run: packaged upgrade, any downgrade, process-kill
  crash tests of `code.import`, packaged restore.
- Code rollback is re-activation of an earlier import (a guarded activation), not a schema downgrade. There is
  no disable operation for code analysis or semantic retrieval; opting out is not registering a policy.
- Update rollback, signed activation and restart-after-update: not built or run.

## 10. Evidence dimensions not run or incomplete

Platform/architecture: one host class only (macOS arm64); Intel, older macOS, Linux/Windows rows not run
locally. Offline/network-denied: producers assert no network modules; Node permissions do not gate the
network; no OS-level denial test. Permission revocation and hidden-corpus: code-graph differential and
revocation tests exist (P29, P30); the BM25 add/remove rare-term statistics matrix (B06) and two-workspace cache
isolation (A17) are not run (the lexical path's own partial evidence is mapped in section 17). Retention/deletion: WP-15 repository gates only. Performance/resource: calibration
timings only; no p95, RSS or scale run (QP-02). Accessibility: none (blocked, external). Signing/notarisation
and update activation: none. Native intake: 130 WP-15 gates = 68 passed, 47 blocked_external, 15 not_executed,
0 failed; that ledger also says no test has a real companion talking to a real Core, which live-Core Swift test
files in the tree contradict (generated file, not edited).

## 11. Release decisions (proposed, pending independent review)

Decisions: SHIP = may be advertised once repository gates pass; DISABLED = in the tree, not advertised or
enabled; DO_NOT_SHIP = nothing releasable or claims unsupported; BLOCKED = could ship but an external
prerequisite is missing. Because plan gate G10 is open (no green plain preflight, no pull-request checks, no
review), **no area is SHIP**. The seventh area is the lexical path, added to the six the plan names.

| # | Area | Decision | Evidence level | Rationale | Entry conditions |
|---|---|---|---|---|---|
| 1 | Native intake | BLOCKED | repository tests (L3); no L5 | 68/130 gates passed, 47 blocked_external, 15 not_executed; unsigned, `production_release_eligible: false`; AT09 harness repaired 2026-10-18 and passing locally only; Swift host failures as recorded; extension sources typechecked, not run | signing identity and notarised build; installed-signed evidence; a hosted run of the AT09 end-to-end half; extension tests; accessibility review |
| 2 | Lexical retrieval, evidence, Context Pack | BLOCKED | L3-L4 where exercised; clauses mapped (3 covered, 4 pending) | pre-existing path, shared authority refactored; erase-then-search, backup/restore-then-search, BM25 add/remove statistics and two-workspace cache isolation have no test; green exact-tip validation absent | green PR checks; evidence for LEX-01, LEX-03, LEX-05 and LEX-06; review |
| 3 | Semantic validation | DO_NOT_SHIP | L0 | no claim gate, warrants, SHACL or Context Pack preservation exists | P4 sub-slices implemented and qualified |
| 4 | Semantic retrieval | DISABLED | L1/L3 foundations | no embedder, backend, build job, operation or transport; QP-01 blocked | QP-01 to QP-03, QP-05, contract decision |
| 5 | Code intelligence | DISABLED (Python, opt-in); TypeScript DO_NOT_SHIP | L4 Python fixtures; L3 TS | real repositories refused beyond about 35-40 modules; QP-04 not run; unaccepted migrations; MCP off | multi-batch or ceiling decision; real-repo run; Node provisioning for TS; migration acceptance |
| 6 | Settings / update / model UX | DO_NOT_SHIP | build + unit (record-stated) | `settings.*` refused; no update, model, shortcut, indexing UI; C-01 and W18 absent. Merging also ships the v2 window | decide whether to hold the window commits; implement and qualify W17, W18, update owner, Models page |
| 7 | Optional Spotlight | DISABLED (deferred) | none | not implemented; outside the bounded first release | field-level consent, restricted-source exclusion and journalled removal qualified |

## 12. Deferred register

Spotlight, ANN/quantised shortlist (EX2), reranking (EX3), multimodal embeddings (EX4), other languages and
embedding models (EX5), additional analysers (EX7), content-defined chunking (EX1), related-evidence
suggestions (EX6), alternative fusion (EX8), cloud hosting and theorem-prover integration: each stays off with
the entry condition that a measured need, a qualified profile and a recorded decision exist first.

## 13. Gates

Plan gates (implementation plan section 6) are **not** the spec's gates (spec section 21.1); the labels
G0-G10 collide.

| Plan gate | Status | Why |
|---|---|---|
| G1 shared contracts/migrations | not met | code and settings contracts pass generation/conformance; status, pause, C-01, model, update-state and validation contracts do not exist; no shared Swift/TS state fixture |
| G2 native intake | partial | repository gates only; no L4 companion-to-Core claim, no L5 |
| G3 lexical retrieval | partial | capability pre-existing; clauses mapped in the lexical traceability fixture, LEX-01, LEX-03, LEX-05 and LEX-06 pending; no hosted or packaged run |
| G4 semantic validation | not met | not implemented; not a release claim |
| G5 semantic retrieval | not met | disabled; not a release claim |
| G6 Settings/updates/models | not met | none of the clauses |
| G7 producers | met at L3 | both producers on fixtures; caveats in P24/P25 |
| G8 governed import and graph | met for Python at L4 | accepted by Codex; TypeScript not |
| G9 clients | partial | Dev L4 (supplied) meets the journeys; Platform L2 |
| G10 integrated qualification | not met | nothing is a release claim |

Spec gates (section 21.1): G0 binding partial; G1 resource/encoder not run; G2 retrieval correctness/privacy
partial (fixture level); G3 lifecycle/durability not run; G4 relevance/resources not run; G5 producer
semantics met at L3; G6 governed import/graph met at L4 for Python; G7 transports/clients partial; G8
packaging not run; G9 Settings/native input not met; G10 indexing controls not met.

## 14. Unrun residuals

Real model and backend; scale and latency; content-bearing input; real repositories and multi-batch import;
TypeScript production route and pinned Node; hybrid public mode; MCP real host; packaged, signed and
notarised builds; update activation; accessibility; Intel/older macOS; BM25 add/remove rare-term matrix;
two-workspace cache isolation; C-01; indexing controls; shortcut; semantic validation.

## 15. Reproduction and retained evidence

- Ledger parse: `jq empty` on `WORK-STATUS.json` and `QUALIFICATION-PLAN.json`.
- Historical overlay (runs RUN-01 to RUN-11; the deselect is no longer needed after section 17): `PATH="$PWD/.venv/bin:$PATH" PYTEST_ADDOPTS="-o tmp_path_retention_policy=failed --deselect packages/omnivia-core-runtime/tests/phase3/runtime/test_at09_selected_source_end_to_end.py::test_selected_source_reaches_core_exact_and_is_left_unchanged" ./scripts/preflight`.
- Retained in the repo: the P1, P7 (Python, TypeScript) and P8 (W12, W13-A/B/C) packets here; the Settings v2,
  update-trust and static-gate records; `docs/quality/wp15-qualification-status.md`. No raw log is retained.

## 16. Stale or contradictory claims

Replaced by this file: "P1 only", "no production caller" for code, "code producers NOT_RUN", "nothing ran".
Still stale and **not edited** (outside this packet): `BASELINE-AND-BINDINGS.md` describes only the P1 slice
(e.g. "Producers, import and graph activation are later work"); WP-15's "no companion-to-Core test" statement;
the spec's own stale supersedes/ui_companion metadata. In the ledger, P15 is scoped to the P1 slice.

## 17. Release-readiness slice (2026-10-18, uncommitted, for Codex review)

Baseline `f4734816` on `codex/core-release-readiness-integration`. Working-tree edits only: not committed,
pushed, merged or released. The decisions in section 11 are unchanged: semantic validation DO_NOT_SHIP,
semantic retrieval DISABLED, code intelligence DISABLED by default (Python opt-in, TypeScript DO_NOT_SHIP),
Settings/update/model UX not release-enabled, Spotlight deferred and DISABLED. No product behaviour changed.

| Change | What it shows | What it does not |
|---|---|---|
| AT09 harness starts the managed service (`test_managed_start._run`) before the Swift half | The exact test passes with no deselect on a local Darwin host with Swift. The defect was the harness: the companion delivers attach-only (`--require-running`), so the item stayed `pending` | No hosted CI row runs the end-to-end half; one text fixture; no OS or Xcode version recorded |
| `.gitattributes` `-text -whitespace` on the AT-09 fixture; blank line removed from `test_refresh_outcome_gates.py` | `git diff --check origin/main` (committed and uncommitted changes) is clean, with the fixture's bytes (pinned by SHA-256) untouched | The committed HEAD alone still carries the blank line until this slice is committed |
| Native extension `swiftc -typecheck -application-extension` in `scripts/preflight` and the macOS step of `phase2-platform.yml` | Both extension source sets typecheck locally | Typechecked, never run; no job, check name or matrix changed; hosted result not observed |
| Lexical traceability fixture and test | 7 clauses: LEX-02, 04, 07 covered by named, unskipped tests; LEX-01, 03, 05, 06 pending | LEX-01: the catalogue has no erase or purge operation. LEX-03: no test restores a backup of a workspace with a built projection and searches it. LEX-05: no BM25 add/remove statistics or rare-term matrix test (the adjacent long-text and supersede tests are partial evidence only). LEX-06: no two-live-workspace or cache-isolation test (the foreign-frontier refusal and no-cache check are partial evidence only). None is claimed |
| `test_lb_l13b` migrated-only-to-0012 build and search | A workspace stopped at 0012 reports `user_version` 12 and ranks as the head schema does | No upgrade-from-populated-0011 data |
| Installed journey: `_require_seeded_retrieval` | The seeded source and record rank first and the Context Pack cites the record and a `governed_record` section, identically on a second build; seven mutations each fail it | One seeded candidate, so position and citation, not discrimination. `pack_id` covers the build instant and is not compared. The journey runs in package qualification, not under pytest. A lifecycle restore-then-search assertion does not fit `run-standard-lifecycle.py`, which restores the legacy pre-upgrade database and never searches |
| WP-15 ledger text | The AT-09 limitation records the repair and the extension typecheck; the generated status report is unchanged and matches | No gate status changed: still 68 passed, 47 blocked_external, 15 not_executed |

Verification run for this slice is listed under "Verification" in the hand-back; no complete `./scripts/preflight`
was run. Not produced: native L5/L6, signing, notarisation, accessibility, Intel or older macOS, a real
security-scoped bookmark, removable or iCloud volumes, a real process kill, any performance measurement, or a
hosted matrix result.
