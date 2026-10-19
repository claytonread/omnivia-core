# TECH-CORE-RETRIEVAL-CODE-001: W13-C production code-analysis vertical (P8, gate G8)

Recorded 2026-10-18.

## Scope

W13-B left W13 `PARTIAL` for three reasons, and this slice closes all three:

| Gap W13-B named | Closed by |
| --- | --- |
| No production service caller reached `register_import_policy` / `import_code_analysis`; tests called the W12 storage seam | `code.policy.register` and `code.import`: catalogue operations served by the engineering family, each running the W12 seam as the domain callback of `execute_mutation` |
| No supported read listed applicable `(repository, snapshot, producer, profile)` bindings | `code.status`: a read beside `code.query` that reports every declared producer profile's availability, readiness, binding, freshness and coverage |
| Every fact of an import cited one manifest artifact, so visibility below whole-import granularity was unreachable from production data | The W12 seam now writes one L0 artifact per captured file and links every fact to the manifest *and* its own file; `code.query` and `code.status` hold diagnostics, coverage and freshness to the same rule |

| Not in this packet | Why |
| --- | --- |
| A TypeScript production route | Declared, reported `unavailable` / `runtime_not_configured`: this service has no verified Node runtime, and nothing locates one without a provisioning decision |
| Review-required candidates | Unaccepted candidates are diagnostics; there is no review queue for them (W12's model, unchanged) |
| Multi-batch imports | One batch per import, so `MAX_CANDIDATES` still bounds a repository (QP-04 decision, unchanged) |
| MCP exposure | Unchanged: the two writes are omitted as mutations, `code.status` as `read_not_allow_listed` |

## Baseline

| Fact | Value |
| --- | --- |
| Checkout | `worktree-omnivia-core-tech-settings`, branch `codex/core-tech-settings-integration` |
| Baseline | `f6f68dbb` (W13-B review repair), clean |
| Commits | `d0a58b8e` file evidence and read side; `c5696685` the three operations; the commit carrying this record |
| Environment | main checkout's `.venv` (CPython 3.11.15, Ruff 0.16.1, mypy) with this worktree's source trees on `PYTHONPATH` plus the OCR overlay; Node from `npm ci` for the TypeScript producer's own tests |

## Chosen binding (decided before coding)

1. **Three catalogue operations, appended as 90-92.** No existing operation carries the
   semantics. `analysis.start` is the structured-data refusal contract and is untouched;
   `import.start` is connector ingestion under `memory:write` with its own job and claim
   tables; `settings.set` is a generic writer whose typed owners validate their own values;
   `code.query` binds one analysis in its required input, so discovery cannot be a seventh
   intent without making the binding optional for the six shipped ones.
2. **The writes join the engineering family; the read joins the read family.**
   `ENGINEERING_FAMILY_OPERATIONS` gains the two names explicitly (repository snapshots,
   their capture and their commit already live there). `code.status` is registered beside
   `code.query` in `build_application_registry` with `knowledge_retrieval`, so
   `service.main`'s derived local-owner read session grants it with no wiring change. No new
   family, session, daemon or store.

   | Operation | Scope / capability | Role | Purpose | Errors | Precondition |
   | --- | --- | --- | --- | --- | --- |
   | `code.policy.register` | `engineering:curate` / `engineering.curate` | `knowledge_reviewer` | `code_analysis_policy` | `CODE_POLICY_MUT` = create + `conflict` | none |
   | `code.import` | `engineering:source` / `engineering.source` | `workspace_contributor` | `code_analysis_import` | `CODE_IMPORT_MUT` = create + `conflict`, `mutation_precondition_failed`, `not_found`, `size_limit_exceeded` | required |
   | `code.status` | `graph:read` / `graph.read` | read session | `knowledge_retrieval` | `POINT_READ` | n/a |

3. **Synchronous route, the `document.reprocess` pattern, not a durable job.** The service
   serves requests and its background work on one thread, so a job would block it just as
   long and `job.cancel` could not reach a running analysis anyway. The producer runs before
   `execute_mutation`, outside any write transaction; the import commits inside it. Cancellation
   is the request's own `deadline_ms`, checked by the producer heartbeat between files.
4. **The CAS token is the scope's activation sequence**, carried as the envelope's
   `mutation_precondition.record_version` (decimal, `0` for none) and read back by the
   precondition reader inside the fenced transaction. W12's own `expected_active_import_id`
   fence is then passed the import that is active in that same transaction.
5. **Replay authority is the executor's claim.** W12's activation idempotency key is derived
   from the caller and the key (`cik-` + SHA-256), so a W12 receipt for it exists exactly when
   the executor's claim does; a settled key skips the policy lookup and the producer and goes
   straight to `execute_mutation`, which replays or refuses `idempotency_conflict`.
6. **Per-file evidence identity is `(repository, path, content digest)`**, kind
   `engineering.captured_file`, over the blob the capture registered, with a code media type
   (`text/x-python`, `text/javascript`, `text/x-typescript`) that `evidence.search` indexes by
   identity only. Each provenance event links two artifacts: the manifest (link 1), the
   fact's own source file (link 2). A version's labels are the union, so a manifest
   restriction still hides everything and a file restriction hides exactly that file's facts.
7. **A hidden manifest means absent everywhere.** `code.status` reports such an analysis as
   `no_active_import` with sequence 0, a hidden requested snapshot as `not_found`, and a hidden
   newer capture is skipped by freshness; `code.query` answers a hidden-manifest binding as
   `projection_unavailable` / `not_ready` / `no_active_import` -- the answer for a scope nobody
   analysed. This refines W13-B decision 6 for the whole-analysis case, where `not_found`
   disclosed that an analysis existed and contradicted `code.status`. W13-B's test for that
   case no longer asserts `not_found`, so it is renamed from
   `test_a_hidden_code_definition_is_not_found_and_is_never_a_neighbour` to
   `test_a_hidden_code_analysis_is_answered_as_a_scope_never_analysed`. A definition hidden by
   its *file* label is still refused byte-identically to one that does not exist.

## What was added

| File | Change |
| --- | --- |
| `storage/code_analysis_import.py` | `FILE_EVIDENCE_KIND`, `file_evidence_id`, `_ensure_file_evidence` (artifact + provenance, refuses an unregistered blob), two evidence links per provenance event; `read_import_policy` / `read_import_policies` (rebuilt through the constructor, refused unless they digest to their record), `import_receipt`, `current_activation_sequence`, `import_file_evidence_ids` |
| `storage/code_query.py` | `read_active_scope`, `read_snapshot_files`, `read_import_files`, `newer_captures` |
| `service/handlers/code.py` | `file_admitted`, `newest_visible_capture`; diagnostics filtered by their own file and the definitions they name, and re-checked at output; `freshness.stale` from captures; hidden-manifest bindings answered as not ready |
| `service/handlers/code_analysis.py` | New: `DECLARED_PRODUCERS`, `CodeAnalysisHandlers.code_policy_register` / `.code_import`, `code_status` |
| `service/application.py`, `service/mutation.py` | Read registration and purpose; engineering family membership and registry; two purposes, two roles |
| `contracts/application/v1/schemas/code.schema.json` (+ `operations`, `application-v1`) | 15 definitions; three catalogue entries |
| `semantics_code.py`, barrel, `generated.py`, `index.ts` | Three validators; regenerated, additive |
| `scripts/check-application-contracts.py` | Frozen entries, `CODE_POLICY_MUT`, `CODE_IMPORT_MUT`, `code.import` in the precondition set |
| CLI `surface.py` | `omnivia code status`, `code policy`, `code import` |
| Ledgers, corpus, docs, pins | Operation and architecture-gate ledgers, MCP omitted partition (+3), seven corpus cases (240, digest re-pinned twice), README catalogue, MCP harness/doc counts, 89 -> 92 and 50 -> 52 pins |
| `tests/phase3/runtime/test_code_analysis_vertical.py` | New, 14 test functions (15 cases) |
| CLI `tests/test_lifecycle.py` | One test against a real managed service |
| `test_code_analysis_import.py`, `test_code_context.py`, `test_code_query.py` | Two-citation provenance, the capture's blob registration in the fixture, the hidden-analysis refusal (renamed, see decision 7), the catalogue tail |

## Evidence

`test_code_analysis_vertical.py` runs on a real installation (`initialise_workspace`), one
real `ServiceRunner`, a real git checkout registered through `engineering.repository.register`,
captured and sealed by `capture_working_tree_snapshot_owned` and committed through
`engineering.source.capture.commit`; the policy, import, discovery and every answer go
through the production dispatchers, in process and over the real local-IPC and HTTP
listeners. Nothing calls the W12 seam, and the reads find the binding through `code.status`.

| Requirement | Test |
| --- | --- |
| The operations ship where their authority lives; MCP unchanged | `test_the_three_operations_ship_where_their_authority_already_lives` |
| No policy, no import; enabling analysis consents to nothing | `test_without_a_registered_policy_nothing_imports_and_nothing_is_consented` |
| Owner-authorised, versioned, idempotent policy; undeclared profile, unknown kind and a smuggled authoriser refused; the authoriser is the audit's principal | `test_a_policy_is_owner_authorised_versioned_and_idempotent` |
| Sealed snapshot -> real Python producer -> W12 -> activation; discovery returns the binding, coverage `partial` (an unparseable admitted file), unsupported suffixes; definition, callers, dependencies, references, `code_context` with spans; file artifacts in `evidence.search` | `test_the_production_route_imports_and_discovery_finds_the_binding` |
| Import, replay, status and every intent including `code_context` equal across in-process / local IPC / HTTP | `test_every_transport_serves_the_same_import_status_and_answers[local-ipc]`, `[http]` |
| Replay returns the stored receipt and never runs the producer; a conflicting replay refuses without analysing; zero rows either way | `test_a_replay_reconciles_and_a_conflicting_replay_refuses_without_analysing` |
| A repeated run of the same snapshot under a fresh key re-activates the same content-addressed import: one activation row, no second import, member, record, assembly, evidence link or artifact | `test_a_repeated_run_reactivates_the_same_import_and_copies_no_fact` |
| A stale plan is `mutation_precondition_failed`; a deadline passing mid-analysis is `deadline_exceeded`; both write nothing and the previous analysis still answers (labelled stale); the same key later succeeds | `test_a_stale_attempt_is_fenced_and_a_cancelled_one_leaves_the_graph_intact` |
| A newer capture marks the analysis stale (status and `code.query`); cutover retires the old binding and no answer mixes the two imports | `test_a_newer_capture_marks_the_analysis_stale_and_cutover_never_mixes_revisions` |
| TypeScript `dependency_unavailable` / `runtime_not_configured`; an undeclared profile `invalid_request`; a snapshot with no Python `conflict` / `no_admitted_source` with zero writes; unsupported suffix groups; another repository's snapshot `not_found` | `test_unsupported_languages_are_stated_never_hidden` |
| One restricted file, two principals: its definitions, relations, diagnostics and evidence vanish for the reader, nothing else moves; a limit sized to the visible members truncates nothing; coverage counts exclude the file; owner unchanged on every transport | `test_one_restricted_file_hides_exactly_its_own_facts_from_another_principal` |
| No path, profile body, principal or workspace can ride in the payload | `test_the_import_route_carries_no_path_principal_or_profile_body` |
| A hidden newer capture neither names itself nor marks staleness; a hidden snapshot is `not_found` exactly as a missing one; a hidden analysis is reported and answered as a never-analysed scope | `test_a_hidden_snapshot_is_reported_exactly_as_one_that_does_not_exist` |
| Acceptance D13: a dependency change re-resolves a byte-identical caller -- the old edge is gone and the producer reports the call `unresolved-in-snapshot`, not a reused edge that W12 would refuse as a dangling endpoint | `test_a_dependency_change_re_resolves_an_unchanged_caller` |
| The real CLI reaches `code status`, `code policy` (with replay) and `code import` on a real managed service; an import without `--record-version` is refused by the CLI before sending | CLI `test_lifecycle.py::test_the_code_analysis_commands_reach_the_service_through_the_generic_cli` |

The real import in these tests accepts 10 definitions and 13 relations and records 10
refused candidates over 4 analysed files (5 admitted Python files; `pkg/broken.py` does not
parse, so coverage is `partial`); the `code_context` architecture component comes back
`available`, a real Context Pack from the existing authority.

## Gate G8

| G8 clause | Status | Proof |
| --- | --- | --- |
| Actual producer -> Core import -> graph/evidence operations through a shipped transport | Met for the Python producer (L4: real Runtime and shipped transports, isolated workspace). Not met for TypeScript: the production route refuses it. | production-route and transport tests; CLI lifecycle test |
| Replay idempotent | Met | replay test (stored receipt, zero rows, producer not run); repeated-run test (no second copy of any fact) |
| Stale producers fenced | Met: the envelope precondition is what fences a stale plan; W12's fence re-checks inside the transaction | stale/cancelled test; mutation probe 3 |
| Mixed revisions never appear | Met | cutover test; D13 test |
| Working-tree versus analysed-snapshot freshness explicit | Met, on one reading: freshness compares the analysed snapshot with the newest admitted capture of its stream, and states working-tree freshness separately as `not_observed` -- Core never reads the live tree, and a new capture is how a working-tree change becomes observable. If G8 means Core must itself detect an uncaptured edit, that is not built. | stale-snapshot and hidden-capture tests |
| No second graph database, server or API | Met: W12 tables, 0009 governed records, the existing executor, transports and families | design; no migration in this slice |

## Status ledgers

`WORK-STATUS.json` records only what the tests above and the re-run W12/W13 suites prove.
It gains one evidence level, `l4_integrated_isolated_workspace` (the specification's L4).

| Row | Before | After | Basis |
| --- | --- | --- | --- |
| D09 forged/mismatched candidates rejected; worker cannot accept its own facts | PARTIAL | PASS | W12's refusal tests plus the production route's payload, policy and snapshot checks |
| D11 replay, conflicting replay, no duplicate edges | PARTIAL | PASS | replay, conflicting-replay and repeated-run tests; W12 replay test |
| D12 atomic activation never mixes; history only where authorised | NOT_RUN | PASS | cutover and stale/cancelled tests; W12 cutover test for history and isolation |
| D15 qualified same-name choices; one-snapshot callers/callees/references with evidence | NOT_RUN | PASS | W13-A same-name test; production-route and transport tests |
| D10 no rule, no acceptance; bulk rule separately authorised | NOT_RUN | PARTIAL | there is no pending or review state, only diagnostics |
| D13 dependency change re-resolves an unchanged caller | NOT_RUN | PARTIAL | one Python dependency change; export, configuration and TypeScript not exercised |
| D03 unsupported cases recorded, never "complete" | PARTIAL | PARTIAL | note rewritten; TypeScript has no production route |
| D16 hidden endpoint or support moves nothing | PARTIAL | PARTIAL | per-file attribution now from production data; finer support and a label writer missing |
| P19 producers and code graph import | NOT_RUN | PARTIAL | Python through the production route; TypeScript only through the W12 seam |
| W12, W13 | PARTIAL | PARTIAL | notes now carry only the remaining reasons |

D14 (rename/delete continuity) is untouched: this slice adds no evidence for it, although
W12's cutover test covers its deletion half. `QUALIFICATION-PLAN.json` is unchanged: QP-04
needs real repositories and recorded producer build identities, QP-05 is the hybrid-mode
transport run, and neither ran.

## Verification

| Command | Outcome |
| --- | --- |
| `pytest test_code_analysis_vertical.py` (host, real sockets; final tree `a6924504`) | **15 passed** |
| `pytest test_code_context.py` (final tree, renamed W13-B test) | **22 passed** |
| `pytest` W12 import + code.query + code_context on an export of exactly `d0a58b8e` | **52 passed** |
| `pytest` code-analysis vertical/query/context/import/python/typescript/candidates, engineering register/capture/source/continuity/qualification, working-tree snapshot, graph traverse, context pack, governed resolution, evidence capture development-time run, passed after the one expected pin update (`code.query` is no longer the last entry); superseded by the full preflight suite below |
| `pytest` S0 mutation foundation, workspace-inspect refusals, knowledge-search vertical, settings refusals, S5 integrated registry, installed MCP authority, application authorisation, DEV-REQ-081 | all passed after the exact-set and count pins moved |
| `pytest tests/contracts tests/service_conformance` | **12032 passed, 19 skipped** |
| `pytest packages/omnivia-core-cli/tests packages/omnivia-core-client/tests packages/omnivia-core-mcp/tests` | **3266 passed, 8 skipped, 5 failed** -- the five known worktree-only MCP installed-setup tests, whose qualification child imports the main checkout. Through a throwaway venv whose `.pth` lists this worktree's trees: **22 passed** |
| CLI `test_lifecycle.py` (real managed service) | **6 passed** |
| `generate-application-contracts.py --check`, `check-application-contracts.py` | up to date; passed |
| `check-package-boundaries.py`, `check-raise-discipline.py`, `check-migration-allocations.py`, `generate-code-analysis-typescript.py --check` | passed; current |
| `ruff check src packages baseline tests scripts` | all checks passed |
| `mypy --strict packages/omnivia-core-runtime/src/omnivia_core_runtime src/omnivia_core` | no issues in 356 source files |
| `git diff --check` | clean |
| `./scripts/preflight` (full run on `c5696685` working tree, `PYTHON` = throwaway worktree venv) | **exit 1.** All gates before the full suite passed. Full repository suite: **4 failed, 38,388 passed, 59 skipped, 7 warnings in 3239.13s**. The script stops there, so the last four steps were run individually (rows below) |
| The four preflight failures | Three (`test_v06_5_s1_two_legacy_workspace_services_still_coexist`, CLI `test_a_foreign_same_named_service_on_path_is_not_selected`, MCP `test_a_lost_capture_response_replays_after_a_real_service_restart`) need an `omnivia-core-service` console script beside the interpreter, which the throwaway venv lacks; rerun with the repository main venv and this worktree's `PYTHONPATH`: **3 passed**. The fourth, AT09 `test_selected_source_reaches_core_exact_and_is_left_unchanged` (Swift delivery stays `pending`, not `admitted`), **fails identically on an export of baseline `f6f68dbb`** |
| Workflow `Run benchmark tests` | **23 passed** |
| Workflow `Run Ruff` (ruff 0.16.1, full workflow scope) | all checks passed |
| Workflow `Run strict mypy` (mypy 2.3.0, full workflow scope) | no issues in **429** source files |
| Workflow `Build and test the macOS status menu companion` | **Bounded exception, not green.** Build passes; `swift test` fails in AbsentTarget acceptance tests (`start_failed` for `start_started`, then an `Index out of range` crash), host/service-start failures reproduced at baseline; the full baseline Swift suite later timed out in `OcrResourceAdministrationTests`. Neither full Swift suite was rerun. This slice changes no Swift code |

### Mutation probes

Each mutant applied in place, `__pycache__` dropped, the named test run with `-B`, the source
restored under a SHA-256 check and the working-tree diff digest compared before and after.
**11 killed, 0 survived.**

| Mutant | Killed by |
| --- | --- |
| Facts cite only the manifest (file link dropped) | restricted-file test: the limit-1 `truncated` falsifier |
| Diagnostics not held to their own file | restricted-file test: the limit-1 `truncated` falsifier (the silent path, not the output revalidation) |
| Precondition reader always agrees | stale/cancelled test: the stale plan then *succeeds*, so W12's in-transaction fence alone does not fence a stale caller |
| Deadline ignored by the heartbeat | stale/cancelled test |
| Settled key analysed again | replay test (producer ran twice) |
| Status coverage counts hidden files | restricted-file test (the reader's analysed count includes the hidden file: 4, not 3) |
| Status shows a hidden-manifest analysis | hidden-snapshot test |
| Freshness reports a hidden newer capture | hidden-snapshot test |
| Policy may lock an undeclared profile | policy test |
| Raw payload keys not held to the declared set | smuggled-payload test |
| `code.query` answers a hidden-manifest binding | hidden-snapshot test |

The first diagnostics probe was masked by the output revalidation (an unlimited answer that
disclosed the leaked diagnostic was refused `not_found`). The test now asks the limit-1
question first, with a visible diagnostic that sorts before the hidden one, so a pre-walk leak
shows up as `truncated: true` -- the case W13-B's repair record warns revalidation cannot see.

## Limitations and residuals

- **TypeScript has no production route.** Running the packaged analyser needs a verified
  Node runtime this service is never told about, and `subprocess.run` blocks the service
  thread without renewing the lease for up to the profile's 600 s timeout. A TypeScript
  route needs a runtime-provisioning decision and a heartbeat-capable runner first.
- **No production writer labels code-file artifacts.** Attribution is per file in production
  data; the labels themselves come from 0008's label stream, which today only connector
  ingestion writes, and `EVIDENCE-LABEL-GRANT-DEFERRED` still bounds the grant model (the
  local owner holds every label, anyone else none). The tests attach the label through that
  stream. A file artifact is per content revision, so a changed file is a new artifact
  carrying no file label until something writes one; a per-path source policy would.
- **Support finer than the file is not recorded.** A relation is held to its occurrence file
  and both endpoints' files. A resolution that also depends on an intermediate file (a
  TypeScript re-export chain) is not visible to the gate, because the producers record one
  source binding per candidate.
- **The import is synchronous** and occupies the service thread for the analysis, bounded by
  the declared profile (500 files, 32 MiB, 10 000 candidates) and the request deadline.
- **One batch per import:** a repository whose analysis exceeds 10 000 candidates (roughly
  35-40 real Python modules) is refused `size_limit_exceeded`.
- **Millisecond read instants:** `code.query` and `code.status` select at the floored
  millisecond, so a read inside the same millisecond as an activation may not yet see it.
- **Code-file artifacts join `evidence.search`** by identity (path tokens), not content.
  W14 code-oriented retrieval is not claimed.
- **The repository preflight is not green.** Full suite 4 failed / 38,388 passed / 59 skipped: three throwaway-venv service-script failures pass under the main venv, AT09 fails identically at baseline `f6f68dbb`, and the Swift companion suite fails with baseline-reproduced host/service-start failures. Whole-repository qualification is not claimed.
- **Adapters remain `pending_candidate`** in the operation-traceability ledger; the
  live-evidence runner was not run.

## ADR / PM

Codex should record against decisions D19-D20 (the graph/governance authority decisions
QP-04 cites, not acceptance rows D19-D20): the three operations and their families, scopes,
roles and purposes; the synchronous route with the activation sequence as the CAS token and the
request deadline as cancellation; per-file evidence attribution (two citations per fact) as the
code-visibility granularity; "a hidden manifest means absent everywhere", which refines W13-B
decision 6; and TypeScript as declared-but-unavailable pending a runtime decision. G8 is met
at L4 for the Python producer. W13 stays `PARTIAL` only for reasons outside the three gaps
this slice closed: acceptance D16's resolution-support granularity and label writer, D17's
seed producer (W14), D18's absent similarity source, and the TypeScript route. Codex should
also decide whether `working_tree: not_observed` satisfies G8's freshness clause (see the
G8 table).
