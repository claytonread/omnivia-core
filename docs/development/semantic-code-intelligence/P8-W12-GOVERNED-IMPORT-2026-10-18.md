# TECH-CORE-RETRIEVAL-CODE-001: P8 governed code-analysis import and applicability (W12)

Recorded 2026-10-18.

## Scope: the import/activation authority seam only

This slice delivers work item **W12**: one storage seam that takes a
`VerifiedCodeAnalysisBatch` from either real P7 producer, plus an explicit, versioned,
owner-authorised acceptance policy, and stages then atomically activates it as the
applicable governed code graph for exactly one workspace / repository / snapshot /
producer / profile. It is *only* that seam:

| Still open | Why it is open |
| --- | --- |
| **No production caller** | No catalogue operation, handler, scheduler, CLI, MCP or HTTP surface calls `import_code_analysis` or `register_import_policy`. Owner policy authorisation has no public intent yet; the seam is shaped to be the `mutate` callback of `execute_mutation` when W13 adds one (see decision 10). |
| **W13 public query intents** | Code facts are reachable only through `read_graph_snapshot` and the `storage/governed.py` resolver. See *Limitations*. |
| **QP-05 shipped transport**, W14-W16 | Untouched. |

`QUALIFICATION-PLAN.json` is unchanged. QP-04 stays `NOT_RUN`, and G8 is not claimed: both
need W13 intents and shipped transport, which are later packets.

## Baseline

| Fact | Value |
| --- | --- |
| Checkout | `worktree-omnivia-core-tech-settings` |
| Branch | `codex/core-tech-settings-integration` |
| Baseline commit | `7fb25718` (W11 TypeScript producer) |
| Environment | main checkout's `.venv` (CPython 3.11.15, Ruff 0.16.1, mypy) with this worktree's seven source trees on `PYTHONPATH` plus the OCR overlay; Node from `npm ci` for the TypeScript producer |

## What was added

| File | Change |
| --- | --- |
| `storage/migration_files/0074_code_analysis_applicability.sql` | New. The bounded `code.*` relation namespace and six append-only, fenced tables: import policies, imports, covered files, members, diagnostics and the activation log. |
| `storage/code_analysis_import.py` | New. `CodeAnalysisImportPolicy`, `register_import_policy(connection, settlement, *, workspace_id, principal_id, policy)`, `import_code_analysis`, `active_import_id`, `read_import_diagnostics`. |
| `storage/governed.py` | `resolve_governed_versions` drops an enrolled code version from `current_canonical` unless its import is the active one for its scope at the instant. |
| `storage/graph.py` | `read_graph_snapshot`'s watermark also covers the newest activation at or before the instant. |
| `contracts/migrations/v1/allocations.json`, `tests/test_migration_allocations.py` | 0074 allocated as a `candidate`, pinned in the follow-up commit (see *Migration discipline*). |
| `test_document_intake_migration.py`, `test_working_tree_snapshot.py` | The literal migration-head pins, 73 -> 74. |
| `tests/phase3/runtime/test_code_analysis_import.py` | New, 18 tests. |

## Architecture decisions

1. **No second authority.** Every accepted node is an ordinary 0009 `knowledge.entity` and
   every accepted edge an ordinary `knowledge.relation` with a sealed
   `omnivia_governed_relation_endpoints` row, written into 0009's tables under 0009's seal
   trigger. The human-only `memory.create` shape is not used and was not changed.
2. **The seal trigger is replaced, not weakened.** 0074 drops and recreates
   `omnivia_guard_governed_version_seals_insert`. Diffed against 0009, the only changes are
   the two relation-type lists: twelve `code.*` types join the allowlist, and they join
   `reconciliation.supports` as directed types exempt from symmetric-endpoint
   normalisation. A predecessor gate refuses the migration unless the trigger being
   replaced is 0009's unwidened text, and the refusal rolls the whole migration back.
3. **Closed vocabulary.** `RELATIONSHIPS` maps each producer's own relationship strings
   onto the twelve types, keyed by producer, and is pinned equal to each producer's
   `RELATIONSHIP_TYPES`. A string outside that map is never copied into the ontology: it
   becomes a `relationship_unmapped` diagnostic. Type-only edges share their runtime
   type and keep `type_only` and `producer_relationship` in governed content.
4. **The policy decides; the analyser does not.** There is no default policy, so
   enabling analysis records no consent. A policy locks `(producer, version, profile)`,
   lists accepted node kinds and `(relationship, strength)` pairs, and may accept only
   `syntactic` or `resolved_static`. Unresolved, ambiguous, external,
   `inferred_dynamic`, unmapped, non-accepted, endpoint-not-accepted and self-relation
   candidates become diagnostics and are never graph edges. The governed version is
   decided by `policy_evaluator` = the policy id/version. The producer appears only as the
   candidate's extractor.
5. **Nothing from the worker is trusted.** The seam rebuilds the batch through the
   candidate constructors, re-runs `verify_batch_binding` against the sealed snapshot and
   requires the same stream, sequence, manifest digest and manifest evidence, re-reads and
   re-hashes every captured blob, and checks every locator's lines and byte spans against
   those bytes, UTF-8 boundaries included.
6. **M1 lineage.** Each accepted fact is a sealed `extracted`/`deterministic` candidate
   correlated to the import's extraction run (`omnivia_governed_extraction_lineage` carries
   producer id, version and profile digest), then a sealed governed version correlated
   to the import's `m1_audit` event. Every provenance event links to the snapshot's sealed
   manifest evidence. Policy authorisation, import and each activation are their own audit
   events.
7. **Staging, then one activation.** Import, coverage manifest, members and diagnostics
   are staged, then one activation row is written. 0074's activation trigger enforces the
   CAS fence (the sequence advances by one and names the import it retires), refuses an
   activation earlier than the newest, checks the staged counts, recomputes the reduced
   coverage count, and admits `reduced_authorised` only under a policy that allows it. An
   activated import's staged set is closed to further inserts. The call runs in a
   SAVEPOINT inside the caller's fenced transaction, so a refusal rolls back only its own
   writes.
8. **Applicability is a read-time selection.** Retirement is a newer activation and never
   a deletion. A retired import's versions are neither current nor `history`, because no
   version superseded them. An as-of read at an instant when their import was active
   returns them again. Versions no import enrolled get a NULL applicability column and
   resolve exactly as before. The activation log is read in the same statement and
   transaction as the versions.
9. **Identity is content.** Import ids derive from repository, snapshot, producer,
   profile, batch digest and policy digest, and every record/version/assembly/event/seal
   id derives from the import id and candidate key. A second workspace derives byte-equal
   ids and an equal receipt. Re-activating an earlier import (rollback) writes one
   activation and one audit event, nothing else.
10. **The authoriser comes from the settlement, never from the policy.** The policy
    document is only the rules; it has no `authorised_by` field. `register_import_policy`
    takes the executor's `MutationSettlementContext` and the authenticated `principal_id`,
    exactly as `dataset_state.record_observation` and `governance.apply_governance_transition`
    do, and writes no audit event of its own: the row cites `settlement.audit_ref` at
    `settlement.settled_at_us`. 0074's insert guard refuses the row unless that audit event
    exists in the same workspace, `succeeded`, was recorded at exactly the row's instant
    and names `authorised_by` as its principal, and it refuses `core-service` as an
    authoriser outright, so the import's own service audit can never stand in for owner
    consent. In production the only writer of such an audit event is `execute_mutation`,
    which takes the principal from a server-issued `MutationGrant`; the trust boundary is
    therefore the same one every settlement-taking storage function already has. Today the
    test module is the only caller (`grep` over `src`, `packages`, `services`, `scripts`
    and `tests` finds no other reference to the module), and its `settle` helper writes the
    audit event the way the executor does before its domain callback. `import_code_analysis`
    still writes its own `core-service` audit: importing applies a policy an owner already
    authorised and records no consent, and whether W13 runs it under `execute_mutation`
    is W13's decision.

## Evidence (focused module, 18 tests)

| Property | Test |
| --- | --- |
| Real Python and TypeScript producer batches through one authority; endpoint rows are sealed governed versions | `test_python_and_typescript_batches_import_through_one_authority` |
| Exact content/source binding, `policy_evaluator` decision, candidate predecessor, extraction lineage, type-only evidence | `test_accepted_facts_carry_exact_provenance_and_source_evidence` |
| Unresolved/external/dynamic/self-relation stay diagnostics and never inflate the graph | `test_unaccepted_facts_stay_diagnostics_and_never_inflate_the_graph` |
| Unregistered policy refused with zero writes; a narrower policy narrows the graph | `test_policy_decides_and_enabling_analysis_is_not_consent` |
| The policy names no authoriser; registration is refused when the settlement's audit names another principal, was recorded at another instant, is the service's own, or does not exist, each with zero writes; an accepted row cites the settlement's audit and the seam writes no audit itself | `test_the_authoriser_is_the_settlements_principal_never_the_policys_claim` |
| Policy shape bounded to the mapped vocabulary; mapping equals producer vocabularies | `test_policy_shape_is_bounded_to_the_mapped_vocabulary` |
| Arbitrary worker relationship never reaches the ontology | `test_an_arbitrary_worker_relationship_is_never_copied_into_the_ontology` |
| Replay returns the same receipt with zero writes; reused key, stale fence, backdated activation refused; identical ids in a fresh workspace | `test_replay_returns_the_same_receipt_and_a_reused_key_or_stale_fence_is_refused` |
| Atomic cutover, deleted file retired, other producer and manual facts byte-identical, watermark, as-of history, `history`/`candidates` semantics, rollback re-activation | `test_cutover_is_atomic_isolated_and_retires_only_its_own_scope` |
| Reduced coverage refused after full staging with zero residue; admitted only under a consenting policy version | `test_a_refused_cutover_leaves_the_previous_graph_and_reduced_coverage_needs_consent` |
| Span past EOF, span splitting UTF-8, line past EOF, wrong digest, dangling endpoint forged past the constructor, incomplete capture, forged manifest evidence, unlocked profile: each refused for its own reason, zero writes | `test_malformed_or_unprovable_input_fails_closed_and_writes_nothing` |
| Requires the caller's fenced transaction; a refusal keeps the caller's own writes | `test_the_seam_requires_the_callers_fenced_transaction` |
| Fresh install: head, guarded tables, guards intact, canonical fingerprint, seal allowlist | `test_0074_is_the_head_and_adds_six_guarded_tables` |
| Upgrade from a real 0073 workspace keeps its governed graph; integrity and FK checks | `test_0074_upgrades_a_0073_workspace_and_keeps_its_governed_graph` |
| Tampered seal trigger: migration refused and rolled back whole | `test_0074_refuses_to_replace_a_tampered_seal_trigger_and_rolls_back` |
| UPDATE/DELETE refused on every table; manual-fact enrolment, closed staging, bad sequence, misstated coverage, out-of-vocabulary reason, unfenced write refused | `test_0074_tables_refuse_update_delete_and_unproven_rows` |
| The seal trigger refuses an unlisted `code.*` type even if the importer map were widened | `test_the_seal_trigger_admits_only_the_bounded_code_namespace` |
| Backup, verify, restore carry the applicable graph, watermark and active import | `test_backup_and_restore_carry_the_applicable_code_graph` |

### Recovery note: the deadlock

The previous worker's focused run hung in `pysqlite_connection_backup -> sqlite3_sleep`.
The backup test called `backup_database` while the fixture's service-owned connection
was still open, and only ran a `wal_checkpoint` first. It now closes that connection first,
the way a quiesced service does (the pattern `test_engineering_restore.py` already uses).
After that, one assertion still failed, and it was a fixture defect: the dangling-endpoint
case was built through `dataclasses.replace`, so the batch constructor refused it before
the seam ran. It is now forged past the constructor, and a separate assertion shows the
constructor refuses it too. The "inside `é`" case used byte 6 (the lead byte) and was
moved to 7. No production code changed in that recovery.

### Finalisation repairs

- **Pre-0074 compatibility.** `resolve_governed_versions` and `read_graph_snapshot` probe
  `sqlite_master` for `omnivia_code_analysis_import_members` /
  `omnivia_code_analysis_activations` and fall back to the pre-0074 query (applicability
  NULL, activation watermark 0) when the table is absent, so fixtures migrated short of
  0074 resolve exactly as before. The fallback's string literals were parenthesised for
  Ruff `ISC004`; behaviour is unchanged.
- **Policy authorisation hardening.** Decision 10. The previous draft wrote its own audit
  event with `policy.authorised_by` as the principal, which let the policy document assert
  its own authoriser.

## Verification

| Command | Outcome |
| --- | --- |
| `pytest tests/phase3/runtime/test_code_analysis_import.py -q` | **18 passed**, about 14 s |
| `pytest` over `test_governed_truth_and_relations_migration.py`, `test_governed_resolution.py`, `test_graph_traverse.py`, `test_context_pack_build.py`, `test_code_analysis_import.py` (host, not sandboxed) | **352 passed**, 0 failed, about 57 s. The six local IPC/HTTP bind refusals seen under the Codex sandbox did not occur on the host |
| `pytest test_document_intake_migration.py` and `test_working_tree_snapshot.py::test_0059_upgrade_seeds_a_preexisting_0058_capture` (the two literal head pins) | **32 passed** |
| `tests/test_migration_allocations.py`, `scripts/check-migration-allocations.py` | Run in the follow-up pin commit, which records the result in its message |
| `ruff check src packages baseline tests scripts` | All checks passed |
| `mypy --strict packages/omnivia-core-runtime/src/omnivia_core_runtime` | no issues in 196 source files |
| `check-package-boundaries.py`, `check-raise-discipline.py` | passed |
| `git diff --check` | clean |

The whole-repository suite (~46 min) was **not** run for this packet. Its inherited
baseline is the six failures recorded in `P7-TYPESCRIPT-PRODUCER-2026-10-18.md`: five MCP
installed-qualification tests that import the main checkout from a worktree, and one
Swift XCTest in `test_at09_selected_source_end_to_end.py`. None touches a file this slice
changed. No JSON contract, catalogue or generated schema changed, so no contract
regeneration was needed.

## Migration discipline

0074 is the next number after head 0073. The allocation authority pins
`introduced_commit` to the commit that carries the file's exact content, and that commit
cannot exist until the file is committed. So, as with 0070 and 0071, the packet commit
introduces the SQL, and the next commit adds the `candidate` entry pinned to it. The
pinned sha256 is the text-mode digest `Migration.checksum` computes.

## Limitations that belong to later work, not W12

- **One reader is applicability-aware.** `read_graph_snapshot` and everything routed
  through `resolve_governed_versions` / `read_governed_record_values` (context packs, the
  knowledge handler) filter imported code through the active applicability.
  `storage/memory.py`'s own `read_authorized_memory_frontier` fold and
  `service/knowledge_sharing.py`'s current lookup do not. Nothing reaches them with
  `engineering.code` records today, because there is no production caller. W13 must
  either route code reads through the resolver or teach those folds the same selection.
- **`history` is supersession history.** A retired import's facts are recovered by an
  as-of read, not by `view="history"`.
- **One batch per import.** `MAX_CANDIDATES` still binds large real repositories (the
  single `typescript.d.ts` case in the W11 record). Multi-batch imports under one
  activation are a later decision.
- **Self-relations** (direct recursion) are diagnostics, because 0009 refuses an
  endpoint row whose two records are one.

## ADR / PM

Codex should record the `code.*` namespace and the read-time applicability rule
(retirement is not supersession) against D19-D20, and update W12 here. The ledger entry
is moved only to what is proven: still `PARTIAL`, now because the seam exists but has no
production caller.
