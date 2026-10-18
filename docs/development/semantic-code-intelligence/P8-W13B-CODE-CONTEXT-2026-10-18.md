# TECH-CORE-RETRIEVAL-CODE-001: W13-B code-context assembly

Recorded 2026-10-18. Revised the same day after Codex review; the repair and the evidence
it corrected are in *Review repair* below.

## Scope

The sixth and last intent of the first shipping code scope, `code.query/code_context`,
and the confidentiality rules the whole read now runs under. No new catalogue operation,
no new authorisation family, no second context authority and no second database.

| Not in this packet | Owner |
| --- | --- |
| A production caller of the W12 import seam (policy authorisation or import as an operation) | Later packet. Imports in the tests still go through the W12 storage seam directly. |
| A discovery operation listing applicable `(repository, snapshot, producer, profile)` bindings | Not decided. A caller must already know the binding it asks for. |
| MCP exposure | Still deliberately off. `code.query` stays in the unexposed partition as `read_not_allow_listed`; no existing read allow-list decision covers it. |
| Differentiated code-fact visibility *between* real principals at sub-import granularity | Bounded by the writer, not by this read. See **Limitations**. |

## Baseline

| Fact | Value |
| --- | --- |
| Checkout | `worktree-omnivia-core-tech-settings`, branch `codex/core-tech-settings-integration` |
| Baseline | `825a6a3b` (W13-A plus its review repair), clean |
| Environment | main checkout's `.venv` (CPython 3.11.15, Ruff 0.16.1, mypy) with this worktree's source trees on `PYTHONPATH` plus the OCR overlay; Node + `npm ci` for the TypeScript producer |

## What was added

| File | Change |
| --- | --- |
| `contracts/application/v1/schemas/code.schema.json` | `CodeContextSeed`, `CodeSpanContent`, `CodeArchitectureContext`, `CodeContext`; `CodeQueryCoverage.graph_expansion` (required) and `.expansion_reason`; `CodeQueryInput.seed` / `.context`; `CodeQueryResult.context`. No new published pattern: the component is a `$ref` to `ContextPackBuildInput` / `ContextPackBuildResult`. |
| `application-v1.schema.json`, `generated.py`, `generated/typescript/.../index.ts` | Four new published definitions; regenerated, additive only. |
| `src/omnivia_core/contracts/v1/semantics_code.py` (+ barrel) | `code_context` added to the closed intent set; `seed` and `context` accepted only by it and `context` required by it; every member of a seed's binding checked with the same generated guards the request's own binding gets; the task-context payload delegated whole to `validate_context_pack_build_input`. |
| `service/handlers/context_pack.py` | `context_pack_build` split into the operation and `build_task_context`, the shared authority. Four values are now the caller's: the resolution instant, `retrieval_version`, `policy_versions` and an `admit` narrowing applied to both governed partitions before the freeze. |
| `service/handlers/code.py` | The `code_context` intent; the evidence-label ACL before the walk and again at output; bidirectional and multi-seed traversal; span assembly; the architecture component. |
| `storage/code_query.py` | `CapturedSource` / `CapturedSpan`: content-addressed, verified, bounded span reads through `_captured_bytes`. |
| `storage/repository.py` | `read_evidence_labels` and `read_governed_record_labels`: the L2 half of the ACL input, folded by the same `_fold_labels`. |
| `storage/memory.py`, `service/knowledge_sharing.py` | `engineering.code` excluded from the two independent folds W12's audit named. |
| Test fixtures | `m3.link_row` and `gt.seal` take an `evidence_id` (and `normalized`), so two governed records can differ in visibility. |
| Wire corpus, pins | `code.query/primary-success` gains `graph_expansion`; new `code.query/code-context` case (232 -> 233, digest re-pinned, traceability `case_count` and the 696 -> 699 adapter-evidence pin). |
| `tests/phase3/runtime/test_code_context.py` | New, 22 tests (21 in `df9a4892`, one from the review repair). |

## Design decisions

1. **One intent on the existing read, not a seventh operation.** `code_context` is a
   `code.query` intent, so it inherits the catalogue entry, the `graph:read` scope, the
   `graph.read` capability, the `knowledge_retrieval` purpose, the local-owner read
   family, the error profile and the CLI leaf. The catalogue stays at 89 operations.
2. **One context authority, with two callers.** `handlers/context_pack.py` was *split*,
   not copied: `build_task_context` holds the six-read snapshot, the L0 grant and
   frontier, the freeze, the builder and the contract's own judge, and
   `context_pack_build` is now its first caller. A composing handler supplies only what
   the authority cannot know -- the instant, the retrieval version, the policy versions
   and the admission -- and can reach nothing else. The component a `code_context`
   returns is a real `ContextPackBuildResult`: content-addressed, carrying the frozen
   authorized-candidate-set checksum and the authority, scopes and purpose this request
   was actually granted.
3. **The component is independently authorised, and unavailable rather than silent.**
   `code.query` is granted as a graph read; the pack is the Context Pack authority's
   material. The gate is `CONTEXT_PACK_BUILD_OPERATION in context.granted_operations` --
   the session grant `authorize_application_request` already decided, never a payload
   field and never a scope string reconstructed from one. A caller without it gets
   `availability: "unavailable"`, `reason: "not_authorised"` and the whole code answer.
   Everything the authority itself refuses (missing or lagging projection, a frontier
   wider than one pack, an exhausted budget) becomes an unavailable component with that
   reason, because the code facts are true whether or not a pack can be built beside them.
4. **Supported, and not code.** The admission narrows the frozen governed partitions to
   records that are the *target* of an accepted `reconciliation.supports` relation whose
   source record and whose own relation record this caller may also see, and that are not
   `engineering.code`. An assertion nothing visibly supports is not architecture
   knowledge for that caller; similarity, prose and proximity are none of them support,
   and 0009 has no other relation that is.
5. **Visibility before the walk, for every intent.** The effective caller's
   `EvidenceLabelGrant` is evaluated against the labels of the evidence each governed
   version cites, before the subject is resolved. A definition the grant refuses leaves
   the universe, and every relation touching it goes with it -- so it cannot be a
   neighbour, a step, a degree, a limit it consumed or a `depth_bounded`. A relation the
   grant refuses is removed the same way even when both of its ends are visible. This is
   `engineering.expand`'s rule, applied to the code graph.
6. **Revalidation at output is a refusal, not a redaction.** The labels are read a second
   time after the answer exists, over every governed version it names (definitions,
   symbols, relation records, and the component's records, history and context models)
   and every evidence artifact the component cites. One member no longer visible makes
   the answer `not_found`, with the frozen message a subject that never existed gets.
   Recomputing around the gap would disclose that something was dropped and a count
   would disclose how much.
7. **A seed is identity plus the binding it was selected against.** `CodeContextSeed`
   carries the repository, snapshot, producer and profile a lexical or semantic search
   selected under, and the read compares all four with the analysis applicable now. A
   mismatch answers `resolution: "seed_not_applicable"`, `graph_expansion: "unavailable"`,
   `expansion_reason: "seed_binding_mismatch"` and discloses nothing. `graph_expansion`
   is a required coverage field on every answer, so unavailability is always stated and
   never inferred from an empty list. A seed that half resolves against the current
   analysis is `not_found` whole.
8. **Spans are content-addressed, exact and bounded.** Each span is read from the blob
   its fact's own `content_digest` names, through the same `_captured_bytes` the
   producers and the import seam use -- `O_NOFOLLOW`, re-checked through the descriptor,
   and refused unless the bytes hash to the address. The working tree is never opened and
   no absolute path is returned. A locator with a byte range slices exactly it; a
   locator without one (the Python producer records none) is read as whole lines, because
   columns are in the producer's stated basis and a basis this module guessed would
   sometimes be wrong. The ceiling is 4096 bytes, stated as `truncated` beside the span's
   full `byte_length`, and a cut landing inside a UTF-8 character steps back off it.
9. **An unresolved subject assembles no context.** Ambiguity and a stale seed both give
   `spans: []` and `architecture: unavailable / subject_not_resolved`. Choices stay
   choices; a context assembled around a subject the caller has not chosen is the
   arbitrary context the disambiguating answer exists to avoid.
10. **The two W12 bypasses are closed by structural exclusion, not by a second copy of
    0074.** `storage/memory.py`'s fold and `service/knowledge_sharing.py`'s current-version
    lookup each decide currency for themselves, and neither can decide 0074's: an import
    is retired by an activation, which changes no version id, no digest and no
    supersession row, so a share of a code fact would keep verifying long after its
    analysis stopped being applicable. Teaching both folds the applicability rule would
    put one currency decision in three places; excluding the domain keeps it in one.
    Nothing is lost: code analysis is read through `code.query`, which binds a repository,
    snapshot, producer and profile, and a memory or sharing read can bind none of those.

## Evidence (`test_code_context.py`, 22 tests, real producers, real SQLite, production dispatcher)

| Requirement | Test |
| --- | --- |
| Exact context: captured span bytes/text, same-analysis neighbours both ways, separately labelled authorised architecture component, content-addressed pack, nothing of the code domain inside it | `test_a_code_context_is_spans_same_snapshot_neighbours_and_a_labelled_component` |
| D18: an assertion with no accepted support is not architecture knowledge | `test_an_unsupported_assertion_is_not_architecture_knowledge` |
| Same-name ambiguity stays choices, with no context assembled | `test_same_name_ambiguity_stays_choices_and_assembles_no_context` |
| D16: a hidden supported claim, a hidden supporting record or a hidden support relation moves no disclosed path, depth, count, truncation flag or coverage, and leaves the pack's manifest | `test_a_hidden_endpoint_or_support_moves_no_disclosed_path_degree_or_count[arch-support/arch-claim/arch-link]` |
| D16: a hidden code definition is `not_found`, byte-identical to a subject that does not exist | `test_a_hidden_code_definition_is_not_found_and_is_never_a_neighbour` |
| D16: a hidden code endpoint is removed before the walk, not after (subtraction only, degree drops by exactly one) | `test_a_hidden_code_endpoint_is_removed_before_the_walk_not_after_it` |
| D16: a hidden code relation between two visible ends is no step, neighbour, span, limit slot, `truncated`, `depth_bounded` or degree (review repair) | `test_a_hidden_code_relation_between_visible_endpoints_is_never_walked_or_counted` |
| D16: revocation between selection and output refuses, indistinguishably from absence, after two real ACL reads | `test_revocation_between_selection_and_output_refuses_and_discloses_nothing` |
| D17: a seed of this analysis expands; the same seed against the next activated analysis does not | `test_a_seed_of_this_analysis_expands_and_an_older_one_never_does` |
| A seed naming a definition this analysis does not hold is `not_found` whole | `test_a_seed_naming_a_definition_this_analysis_does_not_hold_is_not_found` |
| D18: a real call out of a real test module is a static `code.calls`, and nothing claims a run | `test_a_reference_out_of_a_test_module_is_static_and_never_runtime_coverage` |
| D18: supported prose never becomes a code relation, in either direction | `test_supported_prose_never_becomes_a_code_relation` |
| The component needs the context authority the code read does not carry | `test_the_component_needs_the_context_authority_the_code_read_does_not_carry` |
| Requirement 8: retired, other-producer and other-profile code facts are unreachable through the memory frontier, the record-id page and the sharing lookup, after a cutover | `test_imported_code_facts_never_reach_the_memory_or_sharing_reads` |
| Only the queried import's facts are ever disclosed; the retired binding is refused | `test_only_the_queried_imports_facts_are_ever_disclosed` |
| `seed` / `context` closed to their intent; refusals quote nothing | `test_the_context_fields_are_closed_to_their_own_intent` |
| A service not serving captured source is a wiring refusal | `test_a_code_context_without_a_blob_root_is_a_wiring_refusal` |
| The span ceiling truncates and says so | `test_the_span_ceiling_truncates_and_says_so` |
| In-process vs local IPC vs HTTP: assembled context with a pack, seed-not-applicable and refusal all equal | `test_every_transport_returns_the_same_code_context[local-ipc]`, `[http]` |

The confidentiality tests run as a second real principal of this endpoint, not as
`LOCAL_PRINCIPAL`: `local_owner_label_grant` holds every label for the configured local
owner, so a hiding claim made as the owner would be a claim about a grant that admits
everything.

## Verification

| Command | Outcome |
| --- | --- |
| `pytest tests/phase3/runtime/test_code_context.py` (host, real sockets) | **21 passed** |
| `pytest` code context + code query + the three code-analysis suites + graph traverse + context pack build/determinism + governed resolution + memory family/migration/chunking + knowledge sharing + 0009 | **682 passed** |
| `pytest` engineering source/captured/preview, knowledge and evidence search verticals, application authorisation, S5 integrated registry, workspace-inspect and settings refusals, S0 mutation foundation, retrieval filter chain | **3068 passed** after the one expected pin update (adapter evidence 696 -> 699) |
| `pytest tests/contracts tests/service_conformance` | **11990 passed, 19 skipped** |
| `pytest qualification tests -k "traceability or architecture_gate or mcp"` | **1155 passed** |
| `pytest packages/omnivia-core-cli/tests packages/omnivia-core-client/tests packages/omnivia-core-mcp/tests` | **3242 passed, 8 skipped, 5 failed** -- exactly W13-A's five known worktree-only MCP installed-setup tests, whose qualification child imports the main checkout. Rerun through a throwaway venv whose `.pth` lists this worktree's trees: **22 passed** |
| Mutation probes on `test_code_context.py` (in place, `-B`, cache dropped, restored and re-verified, one at a time) | **8 killed, 0 survived**: the output revalidation removed; the pre-walk visibility filter removed; the seed binding never compared; the architecture admission dropping its support test; the architecture admission dropping its ACL test; the per-relationship deduplication removed; `storage/memory.py`'s domain exclusion neutralised; `service/knowledge_sharing.py`'s domain exclusion neutralised. The deduplication probe found a real defect while this packet was being written -- a bidirectional or multi-seed walk followed one relationship from both of its ends and returned it twice, at two depths, doubling the subject's degree -- which is fixed in `_walk` and pinned by the seed test. None of the eight probed a relation's own label with every definition admitted; that gap is the *Review repair* below |
| `generate-application-contracts.py --check`, `check-application-contracts.py` | up to date; passed |
| `check-package-boundaries.py`, `check-raise-discipline.py`, `check-migration-allocations.py` | passed |
| `ruff check src packages baseline tests scripts` | all checks passed |
| `mypy --strict packages/omnivia-core-runtime/src/omnivia_core_runtime src/omnivia_core` | no issues in 355 source files |
| `git diff --check` | clean |

The whole-repository suite was not run for this packet. These are `df9a4892`'s runs; the
repair's own are under *Review repair*.

## Review repair

One repair after Codex review, inside `_visible` and the test file.

| Defect | Repair | Proof |
| --- | --- | --- |
| `_visible` returned the graph unchanged whenever the grant admitted every definition, so a relation's own governed version was checked only when some definition was hidden. A relation citing evidence the caller may not see, between two visible ends, was walked. Unbounded, it reached the answer and the output revalidation refused the whole answer `not_found`, with either end as the subject too. Cut off by a limit, it passed revalidation and still set `truncated` and `depth_bounded`. | The shortcut is gone: every relation is filtered on both ends' admission and then on its own, whatever the definitions. Node-first order and the output revalidation are unchanged. | `test_a_hidden_code_relation_between_visible_endpoints_is_never_walked_or_counted` labels only the version of `pkg -> pkg.models.Record`. On `df9a4892` it fails: the depth-2 `code_context` of `make`, limited to the seven visible relations, returns those seven with `truncated: true` and `depth_bounded: true`. After: the unrestricted answer less that relation and `pkg` (reachable only through it), with `depth_bounded: false` and nothing truncated, at that limit and at the default one; and `pkg` resolves with no relation and no neighbour. |

Mutation probes on `test_code_context.py` (in place, `-B`, cache dropped, restored under a
SHA-256 check, one at a time): **3 killed, 0 survived**. The shortcut restored and the
relation's own check dropped are each killed only by the new test, so both survived the 21
tests above. Dropping the endpoint-membership check is killed by
`test_a_hidden_code_endpoint_is_removed_before_the_walk_not_after_it`, so node-first
filtering is still pinned.

| Command | Outcome |
| --- | --- |
| `pytest tests/phase3/runtime/test_code_context.py tests/phase3/runtime/test_code_query.py` | **34 passed** |
| `ruff check` on the two touched Python files, and on `src packages baseline tests scripts` | all checks passed |
| `mypy --strict packages/omnivia-core-runtime/src/omnivia_core_runtime src/omnivia_core` | no issues in 355 source files |
| `git diff --check` | clean |

Not re-run for the repair: the broader groups, `tests/contracts`, `tests/service_conformance`
and the CLI/client/MCP suites. `_visible` has one caller, `code.query`, and when every
relation is admitted it returns the same edges as before.

## Limitations and concerns

- **Code-fact visibility is import-granular under the current writer.** W12 links every
  accepted fact of one import to that snapshot's single sealed manifest evidence
  artifact, so in a real workspace the whole analysis is visible or none of it is. The
  mechanism in this read is per record and is exercised per record -- two tests supply
  the per-record label the writer cannot yet produce, one on a definition and one on a
  relation, and the production grant, filter and walk do the rest -- but differentiated
  visibility *between* two code definitions is not reachable from production data.
  Per-file evidence links are a W12 change, not a W13 one.
- **`EVIDENCE-LABEL-GRANT-DEFERRED` still bounds the grant itself.** `storage/retrieval.py`
  gives every label to the configured local owner and none to anyone else, so the
  evidence here is "an owner sees everything, a second principal sees what is unlabelled",
  not a differentiated production label model.
- **The architecture component needs the `evidence.search` projection.** It is the
  context authority's own requirement; a workspace whose projection is missing or behind
  gets `availability: "unavailable"` with that reason rather than a refusal.
- **Three reads, one instant, three transactions.** The code graph, the supports snapshot
  and the context authority's six-read snapshot each open their own transaction, all
  filtered at the one instant this handler reads. They agree because every one of them is
  an as-of read at that microsecond, not because they share a snapshot.
- **O(workspace) per query, now four times over** -- the code graph's frontier, the
  supports snapshot, the ACL labels before the walk and again at output. The performance
  note in `storage/code_query.py` still applies.
- **Adapters remain `pending_candidate`** in the operation-traceability ledger, like every
  other operation: the live-evidence runner was not run.
- **The `code.query/code-context` corpus case states an unavailable component**, because
  the corpus is a wire-shape fixture and a pack inside it would be a second, unverifiable
  copy of the context contract's own corpus.

## ADR / PM

Codex should record against D16-D20: the `code_context` intent and its closed shape; that
the architecture component is a real Context Pack produced by the one authority and gated
on the caller independently holding `context_pack.build`; that principal visibility is
enforced before traversal and revalidated at output, with a revocation answered as
`not_found`; that a seed carries its own binding and a mismatch disables expansion
explicitly; and that `engineering.code` is now structurally unreachable from the memory
and knowledge-sharing reads. W13 stays `PARTIAL`: all six intents ship, and the
code-graph visibility granularity ceiling, the absent binding discovery and the absent
production caller of the W12 import seam remain.
