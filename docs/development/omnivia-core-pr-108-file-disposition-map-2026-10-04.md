# PR #108 file disposition map

**Date:** 2026-10-04
**Repository:** `claytonread/omnivia-core`
**Historical PR:** [#108](https://github.com/claytonread/omnivia-core/pull/108), closed, head `a97bdb9141aa4aa498bd899af3d2e50b21b42e3b`
**Accepted integration path:** PR #107/current `main`, with the reviewed registered-workspace restart/socket-hardening commit `2568c82` already imported as `b78e152`

## Decision

PR #108 remains closed and must not be merged or cherry-picked wholesale. Every changed path is classified below using the four dispositions required by the completion plan. “Already superseded” means the accepted PR #107/current-main implementation is authoritative, not that the PR #108 blob should be copied. “Reject” means the PR #108 version is deliberately excluded from this completion lane; any future cross-platform hardening needs its own current-main review.

Summary: 2 port, 12 rewrite for current architecture, 96 already superseded, 11 reject; 121 paths total.

## File-level map

| PR #108 path | Disposition | Basis |
|---|---|---|
| `.github/workflows/phase2-platform.yml` | reject | Do not port the PR #108 file version; it is stale evidence/release state or later divergent platform hardening outside this completion lane. |
| `CHANGELOG.md` | reject | Do not port the PR #108 file version; it is stale evidence/release state or later divergent platform hardening outside this completion lane. |
| `README.md` | rewrite for current architecture | The intent is retained only through current-main documentation or the replacement installed-wheel/real-host qualification architecture. |
| `contracts/application/v1/fixtures/application-wire-adapter-conformance-v1.json` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `contracts/application/v1/schemas/application-v1.schema.json` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `contracts/application/v1/schemas/evidence.schema.json` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `contracts/application/v1/schemas/operations.schema.json` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `contracts/migrations/v1/allocations.json` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `docs/development/omnivia-core-mcp-standalone-authoring-and-ingestion-implementation-plan-2026-09-12.md` | rewrite for current architecture | The intent is retained only through current-main documentation or the replacement installed-wheel/real-host qualification architecture. |
| `docs/development/omnivia-core-mcp-standalone-authoring-and-ingestion-requirements-2026-09-12-v1.3.md` | rewrite for current architecture | The intent is retained only through current-main documentation or the replacement installed-wheel/real-host qualification architecture. |
| `docs/development/omnivia-core-mcp-standalone-authoring-and-ingestion-requirements-2026-09-12.md` | rewrite for current architecture | The intent is retained only through current-main documentation or the replacement installed-wheel/real-host qualification architecture. |
| `docs/development/omnivia-core-mcp-standalone-authoring-and-ingestion-traceability-2026-09-12.md` | rewrite for current architecture | The intent is retained only through current-main documentation or the replacement installed-wheel/real-host qualification architecture. |
| `docs/development/omnivia-core-trusted-runtime-workspace-bootstrap-platform-handoff-2026-09-10.md` | rewrite for current architecture | The intent is retained only through current-main documentation or the replacement installed-wheel/real-host qualification architecture. |
| `docs/development/qualification-evidence/mcp-real-host-qualification.json` | reject | Do not port the PR #108 file version; it is stale evidence/release state or later divergent platform hardening outside this completion lane. |
| `docs/distribution/mcp-host-interoperability.md` | rewrite for current architecture | The intent is retained only through current-main documentation or the replacement installed-wheel/real-host qualification architecture. |
| `generated/typescript/application/v1/index.ts` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-cli/README.md` | rewrite for current architecture | The intent is retained only through current-main documentation or the replacement installed-wheel/real-host qualification architecture. |
| `packages/omnivia-core-cli/src/omnivia_core_cli/__init__.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-cli/src/omnivia_core_cli/main.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-cli/src/omnivia_core_cli/mcp_admin.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-cli/src/omnivia_core_cli/surface.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-cli/tests/test_lifecycle.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-cli/tests/test_mcp_administration.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-cli/tests/test_v06_6_dispatch.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-cli/tests/test_v06_6_main_parser.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-cli/tests/test_v06_6_surface.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-client/src/omnivia_core_client/__init__.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-client/src/omnivia_core_client/installed_credentials.py` | reject | Do not port the PR #108 file version; it is stale evidence/release state or later divergent platform hardening outside this completion lane. |
| `packages/omnivia-core-client/src/omnivia_core_client/local_control.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-client/src/omnivia_core_client/local_ipc.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-client/src/omnivia_core_client/managed_local.py` | port | Imported only through reviewed commit `2568c82` as `b78e152`; later current-tree changes remain authoritative. |
| `packages/omnivia-core-client/src/omnivia_core_client/owner_private.py` | reject | Do not port the PR #108 file version; it is stale evidence/release state or later divergent platform hardening outside this completion lane. |
| `packages/omnivia-core-client/tests/test_installed_credentials.py` | reject | Do not port the PR #108 file version; it is stale evidence/release state or later divergent platform hardening outside this completion lane. |
| `packages/omnivia-core-client/tests/test_local_control.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-client/tests/test_managed_local.py` | port | Imported only through reviewed commit `2568c82` as `b78e152`; later current-tree changes remain authoritative. |
| `packages/omnivia-core-client/tests/test_owner_private.py` | reject | Do not port the PR #108 file version; it is stale evidence/release state or later divergent platform hardening outside this completion lane. |
| `packages/omnivia-core-client/tests/test_package_isolation.py` | reject | Do not port the PR #108 file version; it is stale evidence/release state or later divergent platform hardening outside this completion lane. |
| `packages/omnivia-core-mcp/README.md` | rewrite for current architecture | The intent is retained only through current-main documentation or the replacement installed-wheel/real-host qualification architecture. |
| `packages/omnivia-core-mcp/pyproject.toml` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-mcp/src/omnivia_core_mcp/__init__.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-mcp/src/omnivia_core_mcp/configuration.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-mcp/src/omnivia_core_mcp/generated_schema_projection.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-mcp/src/omnivia_core_mcp/manifest.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-mcp/src/omnivia_core_mcp/server.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-mcp/tests/_mcp_interrupted_relay.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-mcp/tests/_mcp_stdio_probe.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-mcp/tests/_mcp_v06_3_fixture.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-mcp/tests/test_mcp_architecture_gates.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-mcp/tests/test_mcp_configuration.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-mcp/tests/test_mcp_exposure_manifest.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-mcp/tests/test_mcp_import_job_acceptance.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-mcp/tests/test_mcp_installed_verification.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-mcp/tests/test_mcp_recovery_acceptance.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-mcp/tests/test_mcp_server_authority.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-mcp/tests/test_mcp_standalone_authoring_acceptance.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-mcp/tests/test_mcp_stdio_end_to_end.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/application.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/handlers/evidence.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/import_execution.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/ingestion_coordinator.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/installation_host.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/installed_mcp.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/local_control.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/main.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/mcp_control.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/mutation.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/protocol.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/source_capture.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/transport.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/installation_migration_files/0002_mcp_principals_and_authoring_intent.sql` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/installation_migration_files/0003_mcp_role_grants.sql` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/installation_migrations.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/installation_store.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/migration_files/0041_evidence_source_identity.sql` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/projections/fts.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/src/omnivia_core_runtime/workspace/blob_publication.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/tests/phase2/test_transport_conformance.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/tests/phase3/protocol/test_local_control_codec.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/tests/phase3/runtime/test_application_authorization.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/tests/phase3/runtime/test_blob_publication.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_acceptance.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_source_identity_migration.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/tests/phase3/runtime/test_fts_projection_lifecycle.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/tests/phase3/runtime/test_import_job_execution.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_authority.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_authority_migration.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_local_control.py` | reject | Do not port the PR #108 file version; it is stale evidence/release state or later divergent platform hardening outside this completion lane. |
| `packages/omnivia-core-runtime/tests/phase3/runtime/test_source_capture.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/tests/phase3/runtime/test_t0693_migration_0036_cancellation_lineage.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/tests/phase3/runtime/test_v06_5_c1_application_admission.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/tests/phase3/runtime/test_v06_5_s0_mutation_foundation.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `packages/omnivia-core-runtime/tests/phase3/runtime/test_v06_5_s5_integrated_registry.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `scripts/build-standard-candidate.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `scripts/check-application-contracts.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `scripts/check-client-owner-private.py` | reject | Do not port the PR #108 file version; it is stale evidence/release state or later divergent platform hardening outside this completion lane. |
| `scripts/generate-mcp-exposure-schemas.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `scripts/qualification-stage-import-source.py` | rewrite for current architecture | The intent is retained only through current-main documentation or the replacement installed-wheel/real-host qualification architecture. |
| `scripts/run-host-qualification.py` | rewrite for current architecture | The intent is retained only through current-main documentation or the replacement installed-wheel/real-host qualification architecture. |
| `scripts/run-standard-journey.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `src/omnivia_core/contracts/v1/__init__.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `src/omnivia_core/contracts/v1/conformance.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `src/omnivia_core/contracts/v1/generated.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `src/omnivia_core/contracts/v1/semantics_evidence.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `tests/contracts/fixtures/operation-catalogue-v1.json` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `tests/contracts/test_adapter_conformance.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `tests/contracts/test_generated.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `tests/contracts/test_operation_catalogue.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `tests/contracts/test_runtime_contracts.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `tests/contracts/test_semantics_evidence_capture.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `tests/contracts/test_workflow_run_conformance.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `tests/fixtures/service_conformance/architecture-gate-traceability-v1.json` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `tests/fixtures/service_conformance/operation-traceability-v1.json` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `tests/package_qualification/test_standard_candidate_builder.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `tests/package_qualification/test_standard_journey.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `tests/service_conformance/test_architecture_gate_traceability.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `tests/service_conformance/test_mcp_authoring_traceability.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `tests/service_conformance/test_mcp_real_host_qualification.py` | rewrite for current architecture | The intent is retained only through current-main documentation or the replacement installed-wheel/real-host qualification architecture. |
| `tests/service_conformance/test_operation_traceability.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |
| `tests/test_core_acceptance_workflow.py` | reject | Do not port the PR #108 file version; it is stale evidence/release state or later divergent platform hardening outside this completion lane. |
| `tests/test_migration_allocations.py` | already superseded | Accepted PR #107/current-main implementation and tests are authoritative; no PR #108 file content is needed. |

## Integration guardrails

- No remaining PR #108 commit is approved for direct cherry-pick.
- The two “port” rows identify the already-completed selective import; they do not authorize another import.
- Qualification evidence from PR #108 is historical only and cannot satisfy final exact-tip Claude Code or Codex acceptance.
- Release notes remain deferred until exact-tip qualification, review, preflight, hosted checks, and the explicit release decision.
