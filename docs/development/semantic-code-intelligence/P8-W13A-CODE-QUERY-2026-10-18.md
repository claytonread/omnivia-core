# TECH-CORE-RETRIEVAL-CODE-001: W13-A `code.query` read vertical

Recorded 2026-10-18.

## Scope

One shipped, read-only application operation, `code.query`, over the governed code graph
W12 imports and activates. Five closed intents: `definition`, `references`, `callers`,
`callees`, `dependencies`. It is a single vertical: contract, generated bindings, storage
read, handler, production registration, CLI command, ledgers and a real-producer test over
all three production transports.

| Not in this packet | Owner |
| --- | --- |
| Code-context assembly (composing `code.query` seeds with the context authority and multi-principal revalidation) | W13-B |
| D16 multi-principal endpoint confidentiality, D17 | W13-B. Only the local-owner read session serves `code.query` here; nothing below proves another principal's view. |
| A production caller of the W12 import seam (policy authorisation or import as an operation) | Later packet. Imports in the tests are made through the W12 storage seam directly. |
| A discovery operation listing applicable `(repository, snapshot, producer, profile)` bindings | Not decided. A caller must already know the binding it asks for. |
| MCP exposure | Deliberately off: `code.query` joins the unexposed partition as `read_not_allow_listed`. |

## Baseline

| Fact | Value |
| --- | --- |
| Checkout | `worktree-omnivia-core-tech-settings`, branch `codex/core-tech-settings-integration` |
| Baseline | `4207a71b` (W12 pin), clean |
| Environment | main checkout's `.venv` (CPython 3.11.15, Ruff 0.16.1, mypy) with this worktree's source trees on `PYTHONPATH` plus the OCR overlay; Node + `npm ci` for the TypeScript producer |

## What was added

| File | Change |
| --- | --- |
| `contracts/application/v1/schemas/code.schema.json` | New definition container: `CodeQueryInput`, `CodeQueryResult`, `CodeAnalysisBinding`, `CodeSymbol`, `CodeRelation`, `CodeDiagnostic`, `CodeSourceSpan`, `CodeQueryCoverage`, `CodeSemanticKey`. No new published pattern: values reuse `Identifier`, `OpenCode`, `ContentChecksum`, `EvidenceId`, `Timestamp`, `PageLimit`, `GraphDepthLimit`, `RecordVersionReference`, `ProjectionFreshness`. |
| `operations.schema.json`, `application-v1.schema.json`, catalogue fixture | `code.query` appended as operation 89 (after `settings.set`), `graph:read` / `graph.read@1.0`, synchronous, not paginated, audited read, `GRAPH_READ` error profile. |
| `scripts/{generate,check}-application-contracts.py` | `code` source schema; `FROZEN_OPERATIONS` entry. |
| `generated.py`, `generated/typescript/.../index.ts` | Regenerated; additive only. |
| `src/omnivia_core/contracts/v1/semantics_code.py` (+ barrel exports) | `validate_code_query_input`: closed intents, generated guards for every patterned field, the producers' semantic-key grammar, intent-scoped `depth` / `runtime_only`. |
| `storage/code_query.py` | `read_code_graph`: one read transaction over 0074's active import, the applicability-aware resolver, the import's members, sealed relation endpoints and diagnostics. |
| `service/handlers/code.py` | `code_query`: the five intents, ambiguity, coverage, not-ready refusal. |
| `service/application.py` | `CODE_QUERY_OPERATION`, `OPERATION_PURPOSES` (`knowledge_retrieval`), `build_application_registry`. `main.py` unchanged: the read session is derived from the registry. |
| `omnivia_core_cli/surface.py` | `omnivia code query` (the import-time catalogue bijection requires one command per operation; dispatch is generic). |
| Wire corpus, ledgers, pins | One primary-success corpus case (232 cases, digest re-pinned twice); operation-traceability block + MCP omission; three whole-catalogue architecture gates; counts 88 -> 89 and MCP unexposed 53 -> 54 / excluded 92/71 -> 93/72 in tests, harness, guide and docstrings; the settings "last two" pin moved to `[-3:-1]`. |
| `tests/phase3/runtime/test_code_query.py` | New, 12 tests. |

## Design decisions

1. **One generic read, in the existing read family.** Registered beside `graph.traverse`
   in `build_application_registry`, so the local-owner session `service.main` derives
   grants it with no wiring change. Same scope, capability and purpose as `graph.traverse`;
   no new authorisation family, and `ENGINEERING_FAMILY_OPERATIONS` is untouched.
2. **The binding is checked, never trusted.** The request names repository, snapshot,
   producer and profile. The read takes the newest 0074 activation of
   `(repository, producer, profile)` at the query instant and refuses unless its import
   analysed exactly the requested snapshot. Workspace and principal come only from the
   authorised `OperationContext`.
3. **Not ready is a refusal, not an empty answer.** `projection_unavailable`
   (`retryable_after_delay`, the engineering reads' convention) with
   `details = {"readiness": "not_ready", "reason": "no_active_import" | "snapshot_not_active"}`.
   No current or mixed fact is returned on that path.
4. **No second currency rule.** Facts come from `resolve_governed_versions`, which already
   drops retired imports; membership in the one active import then separates them from
   manual facts, other producers, profiles and imports. Edges are the sealed
   `omnivia_governed_relation_endpoints` rows, both ends required to be current entity
   members of the same import; content supplies display data and source spans only.
5. **Static, and said so.** Relations are accepted `syntactic`/`resolved_static` W12 edges
   only. Refused candidates (unresolved, ambiguous, external, `inferred_dynamic`, out of
   policy, self-relation) that bear on the subject come back as `diagnostics` and are never
   walked. `coverage.analysis` is always `static`; inbound intents always set
   `unattributed_inbound`; `complete` is true only when no limit, depth, diagnostic,
   inbound attribution gap or ambiguity bounded the answer. Nothing is runtime coverage or
   test coverage, and no similarity or prose source is read.
6. **Ambiguity is returned.** A shared display name answers `resolution: "ambiguous"` with
   every qualified choice in key order and walks nothing. An unknown subject, or a key and
   name that disagree, is `not_found`.
7. **Intent semantics.** `references`: inbound `code.references`, one step. `callers` /
   `callees`: inbound / outbound `code.calls`, depth 1-8 (default 1). `dependencies`:
   outbound `code.imports` + `code.references`, depth 1-8; `type_only` preserved on every
   relation, and `runtime_only` drops type-only edges from both answer and walk.
   `depth_bounded` reports relations left unfollowed past the depth.
8. **Bounded, deterministic output.** Each list is cut at `limit` (default 100, max 1000)
   with `truncated` stated; symbols order by `(depth, key)`, relations by
   `(depth, source, type, target, edge key)`, diagnostics and choices by key. Source spans
   are repository-relative path + captured content digest + locator; the binding carries
   import, batch, policy and manifest-evidence references. No absolute path, blob, corpus
   count or internal id beyond governed record references is exposed.

## Evidence (`test_code_query.py`, 12 tests, real producers, real SQLite, production dispatcher)

| Requirement | Test |
| --- | --- |
| Shipped read beside `graph.traverse`, last catalogue entry, granted by the derived local-owner session | `test_code_query_is_a_shipped_read_beside_graph_traverse` |
| 1. Exact key, unique name, same-name ambiguity with deterministic qualified choices, limit truncation, `not_found` cases | `test_exact_and_same_name_definitions_are_deterministic_and_qualified` |
| 2. Resolved reference with exact source locator (byte span slices `Shape` from the captured bytes) | `test_resolved_references_carry_the_exact_occurrence` |
| 3. Caller/callee direction and depth | `test_callers_and_callees_follow_static_calls_in_their_own_direction` |
| 3. Dependency depth, transitive step, `type_only` and `runtime_only` | `test_dependencies_honour_depth_and_type_only_semantics` |
| 4. Unresolved / dynamic edges only as diagnostics (Python dynamic binding, TypeScript dynamic dispatch) | `test_dynamic_and_unresolved_calls_are_only_coverage_diagnostics` |
| 5. No import, other profile, other producer, unimported producer, later snapshot: not ready | `test_missing_or_mismatched_analysis_is_not_ready_never_empty` |
| 6. Cutover changes the answer and the old binding becomes not ready; no fact of the retired import; replay returns the same receipt and the same answer | `test_cutover_changes_the_answer_atomically_and_replay_adds_nothing` |
| 7. Manual facts and the other producer stay out (every returned record is a member of the queried import) | `test_manual_facts_and_the_other_producer_stay_out` |
| Invalid requests refused without echoing caller values | `test_invalid_requests_are_refused_without_echoing_them` |
| 8. In-process vs local IPC vs HTTP: success, not-ready and invalid answers equal (only the query instant differs) | `test_every_transport_returns_the_same_answer[local-ipc]`, `[http]` |

## Verification

| Command | Outcome |
| --- | --- |
| `pytest tests/phase3/runtime/test_code_query.py` (host, real sockets) | **12 passed** |
| `pytest` W12 import, graph traversal, context pack build + determinism, governed resolution, code query | **282 passed** |
| `pytest` registry/authorisation pins: knowledge-search vertical, S0 mutation foundation, workspace-inspect refusals, installed MCP authority, application authorisation, S5 integrated registry, DEV-REQ-081, settings refusals | **2910 passed** after six expected pin updates (below) |
| `pytest tests/contracts tests/service_conformance` | **11990 passed, 19 skipped** |
| `pytest` CLI surface/dispatch, MCP exposure manifest, MCP real-host qualification unit tests | **1344 passed** |
| `pytest packages/omnivia-core-cli/tests packages/omnivia-core-client/tests packages/omnivia-core-mcp/tests` (includes the CLI lifecycle tests, which start a real service and so prove the registry is complete) | **3242 passed, 8 skipped, 5 failed**. The five are the known worktree-only MCP installed-setup tests (`test_mcp_installed_verification.py` x3, `test_mcp_import_job_acceptance.py`, `test_mcp_standalone_authoring_acceptance.py`): their qualification child imports the main checkout. Rerun through a throwaway venv whose `.pth` lists this worktree's trees: **22 passed** |
| Mutation probes on `test_code_query.py` (in place, `-B`, restored and re-verified) | killed: snapshot check removed (2 fail), `runtime_only` ignored (1), ambiguity picks a winner (1), `depth_bounded` never set (2), diagnostic relation-type filter dropped (1, after adding an assertion for it). Survived as equivalent under real data: the content `import_id` check and the endpoint-membership check, both already implied by 0074's member table and sealed endpoints and kept as defence in depth |
| `generate-application-contracts.py --check`, `check-application-contracts.py` | up to date; passed |
| `check-package-boundaries.py`, `check-raise-discipline.py`, `check-migration-allocations.py` | passed |
| `ruff check src packages scripts tests` | all checks passed |
| `mypy --strict packages/omnivia-core-runtime/src/omnivia_core_runtime src/omnivia_core` | no issues in 355 source files |
| `git diff --check` | clean |

The six pin updates the first registry run asked for, all counts or exact sets that move
with a new read: the read registry's exact set in three tests, the settings surface size,
and the corpus case count x three adapters (693 -> 696). The whole-repository suite was not
run for this packet.

## Limitations and concerns

- **Local owner only.** D16/D17 multi-principal endpoint confidentiality is not proven and
  not claimed; W13-B owns it together with code-context assembly.
- **No discovery.** Nothing lists applicable bindings; a client needs a W12 receipt or other
  knowledge of `(repository, snapshot, producer, profile)`.
- **Scopes are per profile.** Two profiles of one producer can both be applicable at once
  (W12's scope rule); a request bound to an older profile whose import is still active is
  answered, because that import *is* current for its scope.
- **O(workspace) per query.** The read resolves the whole governed frontier, as
  `graph.traverse` and `context_pack.build` do (see the performance note in
  `storage/code_query.py`).
- **Python emits no `references` relationship,** so Python `references` answers are empty
  of relations by producer design, with `unattributed_inbound` set.
- **Adapters remain `pending_candidate`** in the operation-traceability ledger, like every
  other operation: the live-evidence runner was not run.

## ADR / PM

Codex should record against D19-D20: `code.query` as the W13-A read (closed intents,
not-ready as `projection_unavailable` with `details.readiness`), its place in the
`knowledge_retrieval` read family, and that MCP exposure is a separate decision. W13 moves
to `PARTIAL`: the query vertical exists; W13-B context assembly and D16 do not.
