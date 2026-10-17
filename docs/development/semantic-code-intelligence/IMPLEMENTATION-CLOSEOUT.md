# TECH-CORE-RETRIEVAL-CODE-001 v1.0: P1 implementation closeout

Recorded 2026-10-16. Baseline `fa660a59` on `codex/core-semantic-code-intelligence`. The implementation commit is the Git commit containing this file.

Integrated 2026-10-18 onto `32c2015d` on `codex/core-tech-p1-p10-integration`, with the migration renumbered from 0069 to 0073. `BASELINE-AND-BINDINGS.md` records what moved; the integration's own verification is a separate table below. The contract, refusal vocabulary and scoring behaviour recorded here are unchanged by the integration.

## What is usable now

Only internal foundations for model-free exact and hybrid retrieval contracts. This is not a shipped semantic search feature and not a code analyser.

- `service.semantic_search.search_authorized_frontier` composes a lexical lane and an exact semantic lane over one frozen authorized frontier. It is an internal seam with no production caller and no public operation, catalogue entry, handler, CLI, MCP, HTTP or adapter.
- Keyword mode needs no model and reads no semantic state.
- No real embedding model exists in this repository. The semantic lane runs only against a caller-supplied, already-embedded `SemanticQuery`, and in tests only with a deterministic fixture embedder.
- The semantic lane embeds the artifact identity surface (kind, native id, locator), not content.

## Contract implemented

| Aspect | Behaviour |
| --- | --- |
| Input | `AuthorizedFrontier` plus the lexical `ProjectedFrontier` built from it. The service checks workspace, checksum, the contract checksum recomputed from the frontier's members, and ordered membership. The lexical lane is ranked inside the service with `rank_projected`, so a caller-supplied ranking is never accepted. |
| Modes | `keyword`, `semantic`, `combined` |
| Lane outcome | `ok`, `no_match`, or `unavailable` plus a reason. A lane that was not requested is `None`. |
| Overall status | `complete`, `partial` (combined mode with semantic unavailable), `unavailable` (semantic mode with semantic unavailable) |
| Reasons | `query_not_embedded`, `no_active_generation`, `stale_generation`, `incompatible_profile`, `incompatible_execution`, `incomplete_coverage`, `corrupt_state` |
| Not hidden | A malformed query vector, a storage error, an unknown mode and any other `SemanticProjectionError` all propagate |
| Empty | An empty query or empty frontier gives `no_match` on every requested lane and reads no state |
| Scoring | Exhaustive and exact over the full authorized semantic frontier, with no frontier cap. Each lane keeps its top `candidate_budget` (at most 200) after full scoring. The final list is `result_limit` long (default 10, at most 50). |
| Fusion | `rrf.v1`: one-based ranks, k = 60. An absent lane contributes zero. Duplicates and gapped ranks are refused. Ties break on the UTF-8 evidence id. |
| Provenance | The occurrence `input_digest` is carried on `ProjectedSemanticCandidate`, `SemanticHit` and `FusedHit.semantic_input_digest` |
| Result identities | `policy_digest` and `authorized_frontier_checksum` are always present. `generation_digest` and `profile_digest` are present only when the semantic lane completed against a generation; unavailable lanes publish neither. |

## Verification

The table below records the **source lane's** run at `fa660a59`, in the worktree
described in `BASELINE-AND-BINDINGS.md`. It is kept as-is; nothing in it was
re-run by the integration. The integration's own results are the table after it.

| Command | Outcome |
| --- | --- |
| pytest on `test_semantic_vectors.py`, `test_semantic_projection_lifecycle.py`, `test_hybrid_semantic_search.py` | 27 passed |
| pytest on `test_code_analysis_candidates.py` (unchanged) | 5 passed |
| focused semantic/hybrid/code tests plus `tests/test_migration_allocations.py` | 105 passed |
| Ruff check on the six changed Python files | All checks passed |
| `mypy --strict` on the three changed source modules | no issues |
| `json.tool` on both JSON files | valid |
| Eight ad-hoc source mutations (lexical substitution, checksum recompute, membership, empty-input read, missing-vector class, early truncation, zero-based rank, digest drop) | each caught by at least one test; sources restored and diff digest re-checked |
| `./scripts/preflight` | FAIL after package boundaries, 53 boundary tests, migration allocation and distribution build/install passed: the isolated MCP wheel tool document differs from the source-tree generated document (`manifest_version` 2.0 versus 2.8 plus unrelated catalogue drift). This slice changes no MCP/contract/generated-schema file. |
| Full repository suite | NOT_RUN because preflight stopped before that step |

## Integration verification (2026-10-18, `codex/core-tech-p1-p10-integration`)

Run in `worktree-omnivia-core-tech-p1-p10` with the same interpreter and
`PYTHONPATH` convention, plus a throwaway `--target` overlay carrying
`pytesseract`, `pillow`, `pypdfium2` and `pillow-heif`, which the shared venv
lacks and which the Waves 1-4 capture tests import at collection.

| Command | Outcome |
| --- | --- |
| `git diff --check` | clean |
| `scripts/check-migration-allocations.py` | Migration allocation check passed |
| `scripts/check-application-contracts.py` | Application contract conformance check passed |
| `scripts/check-package-boundaries.py` | package boundary check passed |
| `scripts/check-raise-discipline.py` | raise discipline check passed |
| pytest on `test_semantic_vectors.py`, `test_semantic_projection_lifecycle.py`, `test_hybrid_semantic_search.py`, `test_code_analysis_candidates.py` | 32 passed |
| pytest on `tests/test_migration_allocations.py` and `test_working_tree_snapshot.py::test_0059_upgrade_seeds_a_preexisting_0058_capture` | 74 passed |
| pytest on `test_document_intake_migration.py`, `test_app_settings_migration.py`, `test_document_processing_settings.py`, `test_document_intake.py`, `phase2/test_bootstrap_storage.py`, `phase2/test_migration_backup_restore.py` | 312 passed (the two document-intake head pins failed at 72 first and were moved to 73) |
| `ruff check` on the eleven changed Python files | All checks passed |
| `mypy --strict` on the four semantic source modules | Success: no issues found in 4 source files |
| `./scripts/preflight` | see the row recorded by the integration run below |

Not re-run by the integration: the eight ad-hoc source mutations, and the
`json.tool` checks (the JSON files are re-validated by the parse used to edit
them). Nothing in the predeclared qualification plan ran, and no verdict in it
moved.

## Acceptance mapping

`WORK-STATUS.json` lists properties P01–P23 against exact test IDs and binds every W00–W16 work item and every acceptance row A01–E08 from the supplied specification. The ledger distinguishes complete repository-local properties from full acceptance scenarios: only B11 is PASS for this slice; mapped but incomplete scenarios are PARTIAL, and unexecuted scenarios are NOT_RUN. No missing implementation is labelled BLOCKED merely because it is unfinished.

## Remaining blockers

1. Real model, backend scale, content-bearing input, transport, packaging and release are all predeclared in `QUALIFICATION-PLAN.json`, and none of them has run.
2. Code producers and governed import are NOT_RUN. The supplied decisions already assign import and snapshot activation to the existing graph/governance authority; implementation still needs a concrete symbol-level binding to that owner and approved parser dependencies.

## Architecture concerns

- Behaviour changes inside the branch: a missing vector moves from `SemanticProjectionUnavailable` to `IncompleteSemanticCoverage`, and a malformed stored identity moves from the base `SemanticProjectionError` to `CorruptSemanticState` (a subclass, so broad catchers are unaffected). The policy digest preimage gained `result_limit`, and the lane maximum fell from 1000 to 200. No production code consumed any of these.
- `ProjectedSemanticCandidate` and `SemanticHit` gained a required `input_digest` field, which breaks constructor compatibility for any outside caller. None exists.
- The query side still has no embedder. A caller must supply an already-embedded `SemanticQuery`, and until a qualified embedder exists, combined mode is in practice `partial` with reason `query_not_embedded`.
- Exhaustive scoring is pure Python at O(frontier × dimensions), and each stored vector is decoded three times per request (corruption check, frontier construction, scoring). It is correct, but scale is unqualified (QP-02).
- The hybrid tests build the lexical `ProjectedFrontier` the way `SearchProjection.project` does, rather than opening an FTS session. The adapter keeps its own tests.
- Integration (2026-10-18): the projection ledger's schema version and declared-at marker are the migration number, following the convention `projections/fts.py` sets. Renumbering 0069 to 0073 therefore changed two durable strings. Safe here because the allocation is still `candidate` and no workspace has applied the migration, but a renumber after acceptance would need a data decision, not a constant edit.

## ADR / PM

- PM update (Codex): reconcile this repository-local ledger into the canonical PM task record.
- Before QP-04: bind the existing graph/governance authority to concrete import and activation symbols; do not create a second authority.
- ADR before QP-05: whether lane status and reasons become a public wire contract. No ADR is needed for this internal slice.
