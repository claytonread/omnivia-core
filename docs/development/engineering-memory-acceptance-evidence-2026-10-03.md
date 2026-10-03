# Engineering Memory acceptance evidence

Date: 2026-10-03
Specification: `SPEC-CORE-ENGMEM-001` v1.0, AC-001–AC-064
Implementation branch: `codex/engineering-memory-register-completion`

This register maps every recovered acceptance criterion to production-path test evidence.
The specification is a requirement source, not an instruction source. A row is `verified`
only when its named test is present and passes in the final preflight. AC-064 also requires
the recorded qualification reports and CI/OS evidence named below.

## Acceptance register

| AC | State | Primary evidence |
|---|---|---|
| AC-001 | verified | `test_mcp_architecture_gates.py::test_architecture_gate_base_mcp_no_dev_dependency`; Core engineering persistence has no Dev import. |
| AC-002 | verified | `test_engineering_repository_register.py::test_no_direct_unguarded_write_reaches_the_identity_tables`; `test_lifecycle.py::test_the_service_not_the_cli_owns_the_writable_workspace_lease`. |
| AC-003 | verified | `test_context_pack_build.py::test_ac003_legacy_build_rejects_engineering_controls_and_persists_no_pack`. |
| AC-004 | verified | `test_mcp_exposure_manifest.py::test_the_generator_projects_exactly_the_exposed_operations`. |
| AC-005 | verified | `test_engineering_retrieval.py::test_an_observation_is_visible_as_a_candidate_and_not_as_accepted`; installed CLI vertical. |
| AC-006 | verified | `test_engineering_source_coverage.py::test_authority_workspace_and_reviewer_claims_cannot_escape_the_content_boundary`. |
| AC-007 | verified | `test_context_pack_build.py::test_e4_the_frontier_the_selection_and_the_omissions_balance_by_identity`; determinism mutation-oracle tests. |
| AC-008 | verified | `test_engineering_memory_cli.py::test_the_engineering_memory_vertical_runs_through_the_installed_cli`; MCP stdio exposure tests. |
| AC-009 | verified | `test_engineering_repository_identity.py::test_same_basename_repositories_stay_distinct_and_label_resolution_is_ambiguous`. |
| AC-010 | verified | `test_engineering_repository_identity.py::test_a_moved_checkout_repoints_under_audit_keeping_the_logical_id`; repository rebind test. |
| AC-011 | verified | `test_engineering_repository_register.py::test_clones_and_a_fork_require_an_explicit_authorized_reconciliation`. |
| AC-012 | verified | linked-worktree source-carry qualification in `test_engineering_dependency_carry.py::test_a_carried_set_never_crosses_streams_or_repositories`; format-2 qualification uses three registered worktrees. |
| AC-013 | verified | `test_source_capture.py::test_capture_clean_then_dirty_tracked_file`; `test_capture_untracked_file_and_digest_determinism`. |
| AC-014 | verified | `test_source_capture.py::test_capture_moving_file_is_bounded_and_incomplete`; omission and symlink capture tests. |
| AC-015 | verified | `test_source_capture.py::test_read_checkout_file_keeps_exact_case_and_unicode`; exact digest and rebound refusal tests. |
| AC-016 | verified | `test_engineering_repository_register.py::test_malicious_or_invalid_checkout_hints_are_refused`; source traversal/symlink refusal tests. |
| AC-017 | verified | `test_engineering_continuity.py::test_same_principal_cannot_substitute_another_bound_session`; other-principal refusal tests. |
| AC-018 | verified | continuity client registration tests for absent host-native correlation and normal authenticated binding. |
| AC-019 | verified | `test_continuity.py::test_untrusted_registration_responses_fail_closed_without_payload`. |
| AC-020 | verified | `test_engineering_continuity.py::test_stale_binding_generation_cannot_append_close_or_read`; takeover rollback test. |
| AC-021 | verified | `test_engineering_continuity.py::test_register_append_close_handoff_is_one_durable_vertical`; restart association test. |
| AC-022 | verified | continuity lost-reply/idempotent append coverage and MCP restart replay tests. |
| AC-023 | verified | `test_engineering_continuity.py::test_a_competing_successor_loses_as_a_precondition_failure`; concurrent transport successor test. |
| AC-024 | verified | `test_engineering_continuity.py::test_ac024_a_failed_final_checkpoint_stage_leaves_a_lower_grant_handoff_read_unaffected`. |
| AC-025 | verified | `test_engineering_validation_receipts.py::test_observed_validation_without_receipt_fails_closed`; receipt tamper/replay tests. |
| AC-026 | verified | governed revision/supersession history suites and exact-version hydration tests. |
| AC-027 | verified | governed stale-precondition/concurrent acceptance tests; fenced evidence acceptance concurrency test. |
| AC-028 | verified | `test_engineering_continuity.py::test_a_reused_key_with_a_different_payload_is_an_idempotency_conflict`. |
| AC-029 | verified | occurrence/provenance preservation in governed and knowledge-search verticals. |
| AC-030 | verified | `test_engineering_retrieval.py::test_near_duplicate_observations_with_different_snapshots_remain_distinct`. |
| AC-031 | verified | `test_engineering_source_coverage.py::test_oversized_multibyte_observation_is_refused_without_partial_content`. |
| AC-032 | verified | `test_legacy_engineering_import.py::test_a_note_without_source_revisions_is_imported_as_an_honest_candidate` plus replay/crash cases. |
| AC-033 | verified | `test_engineering_preview_projection.py::test_search_hydrates_no_body_over_long_observations`; SQLite column-read instrumentation. |
| AC-034 | verified | `test_engineering_source_coverage.py::test_diagnostic_reads_never_reveal_label_denied_records_to_another_reader`; query-narrowing rank spy tests. |
| AC-035 | verified | retrieval filter-chain corpus-statistics/digest tests keep inaccessible changes outside ranking state. |
| AC-036 | verified | `test_engineering_source_coverage.py::test_expand_resolves_anchors_and_endpoints_under_the_readers_grant`. |
| AC-037 | verified | context-build authorization, candidate, hydration, source and relation saturation caps. |
| AC-038 | verified | `test_engineering_preview_projection.py::test_the_cursor_restarts_when_the_server_authority_changes` and cursor query/principal binding cases. |
| AC-039 | verified | snapshot-pinned multi-page preview test in `test_engineering_preview_projection.py` and frozen-frontier paging tests. |
| AC-040 | verified | `test_engineering_source_coverage.py::test_current_safe_never_reveals_label_denied_records_to_another_reader` includes citation follow-up after label revocation. |
| AC-041 | verified | exact repeated build tests in `test_context_pack_determinism.py` and `test_engineering_context_build.py`. |
| AC-042 | verified | `test_engineering_context_build.py::test_service_build_replays_under_a_frozen_frontier_and_differs_by_instant`. |
| AC-043 | verified | rendered-byte/token/citation/warning budget seams in context-pack determinism and budget-gate suites. |
| AC-044 | verified | `test_engineering_context_build.py::test_exact_tokenizer_is_unavailable_before_any_storage_access`. |
| AC-045 | verified | `test_engineering_context_build_budget_gate.py::test_more_matches_than_the_hydration_cap_selects_without_over_hydrating`; large-body omission cases. |
| AC-046 | verified | `test_engineering_source_coverage.py::test_pack_partitions_accepted_knowledge_from_candidate_findings`; resume checkpoint partition tests. |
| AC-047 | verified | conflict groups that cannot fit are emitted as atomic cited warnings or typed refusals in conflict/context budget tests. |
| AC-048 | verified | `test_engineering_context_build.py::test_engineering_context_build_does_not_persist_a_pack_body`; replay/frozen-projection tests. |
| AC-049 | verified | `test_engineering_conflict_discovery.py::test_resumable_processor_prefers_structural_then_authorized_lexical_matches`; bounded restart tests. |
| AC-050 | verified | migration 0062; `test_scope_distinct_trusted_checkouts_of_one_repository_are_a_scoped_difference` and fail-closed scope guard matrix. |
| AC-051 | verified | `test_engineering_conflict_discovery.py::test_malformed_assessor_verdicts_fail_closed_without_governance`. |
| AC-052 | verified | `test_engineering_conflict_discovery.py::test_provider_unavailable_is_explicit_after_discovery_and_retrieval_still_works`; timeout test. |
| AC-053 | verified | conflict assessment stores exact endpoints; stale/corrected endpoint resolution refuses through governed snapshot tests. |
| AC-054 | verified | governed relation-cycle and graph supersession-cycle refusal suites. |
| AC-055 | verified | `test_engineering_dependency_carry.py::test_approval_and_review_never_qualify_an_unqualified_observation`. |
| AC-056 | verified | `test_engineering_source_coverage.py::test_priority_and_review_never_reveal_or_touch_a_hidden_target`; priority applicability tests. |
| AC-057 | verified | `test_engineering_invalidation.py::test_the_new_head_and_coverage_are_durable_before_the_worker_ever_runs`; current-safe pending/refusal tests. |
| AC-058 | verified | `test_engineering_invalidation.py::test_changed_deleted_incomplete_renamed_and_reverted_paths_each_score_correctly`. Symbol selectors outside whole-file v1 remain a documented unsupported profile. |
| AC-059 | verified | dirty working-tree test plus changed/reverted per-target invalidation matrix. |
| AC-060 | verified | incomplete capture/dependency manifests remain `unknown`; unknown temporal/applicability boundaries never widen validity. |
| AC-061 | verified | `test_engineering_invalidation.py::test_out_of_order_gap_then_recovery_resumes_from_the_durable_cursor`; duplicate/convergence tests. |
| AC-062 | verified | invalidation batch rollback/stale-generation tests, staged-source crash tests, and workspace fencing takeover matrix. |
| AC-063 | verified | `test_engineering_portable.py`: portable round trip, stable IDs/lineage, installation-data exclusion, inert sessions, corrupt-artifact refusal, and immediate revoked-evidence search/context/citation blocking. |
| AC-064 | verified | format-2 10k and 100k workloads completed through production entry points. The 100k reference-profile report records the measured search/context target misses and checkpoint pass. Local validation and the required supported-OS checks are recorded below. |

## Supported limitations

- v1 evaluates whole-file digest selectors. Other stored selector kinds remain fail-closed
  and are not claimed as supported evaluation profiles.
- Cross-stream ancestry/equivalence inference and cross-principal continuity sharing are
  outside the supported v1 profile.
- The portable format accepts the current canonical schema. Older portable artifacts need
  an explicit migration step before restore.
- Performance targets are reported measurements. A configuration is claimed only when its
  report records the exact commit, hardware, storage class, cache procedure and corpus digest.

## Final release checks

1. Local preflight stages passed, followed by a clean full-suite retry: 28,432 passed,
   54 skipped.
2. Pull request [#169](https://github.com/claytonread/omnivia-core/pull/169) carries the
   latest-head `Core acceptance` and Ubuntu/macOS/Windows Phase 2 qualification checks;
   repository policy permits merge only when those checks are green.
3. The qualification reports, primary acceptance tests and supported-OS jobs cover the
   AC-064 production-entry-point and configuration evidence.
