# TECH-CORE-RETRIEVAL-CODE-001 v1.0: baseline and bindings

Recorded 2026-10-16 for the P1 hybrid-lane slice. Integrated 2026-10-18 onto the
Waves 1-4 baseline; see *Integration* below for what moved.

## Integration onto the Waves 1-4 baseline

Recorded 2026-10-18.

| Fact | Value |
| --- | --- |
| Checkout | `worktree-omnivia-core-tech-p1-p10` |
| Branch | `codex/core-tech-p1-p10-integration` |
| Integrated onto | `32c2015d2c602f82d97efa509e10a6c31763c1f6` (document intake, native, Settings and processing Waves 1-4) |
| Source commits | `523b4ddd`, `10155ee7`, `fa660a59`, `c8265c72`, cherry-picked in order |
| Shared ancestor | `c9f58915`, the head both lanes branched from |

The lane authored its migration as `0069` against `c9f58915`, where 68 was the
head. The Waves baseline had already allocated 0069 to document intake, 0070 to
app settings and 0071/0072 to capture processing, so the collision was resolved
by renumbering the semantic projection to the next free number, **0073**, rather
than by choosing a side. Nothing in the Waves lane moved.

| What moved | From | To |
| --- | --- | --- |
| Migration file | `0069_semantic_vector_projection.sql` | `0073_semantic_vector_projection.sql` |
| Allocation entry | number 69, predecessor 68 | number 73, predecessor 72 |
| `projections/semantic.SCHEMA_VERSION` | `"0069"` | `"0073"` |
| Projection ledger declared-at marker | `"0069-semantic-vector-projection"` | `"0073-semantic-vector-projection"` |

The SQL text is byte-identical, so the pinned content hash
`a21aea92...a0f6ab52` still holds; only the pathname and the allocation's
`introduced_commit` differ. Two literal migration-head pins in the Waves lane's
own tests follow the new head: the applied-versions list in
`test_working_tree_snapshot.py` gains 73 beside 70-72, and the two `max(...)`
assertions in `test_document_intake_migration.py` move from 72 to 73. No
generated contract, application catalogue or MCP document was touched in either
direction: the semantic lane adds no public operation, and the newer Waves
contracts are preserved exactly as the baseline carries them.

## Baseline (source lane, as recorded 2026-10-16)

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
| `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/migration_files/0073_semantic_vector_projection.sql` (authored as `0069_*`) | 469 |
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
| Durable rows | migration `0073` | `omnivia_semantic_profiles`, `omnivia_semantic_run_executions`, `omnivia_semantic_vectors`, `omnivia_semantic_vector_occurrences` |
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

No migration, contract, catalogue, adapter, CLI, MCP, HTTP or native file changed
*by this slice*. The migration the earlier foundations slice added was later
renumbered to 0073 by the integration above; no other file class moved.

## Environment

This worktree has no `.venv`. Tests used `/Users/claytonread/Projects/omnivia-core/.venv/bin/python` (3.11.15, Ruff 0.16.1, mypy 2.3.0), with `PYTHONPATH` pointing at this worktree's source trees, as the repository's collection guard requires. Exact commands are in `WORK-STATUS.json`.

## Workflow evidence

The earlier read-only Claude exploration of this baseline finished cleanly with no diff. Its wrapper reported `failed-monitor-timeout-no-diff` because the live monitor hit ENOBUFS. That failure was in the delegation workflow, not in Core.
