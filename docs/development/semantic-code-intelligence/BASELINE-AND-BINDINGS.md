# TECH-CORE-RETRIEVAL-CODE-001 v1.0: baseline and bindings

Recorded 2026-10-16 for the P1 hybrid-lane slice.

## Baseline

| Fact | Value |
| --- | --- |
| Checkout | `worktree-omnivia-core-semantic-code-intelligence` (temporary worktree; the canonical Core checkout holds unrelated user work) |
| Branch | `codex/core-semantic-code-intelligence` |
| Commit | `fa660a5970f75929faf914af3415ac636504b56c` |
| Initial state | clean (`git status` empty at session start) |
| Slice state | reviewed and committed on the branch; the implementation commit contains this evidence file |

## Already-committed inventory (`c9f58915..fa660a59`)

Commits `523b4ddd` (semantic and code intelligence foundations), `10155ee7` (migration pin) and `fa660a59` (upgrade path).

| File | Lines added |
| --- | --- |
| `contracts/migrations/v1/allocations.json` | 11 |
| `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/semantic_search.py` | 101 |
| `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/code_analysis_candidates.py` | 409 |
| `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/migration_files/0069_semantic_vector_projection.sql` | 469 |
| `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/projections/semantic.py` | 1142 |
| `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/semantic_vectors.py` | 649 |
| `packages/omnivia-core-runtime/tests/phase3/runtime/test_code_analysis_candidates.py` | 206 |
| `packages/omnivia-core-runtime/tests/phase3/runtime/test_semantic_projection_lifecycle.py` | 154 |
| `packages/omnivia-core-runtime/tests/phase3/runtime/test_semantic_vectors.py` | 174 |
| `packages/omnivia-core-runtime/tests/phase3/runtime/test_working_tree_snapshot.py` | +13 / -1 |
| `tests/test_migration_allocations.py` | 2 |

## Owner and symbol bindings

| Concern | Owner module | Symbols |
| --- | --- | --- |
| Frozen authorization | `storage/retrieval.py` | `authorized_frontier`, `AuthorizedFrontier`, `ProjectedFrontier`, `ProjectedCandidate`, `rank_projected`, `candidate_set_manifest`, `normalize_query` |
| Frontier digest | `omnivia_core.contracts.v1` | `compute_authorized_candidate_set_checksum` |
| Lexical projection adapter | `storage/projections/fts.py` | `SearchProjection.project` (produces the `ProjectedFrontier`) |
| Semantic pure values, scorer, fusion | `storage/semantic_vectors.py` | `SemanticEmbeddingProfile`, `ExecutionProvenance`, `GenerationIdentity`, `SemanticRetrievalPolicy`, `ProjectedSemanticCandidate`, `ProjectedSemanticFrontier`, `SemanticHit`, `FusedHit`, `rank_semantic`, `fuse_ranked`, `decide_compatibility`, `input_digest` |
| Semantic projection | `storage/projections/semantic.py` | `build_semantic_projection`, `current_generation`, `require_current`, `project_semantic`; refusals `SemanticProjectionUnavailable`, `StaleSemanticProjection`, `IncompatibleSemanticGeneration`, `IncompleteSemanticCoverage`, `CorruptSemanticState` (new) |
| Durable rows | migration `0069` | `omnivia_semantic_profiles`, `omnivia_semantic_run_executions`, `omnivia_semantic_vectors`, `omnivia_semantic_vector_occurrences` |
| Hybrid composition (internal only) | `service/semantic_search.py` | `search_authorized_frontier`, `SemanticQuery`, `LaneOutcome`, `SemanticSearchResult`. No production caller. |
| Code candidate binding | `storage/code_analysis_candidates.py` | `CodeAnalysisBatch`, `verify_batch_binding`, `VerifiedCodeAnalysisBatch`, `to_memory_create_inputs`. Producers, import and graph activation are later work. |
| Public operations (unchanged) | application catalogue | `evidence.search`, `graph.traverse`, `context_pack.build`, governed knowledge operations |

## This slice's changed-file map

| File | Change |
| --- | --- |
| `storage/semantic_vectors.py` | `input_digest` on `ProjectedSemanticCandidate` (validated) and `SemanticHit`; `FusedHit.semantic_input_digest`; candidate budget default 50 and hard maximum 200; new `result_limit` (default 10, max 50) included in the policy digest; `fuse_ranked` refuses non-one-based or gapped semantic ranks and caps `limit` at 50 |
| `storage/projections/semantic.py` | `project_semantic` reads `o.input_digest`; missing vector raises `IncompleteSemanticCoverage` (was `SemanticProjectionUnavailable`); new `CorruptSemanticState` for a malformed stored identity or vector; module-level `decode_vector` import |
| `service/semantic_search.py` | Rewritten: modes, typed lane outcomes, overall status, same-frontier verification, internal `rank_projected`, stable reasons, result carries policy digest and frontier checksum |
| `tests/.../test_semantic_vectors.py` | Updated for `input_digest`; five new tests (RRF values, refusals, limits, full-frontier exactness, digest validation) |
| `tests/.../test_semantic_projection_lifecycle.py` | Search call moved out; asserts the projected occurrence `input_digest` |
| `tests/.../test_hybrid_semantic_search.py` | New: 16 hybrid composition tests over a real migrated workspace |
| `docs/development/semantic-code-intelligence/*` | These four evidence files |

No migration, contract, catalogue, adapter, CLI, MCP, HTTP or native file changed.

## Environment

This worktree has no `.venv`. Tests used `/Users/claytonread/Projects/omnivia-core/.venv/bin/python` (3.11.15, Ruff 0.16.1, mypy 2.3.0), with `PYTHONPATH` pointing at this worktree's source trees, as the repository's collection guard requires. Exact commands are in `WORK-STATUS.json`.

## Workflow evidence

The earlier read-only Claude exploration of this baseline finished cleanly with no diff. Its wrapper reported `failed-monitor-timeout-no-diff` because the live monitor hit ENOBUFS. That failure was in the delegation workflow, not in Core.
