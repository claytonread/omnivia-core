# OmniVia Core MCP standalone authoring and ingestion requirement traceability

**Date:** 2026-09-12

**Status:** Phase 7 record. Sections 13.A-13.G are mapped; 13.H is split; 13.I is
not yet evidenced. The feature is not declared finished by this document.

**Specification:**
`docs/development/omnivia-core-mcp-standalone-authoring-and-ingestion-requirements-2026-09-12-v1.3.md`

**Implementation plan:**
`docs/development/omnivia-core-mcp-standalone-authoring-and-ingestion-implementation-plan-2026-09-12.md`
(Phase 7, "end-to-end, security, and recovery acceptance")

**Branch:** `codex/core-mcp-completion-integration`

**Traced at:** `58ec1627c960076f00be07d3a8d741fbe2d9b05f`

**Specification baseline:** `990b0f980c633840922170976c73b8f966361eab`

**Machine check:** `tests/service_conformance/test_mcp_authoring_traceability.py`

---

## 1. What this record is, and what it is not

This is the Phase 7 deliverable the implementation plan asks for in section 6:
a traceability table keyed on the v1.3 section and rule identifiers rather than
on phase-level test names. Every row names the file that implements the rule and
the pytest node, repository script or recorded qualification that evidences it.

It is not a release sign-off. Section 17 of the specification says no product
claim may state that standalone authoring is available until every acceptance
gate in section 13 passes in the release environment, and section 13.I has not
been exercised. That statement stands unchanged.

The machine check named above parses this file and fails if a requirement id or
acceptance subsection is missing, if a repository path named here does not
exist, if a pytest node named here does not resolve to a test definition, or if
a row short of its evidence uses completion language.

---

## 2. Evidence types

A row states exactly one. The distinction is the point of the record: three of
these four prove different things, and the fourth is a human reading code.

| Type | What it means | What it does not prove |
|---|---|---|
| `AUTO` | An automated test in this source tree, run by `pytest` against the working copy and the developer virtual environment. | Nothing about the built wheels, the pinned SDK, or an installed host. |
| `WHEEL` | Offline installed-wheel qualification: `scripts/check-package-builds.sh` installs each distribution into an isolated environment with `--no-index --only-binary=:all: --find-links`, from a wheelhouse staged at the reviewed pins in `scripts/mcp-wheelhouse-constraints.txt`. | It is not an offline *acquisition* proof. That script's Phase 1 reaches the configured package index on purpose and says so in its own header; only Phase 2, the installation, is index-free. |
| `HOST` | A recorded session in which an installed host binary -- Claude Code 2.1.269 or Codex CLI 0.146.0 -- launched the server and drove it. | Nothing in this repository is one. See section 4. |
| `REVIEW` | A human read of named source, recorded in section 8 of this document. | It is not a test and does not re-run. |

---

## 3. Status vocabulary

| Status | Meaning |
|---|---|
| `green` | The cited evidence exists in this tree and covers the rule directly. |
| `partial` | Evidence exists but is indirect, at a different layer, or covers part of the rule. The row names the shortfall. |
| `pending-phase-8` | No evidence of the stated type exists yet. Phase 8 of the implementation plan owns it. |

---

## 4. Environment facts that bound every claim here

These four facts constrain what the `AUTO` rows above can be read to mean. They
are recorded once so no row has to restate them.

1. **The SDK under test is not the release pin.** The reviewed wheel closure in
   `scripts/mcp-wheelhouse-constraints.txt` pins `mcp==2.0.0` and
   `mcp-types==2.0.0`. The `.venv` every `AUTO` row was exercised in holds
   `mcp 2.2.0` and `mcp-types 2.2.0`. The MCP package declares `mcp>=2,<3` and
   no test in `packages/omnivia-core-mcp/tests` reads the constraints file. So
   every `AUTO` row is evidence about 2.2.0 behaviour, which section 10 of the
   specification explicitly refuses to accept as evidence about the pin.

2. **An SDK client is not a host.** The end-to-end journeys spawn the real
   server as a real subprocess over real pipes -- `sys.executable -m
   omnivia_core_mcp.server --config <path>` -- and drive it with the official
   Python SDK's own `stdio_client` and `ClientSession`. That is the transport a
   host uses, driven by a client that is not a host: the wire identity is
   `omnivia-core-acceptance`. The token `claude-code` appears in those modules
   only as the value of the installed command's `--host` flag. No third-party
   host binary is launched anywhere in this tree.

3. **The MCP journeys are POSIX-only.** `test_mcp_stdio_end_to_end.py`,
   `test_mcp_standalone_authoring_acceptance.py`,
   `test_mcp_import_job_acceptance.py`,
   `test_mcp_installed_verification.py` and
   `packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_local_control.py`
   each carry a module-level `skipif` on `socket.AF_UNIX`, because the local
   control transport dials a Unix domain socket. They run on the supported macOS
   baseline and are skipped on Windows. Several CLI filesystem tests carry the
   matching `os.name == "nt"` skip.

4. **Workspace creation is not an MCP call.** The standalone journey starts from
   `fixture.serving(seed=False, configure=False)`, which mints an empty migrated
   workspace through the runtime in-process and seeds no application data. The
   journey module itself imports no runtime and issues no SQL, and
   `packages/omnivia-core-mcp/tests/test_mcp_stdio_end_to_end.py::test_only_the_fixture_reaches_the_runtime`
   holds that boundary for every test module in the package.

---

## 5. Verification commands

Each group is one command. `python` below is the repository's `.venv/bin/python`.

| Group | Covers | Command |
|---|---|---|
| G1 contracts | `evidence.capture` schemas, catalogue, semantics, wire corpus | `.venv/bin/python -m pytest tests/contracts -q` |
| G1b generators | contract and MCP projection reproducibility | `.venv/bin/python scripts/check-application-contracts.py && .venv/bin/python scripts/generate-mcp-exposure-schemas.py --check` |
| G2 capture and storage | handler, identity migration, blob primitive, projection lifecycle | `.venv/bin/python -m pytest packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_source_identity_migration.py packages/omnivia-core-runtime/tests/phase3/runtime/test_blob_publication.py packages/omnivia-core-runtime/tests/phase3/runtime/test_fts_projection_lifecycle.py -q` |
| G3 authority and mutation | per-operation authorization, mutation coordinator, memory family | `.venv/bin/python -m pytest packages/omnivia-core-runtime/tests/phase3/runtime/test_application_authorization.py packages/omnivia-core-runtime/tests/phase3/runtime/test_v06_5_s0_mutation_foundation.py packages/omnivia-core-runtime/tests/phase3/runtime/test_v06_5_s2_memory_family.py -q` |
| G4 installed authority | dedicated principal, grants, intent, local control | `.venv/bin/python -m pytest packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_authority.py packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_authority_migration.py packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_local_control.py packages/omnivia-core-runtime/tests/phase3/protocol/test_local_control_codec.py -q` |
| G5 import and jobs | durable job execution and observation | `.venv/bin/python -m pytest packages/omnivia-core-runtime/tests/phase3/runtime/test_import_job_execution.py -q` |
| G6 MCP adapter | manifest, configuration, dispatch, stdio, both journeys | `.venv/bin/python -m pytest packages/omnivia-core-mcp/tests -q` |
| G7 installed CLI | `mcp configure` / `status` / `revoke` | `.venv/bin/python -m pytest packages/omnivia-core-cli/tests -q` |
| G8 this record | the traceability machine check | `.venv/bin/python -m pytest tests/service_conformance/test_mcp_authoring_traceability.py -q` |
| G9 packaging | pinned offline wheelhouse install -- Phase 8 | `PYTHON=.venv/bin/python scripts/check-package-builds.sh` |
| G10 repository gate | everything the required check runs | `./scripts/preflight` |
| G11 real host | Phase 8 -- no command exists in this repository yet | see section 9 |

---

## 6. Appendix E rules R1-R7

| ID | Rule | Implementation | Evidence | Type | Status |
|---|---|---|---|---|---|
| R1 | `evidence.capture` -- immutable L0 artifact, typed validation errors, one artifact per identity and replay | `contracts/application/v1/schemas/evidence.schema.json`, `src/omnivia_core/contracts/v1/semantics_evidence.py`, `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/handlers/evidence.py`, `packages/omnivia-core-runtime/src/omnivia_core_runtime/workspace/blob_publication.py` | `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_capture_writes_the_canonical_rows_and_a_conformant_result`, `tests/contracts/test_semantics_evidence_capture.py::test_valid_result_is_accepted`, `tests/contracts/test_operation_catalogue.py::test_every_operation_matches_its_frozen_metadata_exactly[evidence.capture]` | AUTO | green |
| R2 | Mutation replay -- same key and input returns the stored result, changed input conflicts, a fresh grant is spent on every replay | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/mutation.py`, `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/handlers/evidence.py` | `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_a_same_key_replay_is_answered_and_a_changed_body_conflicts`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_a_replay_is_re_authorized_rather_than_served_from_the_stored_answer`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_v06_5_s0_mutation_foundation.py::test_v06_5_s0_fresh_replay_grant_is_durably_consumed` | AUTO | green |
| R3 | Source identity -- exact claims reuse, disagreement is canonical `conflict`, database uniqueness for new direct submissions | `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/migration_files/0037_evidence_source_identity.sql`, `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/handlers/evidence.py` | `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_an_exact_resubmission_reuses_the_source_and_a_changed_claim_conflicts`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_source_identity_migration.py::test_0037_index_key_is_the_whole_source_identity_tuple`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_source_identity_migration.py::test_0037_refuses_a_repeated_identity_whose_nullable_members_are_null` | AUTO | green |
| R4 | Retrieval and visibility -- capture is lexically searchable before success; a proposal appears only in the candidate view | `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/projections/fts.py`, `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/handlers/evidence.py`, `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/handlers/memory.py` | `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_captured_text_is_lexically_visible_when_the_capture_reports_success`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_v06_5_s2_memory_family.py::test_s2_default_view_does_not_publish_proposed_candidate` | AUTO | green |
| R5 | Setup, grants and migration -- restricted default, explicit enable, redacted status, confirmed revoke, no ambient grant | `packages/omnivia-core-cli/src/omnivia_core_cli/mcp_admin.py`, `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/installed_mcp.py`, `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/installation_store.py`, `packages/omnivia-core-mcp/src/omnivia_core_mcp/configuration.py` | `packages/omnivia-core-cli/tests/test_mcp_administration.py::test_a_first_configure_publishes_both_halves_and_prints_a_snippet`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_authority.py::test_stored_rights_are_exactly_the_profile_and_never_a_wildcard`, `packages/omnivia-core-mcp/tests/test_mcp_configuration.py::test_mutation_enabled_true_alone_is_still_restricted` | AUTO | green |
| R6 | In-flight imports -- a committed job continues under service ownership; MCP observation stops at revoke; the owner path still observes | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/import_execution.py`, `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/handlers/jobs.py` | `packages/omnivia-core-mcp/tests/test_mcp_import_job_acceptance.py::test_a_staged_import_is_executed_observed_and_survives_revocation`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_local_control.py::test_revocation_blocks_the_next_call_and_the_same_key_replay` | AUTO | green |
| R7 | Evidence-backed proposal -- canonical source and evidence references survive on the proposed record, with no MCP-private shortcut | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/handlers/memory.py`, `packages/omnivia-core-mcp/src/omnivia_core_mcp/manifest.py` | `packages/omnivia-core-runtime/tests/phase3/runtime/test_v06_5_s2_memory_family.py::test_v06_5_s2_optional_assertion_evidence_uses_its_declared_source`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_v06_5_s2_memory_family.py::test_v06_5_s2_source_resolution_requires_null_safe_exact_lineage`, `packages/omnivia-core-mcp/tests/test_mcp_standalone_authoring_acceptance.py::test_the_standalone_authoring_journey_runs_on_an_empty_workspace` | AUTO | green |

---

## 7. Acceptance requirements, section 13

### 13.A Tool discovery and profile isolation

| Bullet | Implementation | Evidence | Type | Status |
|---|---|---|---|---|
| A-1 A fresh or upgraded default installation advertises exactly six restricted tools | `packages/omnivia-core-mcp/src/omnivia_core_mcp/manifest.py`, `packages/omnivia-core-mcp/src/omnivia_core_mcp/configuration.py` | `packages/omnivia-core-mcp/tests/test_mcp_exposure_manifest.py::test_the_two_profiles_are_exactly_six_and_eleven_tools`, `packages/omnivia-core-mcp/tests/test_mcp_exposure_manifest.py::test_restricted_is_the_safe_default_for_a_caller_that_names_no_profile`, `packages/omnivia-core-mcp/tests/test_mcp_server_authority.py::test_the_default_session_profile_is_restricted`, `packages/omnivia-core-mcp/tests/test_mcp_stdio_end_to_end.py::test_the_ceiling_alone_leaves_the_server_restricted_over_the_wire` | AUTO | green |
| A-2 An explicitly configured authoring installation advertises exactly eleven tools | `packages/omnivia-core-mcp/src/omnivia_core_mcp/manifest.py`, `packages/omnivia-core-mcp/src/omnivia_core_mcp/server.py` | `packages/omnivia-core-mcp/tests/test_mcp_exposure_manifest.py::test_the_authoring_profile_is_the_restricted_six_plus_five`, `packages/omnivia-core-mcp/tests/test_mcp_stdio_end_to_end.py::test_an_admitted_authoring_session_lists_eleven_and_calls_every_new_tool`, `packages/omnivia-core-mcp/tests/test_mcp_installed_verification.py::test_an_authoring_inventory_qualifies_and_reports_eleven_tools` | AUTO | green |
| A-3 Every excluded catalogue mutation remains unadvertised and undispatchable | `packages/omnivia-core-mcp/src/omnivia_core_mcp/manifest.py` | `packages/omnivia-core-mcp/tests/test_mcp_exposure_manifest.py::test_every_catalogue_operation_outside_the_eleven_is_unreachable`, `packages/omnivia-core-mcp/tests/test_mcp_exposure_manifest.py::test_an_unreviewed_mutating_operation_cannot_be_admitted`, `packages/omnivia-core-mcp/tests/test_mcp_exposure_manifest.py::test_no_service_lifecycle_operation_is_exposed`, `packages/omnivia-core-mcp/tests/test_mcp_stdio_end_to_end.py::test_a_name_absent_from_the_manifest_is_not_callable` | AUTO | green |
| A-4 A forged tool name, workspace identifier, purpose, capability or operation name fails without reaching a business handler | `packages/omnivia-core-mcp/src/omnivia_core_mcp/server.py` | `packages/omnivia-core-mcp/tests/test_mcp_server_authority.py::test_a_guessed_tool_name_refuses_before_the_client`, `packages/omnivia-core-mcp/tests/test_mcp_server_authority.py::test_no_tool_argument_can_restate_the_configured_authority`, `packages/omnivia-core-mcp/tests/test_mcp_server_authority.py::test_a_reserved_name_is_refused_inside_a_mutations_nested_input_too`, `packages/omnivia-core-mcp/tests/test_mcp_architecture_gates.py::test_architecture_gate_stdio_mcp_workspace_grants` | AUTO | green |
| A-5 Changing profile requires an owner or administrator action and a server restart; there is no per-call escalation | `packages/omnivia-core-mcp/src/omnivia_core_mcp/server.py`, `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/installed_mcp.py` | `packages/omnivia-core-mcp/tests/test_mcp_server_authority.py::test_connect_freezes_the_profile_it_was_admitted`, `packages/omnivia-core-mcp/tests/test_mcp_server_authority.py::test_the_profile_a_started_server_freezes`, `packages/omnivia-core-mcp/tests/test_mcp_server_authority.py::test_the_session_is_immutable`, `packages/omnivia-core-mcp/tests/test_mcp_server_authority.py::test_an_authoring_tool_is_uncallable_on_a_restricted_server`, `packages/omnivia-core-mcp/tests/test_mcp_server_authority.py::test_the_listing_does_not_vary_with_the_configured_purposes` | AUTO | green |

The specification offers "server restart **or** an equally atomic reload". This
implementation takes the restart branch: the profile is frozen at `connect` for
the life of the process and there is no reload path. `test_the_session_is_immutable`
is what holds that.

### 13.B Empty-workspace standalone journey

Every numbered step below is one assertion block inside the single journey test
`packages/omnivia-core-mcp/tests/test_mcp_standalone_authoring_acceptance.py::test_the_standalone_authoring_journey_runs_on_an_empty_workspace`,
which is therefore the evidence for all twelve. The line ranges name where each
step is decided.

| Bullet | Where in the journey | Evidence | Type | Status |
|---|---|---|---|---|
| B-1 Configure the authoring profile through the installed command | `omnivia mcp configure --host claude-code --workspace <minted> --profile authoring` run as a subprocess, lines 139-170 | `packages/omnivia-core-mcp/tests/test_mcp_standalone_authoring_acceptance.py::test_the_standalone_authoring_journey_runs_on_an_empty_workspace` | AUTO | green |
| B-2 Confirm eleven tools are visible | listing compared with `exposure_manifest("authoring")`, lines 426-429 | `packages/omnivia-core-mcp/tests/test_mcp_standalone_authoring_acceptance.py::test_the_standalone_authoring_journey_runs_on_an_empty_workspace` | AUTO | green |
| B-3 Call `evidence_capture` with a unique source id and distinctive text | lines 239-245, 287, asserted 439-444 | `packages/omnivia-core-mcp/tests/test_mcp_standalone_authoring_acceptance.py::test_the_standalone_authoring_journey_runs_on_an_empty_workspace` | AUTO | green |
| B-4 Immediately find the evidence through `evidence_search` | line 288, asserted 446-453 | `packages/omnivia-core-mcp/tests/test_mcp_standalone_authoring_acceptance.py::test_the_standalone_authoring_journey_runs_on_an_empty_workspace` | AUTO | green |
| B-5 Call `memory_create` with both `sources` and `assertion.evidence` naming that direct submission | lines 196-219, 289 | `packages/omnivia-core-mcp/tests/test_mcp_standalone_authoring_acceptance.py::test_the_standalone_authoring_journey_runs_on_an_empty_workspace` | AUTO | green |
| B-6 Prove the result is proposed-only | lines 457-467 (`authority_level`, `layer`, `governance_state`) | `packages/omnivia-core-mcp/tests/test_mcp_standalone_authoring_acceptance.py::test_the_standalone_authoring_journey_runs_on_an_empty_workspace` | AUTO | green |
| B-7 Prove default `memory_search` does not publish it | lines 476-481 | `packages/omnivia-core-mcp/tests/test_mcp_standalone_authoring_acceptance.py::test_the_standalone_authoring_journey_runs_on_an_empty_workspace` | AUTO | green |
| B-8 Prove `memory_search` with `view: "candidates"` finds it | lines 292-294, 482-490 | `packages/omnivia-core-mcp/tests/test_mcp_standalone_authoring_acceptance.py::test_the_standalone_authoring_journey_runs_on_an_empty_workspace` | AUTO | green |
| B-9 Repeat both mutations with the same key; stable results and no duplicate business rows | lines 295-296, 494-510 | `packages/omnivia-core-mcp/tests/test_mcp_standalone_authoring_acceptance.py::test_the_standalone_authoring_journey_runs_on_an_empty_workspace` | AUTO | green |
| B-10 Repeat with changed input and prove `idempotency_conflict` | lines 312-325, 512-515 | `packages/omnivia-core-mcp/tests/test_mcp_standalone_authoring_acceptance.py::test_the_standalone_authoring_journey_runs_on_an_empty_workspace` | AUTO | green |
| B-11 Close the MCP session and prove the independently owned Core service stays healthy | lines 411-412, 419-420 | `packages/omnivia-core-mcp/tests/test_mcp_standalone_authoring_acceptance.py::test_the_standalone_authoring_journey_runs_on_an_empty_workspace` | AUTO | green |
| B-12 Retain only a redacted qualification record | lines 162-170 assert the retained host snippet is a command line plus a `--config` path and nothing else; line 401-404 read only the non-secret `principal_id` | `packages/omnivia-core-mcp/tests/test_mcp_standalone_authoring_acceptance.py::test_the_standalone_authoring_journey_runs_on_an_empty_workspace`, `packages/omnivia-core-mcp/tests/test_mcp_installed_verification.py::test_no_refusal_quotes_the_path_the_workspace_or_the_bearer` | AUTO | partial |

B-12 is `partial` because the source-tree journey writes no persisted
qualification artefact for a reviewer to inspect; what it asserts is that the
one thing it does retain -- the printed host snippet -- carries no secret. The
retained-record half of this bullet belongs to the Phase 8 host record and has
no evidence here.

**No pre-seeding.** The journey opens `fixture.serving(seed=False,
configure=False)`; the fixture documents `seed=False` as returning the workspace
exactly as the installation bootstrap left it, holding no evidence, no governed
record and no job. Emptiness is read back through three tool searches before the
first write (lines 284-286, asserted 433-435), so the flag is not trusted. The
journey module imports no runtime and issues no SQL, and
`packages/omnivia-core-mcp/tests/test_mcp_stdio_end_to_end.py::test_only_the_fixture_reaches_the_runtime`
enforces that for every test module in the package. The only installed-CLI calls
in the journey are `mcp configure` and `service health --json`; neither is an
application mutation.

### 13.C Capture validation and collisions

| Bullet | Implementation | Evidence | Type | Status |
|---|---|---|---|---|
| C-1 Both content forms | `src/omnivia_core/contracts/v1/semantics_evidence.py`, `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/handlers/evidence.py` | `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_capture_admits_each_accepted_content_form[plain]`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_capture_admits_each_accepted_content_form[base64]`, `tests/contracts/test_semantics_evidence_capture.py::test_both_text_and_content_base64_present_is_rejected`, `tests/contracts/test_semantics_evidence_capture.py::test_neither_text_nor_content_base64_present_is_rejected` | AUTO | green |
| C-2 Both media types | `contracts/application/v1/schemas/evidence.schema.json` | `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_capture_admits_each_accepted_content_form[markdown]`, `tests/contracts/test_semantics_evidence_capture.py::test_invalid_media_type_is_rejected` | AUTO | green |
| C-3 UTF-8 rejection | `src/omnivia_core/contracts/v1/semantics_evidence.py`; the runtime delegates through `decode_evidence_capture_input` and so does the MCP adapter | `tests/contracts/test_semantics_evidence_capture.py::test_base64_valid_but_not_utf8_is_rejected`, `tests/contracts/test_semantics_evidence_capture.py::test_text_with_an_unpaired_surrogate_raises_contract_semantic_error` | AUTO | green |
| C-4 Strict base64 rejection | `src/omnivia_core/contracts/v1/semantics_evidence.py`, `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/handlers/evidence.py` | `tests/contracts/test_semantics_evidence_capture.py::test_malformed_base64_is_rejected`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_capture_admits_one_mebibyte_and_refuses_the_byte_after_it` | AUTO | green |
| C-5 Zero bytes | as C-4 | `tests/contracts/test_semantics_evidence_capture.py::test_empty_content_is_rejected`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_capture_admits_one_mebibyte_and_refuses_the_byte_after_it` | AUTO | green |
| C-6 Exactly 1 MiB accepted | as C-4 | `tests/contracts/test_semantics_evidence_capture.py::test_one_mebibyte_text_is_accepted`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_capture_admits_one_mebibyte_and_refuses_the_byte_after_it` | AUTO | green |
| C-7 1 MiB plus one rejected, and an encoded bound applied before allocation | as C-4 | `tests/contracts/test_semantics_evidence_capture.py::test_one_mebibyte_plus_one_text_is_rejected`, `tests/contracts/test_semantics_evidence_capture.py::test_encoded_oversize_content_base64_is_rejected_before_decoding` | AUTO | green |
| C-8 Unknown fields | closed schemas, `unevaluatedProperties: false`; the MCP call path refuses an unadvertised key | `tests/contracts/test_adapter_conformance.py::test_every_payload_object_schema_is_closed`, `packages/omnivia-core-mcp/tests/test_mcp_server_authority.py::test_an_unadvertised_key_inside_a_mutations_input_is_refused`, `packages/omnivia-core-mcp/tests/test_mcp_exposure_manifest.py::test_the_advertised_payloads_are_closed` | AUTO | green |
| C-9 Forbidden fields | `packages/omnivia-core-mcp/src/omnivia_core_mcp/server.py` reserved-argument table, applied to the wrapper and again to the nested input | `packages/omnivia-core-mcp/tests/test_mcp_server_authority.py::test_no_tool_argument_can_restate_the_configured_authority`, `packages/omnivia-core-mcp/tests/test_mcp_server_authority.py::test_a_reserved_name_is_refused_inside_a_mutations_nested_input_too`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_a_capture_cannot_reach_another_workspace` | AUTO | green |
| C-10 Exact repeat reuses the source | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/handlers/evidence.py` | `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_an_exact_resubmission_reuses_the_source_and_a_changed_claim_conflicts` | AUTO | green |
| C-11 Same identity, different bytes | as C-10 | `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_an_exact_resubmission_reuses_the_source_and_a_changed_claim_conflicts` | AUTO | green |
| C-12 Same identity, different metadata | as C-10 | `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_an_exact_resubmission_reuses_the_source_and_a_changed_claim_conflicts`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_a_changed_observed_at_conflicts_and_the_stated_one_still_reuses` | AUTO | green |
| C-13 Cross-principal collision inside one workspace | as C-10; principal identity is deliberately outside the source tuple | `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_a_second_principal_reuses_the_source_rather_than_forking_it` | AUTO | green |
| C-14 Same source id in different workspaces stays distinct | `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/migration_files/0037_evidence_source_identity.sql` | `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_source_identity_migration.py::test_0037_keeps_genuinely_different_identities_apart` | AUTO | partial |
| C-15 Legacy duplicate invariant failure | migration fails closed over colliding rows; the handler refuses a non-unique lookup with `internal_non_recoverable` | `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_source_identity_migration.py::test_0037_refuses_to_apply_over_colliding_legacy_rows`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_a_fenced_write_outside_the_handler_cannot_forge_a_second_source` | AUTO | partial |
| C-16 Inert content carrying a URL, a local path and reserved-looking JSON keys | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/handlers/evidence.py` | `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_hostile_looking_content_is_a_document_and_nothing_else`, `packages/omnivia-core-mcp/tests/test_mcp_standalone_authoring_acceptance.py::test_the_standalone_authoring_journey_runs_on_an_empty_workspace` | AUTO | partial |
| C-17 The equivalent keys supplied as actual structured fields are rejected | as C-9 | `packages/omnivia-core-mcp/tests/test_mcp_server_authority.py::test_a_reserved_name_is_refused_inside_a_mutations_nested_input_too` | AUTO | green |
| C-18 Each success asserts blob checksum, L0 disposition, exact source tuple, audit attribution, idempotency settlement and lexical retrievability | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/handlers/evidence.py`, `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/mutation.py` | `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_capture_writes_the_canonical_rows_and_a_conformant_result`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_captured_text_is_lexically_visible_when_the_capture_reports_success`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_a_same_key_replay_is_answered_and_a_changed_body_conflicts` | AUTO | partial |

Shortfalls named by the four `partial` rows above:

- **C-14** holds at the database index, where a differing `workspace_id` is shown
  to keep two otherwise identical identities apart. No test drives two
  workspaces through the capture handler itself; the handler-level neighbour,
  `test_a_capture_cannot_reach_another_workspace`, shows only that a payload
  cannot name a second workspace.
- **C-15** covers the migration half directly. The handler's own
  `len(rows) != 1` refusal has no test, because `0037` makes the state it
  refuses unreachable once applied. The branch is a belt on top of the braces,
  and it is untested.
- **C-16** asserts inertness by observed state -- the stored bytes are
  byte-identical, the workspace directory listing is unchanged, the path named
  inside the body was never created, the table named inside the body still
  exists -- rather than by instrumenting `socket`, `subprocess` or `open`. The
  specification asks for instrumentation; this is the weaker state-based form.
- **C-18** asserts audit attribution as a row count plus equality of the returned
  `audit_reference`. No test reads back the audit row's `principal_id`,
  `operation`, `purpose` or recorded authority for a capture.

### 13.D Import and job journey

Every bullet is decided inside the single journey test
`packages/omnivia-core-mcp/tests/test_mcp_import_job_acceptance.py::test_a_staged_import_is_executed_observed_and_survives_revocation`.
The runtime-side executor has its own suite.

| Bullet | Implementation | Evidence | Type | Status |
|---|---|---|---|---|
| D-1 A valid staged descriptor starts exactly one durable job | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/import_execution.py` | `packages/omnivia-core-mcp/tests/test_mcp_import_job_acceptance.py::test_a_staged_import_is_executed_observed_and_survives_revocation`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_import_job_execution.py` | AUTO | green |
| D-2 Same-key replay returns the same job and does not enqueue again | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/mutation.py` | `packages/omnivia-core-mcp/tests/test_mcp_import_job_acceptance.py::test_a_staged_import_is_executed_observed_and_survives_revocation` | AUTO | green |
| D-3 A changed descriptor under the same key conflicts | as D-2 | `packages/omnivia-core-mcp/tests/test_mcp_import_job_acceptance.py::test_a_staged_import_is_executed_observed_and_survives_revocation` | AUTO | green |
| D-4 `job_get` observes state and terminal result | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/handlers/jobs.py` | `packages/omnivia-core-mcp/tests/test_mcp_import_job_acceptance.py::test_a_staged_import_is_executed_observed_and_survives_revocation` | AUTO | green |
| D-5 `job_events` paginates an ordered, snapshot-stable event sequence | as D-4; the 1,000-event page maximum is projected from the contract | `packages/omnivia-core-mcp/tests/test_mcp_import_job_acceptance.py::test_a_staged_import_is_executed_observed_and_survives_revocation`, `packages/omnivia-core-mcp/tests/test_mcp_exposure_manifest.py::test_every_read_tool_advertises_the_canonical_input_schema_unwrapped` | AUTO | green |
| D-6 Terminal accounting is internally consistent and each created evidence item is retrievable through MCP | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/import_execution.py` | `packages/omnivia-core-mcp/tests/test_mcp_import_job_acceptance.py::test_a_staged_import_is_executed_observed_and_survives_revocation` | AUTO | green |
| D-7 The MCP principal cannot cancel or retry the job | `packages/omnivia-core-mcp/src/omnivia_core_mcp/manifest.py` | `packages/omnivia-core-mcp/tests/test_mcp_import_job_acceptance.py::test_a_staged_import_is_executed_observed_and_survives_revocation`, `packages/omnivia-core-mcp/tests/test_mcp_exposure_manifest.py::test_every_catalogue_operation_outside_the_eleven_is_unreachable` | AUTO | green |
| D-8 Revocation prevents later MCP observation but does not cancel committed service-owned work; the owner operator path can still observe it | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/installed_mcp.py`, `packages/omnivia-core-cli/src/omnivia_core_cli/mcp_admin.py` | `packages/omnivia-core-mcp/tests/test_mcp_import_job_acceptance.py::test_a_staged_import_is_executed_observed_and_survives_revocation`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_local_control.py::test_revocation_blocks_the_next_call_and_the_same_key_replay` | AUTO | green |

The revocation in D-8 is issued mid-session, between the observations that must
answer and the ones that must not, and the owner's own `omnivia job get` runs
afterwards. Staging itself is on the far side of the milestone boundary: the
handle is written by the suite's trusted fixture, and the journey module names
it without holding any path, URL or staging tool.

### 13.E Negative security matrix

Each row asserts both halves of the bullet: no business mutation, and no secret,
path or content in the refusal.

| Bullet | Implementation | Evidence | Type | Status |
|---|---|---|---|---|
| E-1 Missing and wrong workspace membership | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/authorization.py`, `packages/omnivia-core-mcp/src/omnivia_core_mcp/server.py` | `packages/omnivia-core-runtime/tests/phase3/runtime/test_application_authorization.py::test_workspace_operations_require_a_granted_workspace[evidence.capture]`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_application_authorization.py::test_workspace_specific_endpoints_require_exact_agreement[evidence.capture]`, `packages/omnivia-core-mcp/tests/test_mcp_server_authority.py::test_a_service_serving_another_workspace_is_refused_at_startup` | AUTO | green |
| E-2 Missing scope | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/authorization.py` | `packages/omnivia-core-runtime/tests/phase3/runtime/test_application_authorization.py::test_the_catalogue_required_scope_must_be_granted_not_merely_claimed[evidence.capture]`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_application_authorization.py::test_a_claimed_scope_the_session_does_not_grant_is_denied[evidence.capture]` | AUTO | green |
| E-3 Missing capability | as E-2 | `packages/omnivia-core-runtime/tests/phase3/runtime/test_application_authorization.py::test_a_capability_the_session_was_never_granted_is_refused[evidence.capture]`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_application_authorization.py::test_a_grant_below_the_catalogue_floor_is_refused[evidence.capture]` | AUTO | green |
| E-4 Wrong purpose | as E-2, plus the adapter's own pre-dispatch check | `packages/omnivia-core-runtime/tests/phase3/runtime/test_application_authorization.py::test_the_purpose_must_be_one_the_session_allows[evidence.capture]`, `packages/omnivia-core-mcp/tests/test_mcp_server_authority.py::test_a_purpose_outside_the_configuration_refuses_before_the_client`, `packages/omnivia-core-mcp/tests/test_mcp_stdio_end_to_end.py::test_a_purpose_outside_the_configuration_refuses_over_the_wire` | AUTO | green |
| E-5 Absent, expired, reused or already-spent grant | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/mutation.py` | `packages/omnivia-core-runtime/tests/phase3/runtime/test_v06_5_s0_mutation_foundation.py::test_v06_5_s0_server_issued_grant_required`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_v06_5_s0_mutation_foundation.py::test_v06_5_s0_grant_is_bound_to_canonical_request`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_v06_5_s0_mutation_foundation.py::test_v06_5_s2_replay_expiry_after_execution_write_rolls_back_grant_spend` | AUTO | partial |
| E-6 Credential mismatch | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/installed_mcp.py`, `packages/omnivia-core-mcp/src/omnivia_core_mcp/server.py` | `packages/omnivia-core-mcp/tests/test_mcp_server_authority.py::test_a_credential_this_installation_cannot_produce_fails_closed_at_startup`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_authority.py::test_a_wrong_credential_is_refused_with_one_fixed_message`, `packages/omnivia-core-mcp/tests/test_mcp_configuration.py::test_an_unsafe_credential_reference_fails_closed_before_initialization` | AUTO | green |
| E-7 Source ACL denial | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/handlers/memory.py` | `packages/omnivia-core-runtime/tests/phase3/runtime/test_v06_5_s2_memory_family.py::test_v06_5_s2_evidence_acl_precedes_claim_and_record_materialization` | AUTO | green |
| E-8 Malformed configuration | `packages/omnivia-core-mcp/src/omnivia_core_mcp/configuration.py` | `packages/omnivia-core-mcp/tests/test_mcp_configuration.py::test_malformed_documents_are_refused_without_payload_leakage`, `packages/omnivia-core-mcp/tests/test_mcp_configuration.py::test_invalid_or_authority_widening_values_are_refused`, `packages/omnivia-core-mcp/tests/test_mcp_configuration.py::test_semantic_failures_drop_sensitive_values_and_parser_context` | AUTO | green |
| E-9 Unsafe file mode | `packages/omnivia-core-client/src/omnivia_core_client/owner_private.py`, `packages/omnivia-core-mcp/src/omnivia_core_mcp/configuration.py` | `packages/omnivia-core-mcp/tests/test_mcp_configuration.py::test_posix_group_or_other_access_is_refused`, `packages/omnivia-core-mcp/tests/test_mcp_configuration.py::test_an_owner_mismatch_is_refused`, `packages/omnivia-core-mcp/tests/test_mcp_installed_verification.py::test_a_configuration_that_is_not_owner_private_is_refused` | AUTO | green |
| E-10 Symlinked config | as E-9 | `packages/omnivia-core-mcp/tests/test_mcp_configuration.py::test_a_symlink_is_refused`, `packages/omnivia-core-mcp/tests/test_mcp_configuration.py::test_a_non_regular_file_is_refused`, `packages/omnivia-core-mcp/tests/test_mcp_configuration.py::test_a_replacement_during_the_read_is_refused` | AUTO | green |
| E-11 Idempotency conflict | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/mutation.py` | `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_a_same_key_replay_is_answered_and_a_changed_body_conflicts`, `packages/omnivia-core-mcp/tests/test_mcp_standalone_authoring_acceptance.py::test_the_standalone_authoring_journey_runs_on_an_empty_workspace`, `packages/omnivia-core-mcp/tests/test_mcp_import_job_acceptance.py::test_a_staged_import_is_executed_observed_and_survives_revocation` | AUTO | green |
| E-12 Projection unavailable and stale projection | `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/projections/fts.py`, `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/handlers/evidence.py` | `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_a_projection_failure_refuses_and_the_same_key_repairs_it`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_fts_projection_lifecycle.py::test_lb_l10_an_unactivated_projection_is_unavailable_not_empty`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_fts_projection_lifecycle.py::test_lb_l11_a_search_over_a_lagging_projection_refuses_and_builds_nothing` | AUTO | green |
| E-13 Service-fence loss | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/mutation.py` | `packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_authority.py::test_a_stale_installation_authority_cannot_write`, `packages/omnivia-core-mcp/tests/test_mcp_architecture_gates.py::test_architecture_gate_clients_never_own_workspace_lease` | AUTO | partial |
| E-14 No secret, path or content in any refusal | fixed sentences throughout the adapter, the CLI and the handler | `packages/omnivia-core-mcp/tests/test_mcp_server_authority.py::test_a_contract_refusal_repeats_nothing_of_what_was_sent`, `packages/omnivia-core-mcp/tests/test_mcp_installed_verification.py::test_no_refusal_quotes_the_path_the_workspace_or_the_bearer`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_no_refusal_or_audit_record_carries_the_submitted_text`, `packages/omnivia-core-cli/tests/test_mcp_administration.py::test_no_compensation_point_discloses_anything_it_touched` | AUTO | green |

Shortfalls named by the two `partial` rows:

- **E-5** covers an absent grant, a grant bound to a different canonical request,
  and grant-spend rollback on a replay-expiry fault. A grant deliberately
  re-presented after it was already spent has no dedicated capture-path test;
  the one-grant-one-use branch in the coordinator is reached only through those
  neighbours.
- **E-13** covers fence loss on the installation-authority side and shows that
  an MCP client never takes the workspace lease. Fence loss injected mid-capture
  is not exercised.

### 13.F Atomicity and recovery

| Bullet | Implementation | Evidence | Type | Status |
|---|---|---|---|---|
| F-1 Inject failure at every durable step for each mutation and assert the documented all-or-nothing boundary | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/mutation.py`, `packages/omnivia-core-runtime/src/omnivia_core_runtime/workspace/blob_publication.py` | `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_a_capture_over_a_damaged_object_is_refused_rather_than_settled[corrupt]`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_a_projection_failure_refuses_and_the_same_key_repairs_it`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_blob_publication.py::test_a_fault_leaves_no_temporary_file_behind`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_authority.py::test_a_fault_at_a_write_boundary_persists_nothing` | AUTO | partial |
| F-2 Terminate the service after commit but before the MCP response, restart, replay the same key, assert the same canonical result | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/mutation.py` | `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_a_projection_failure_refuses_and_the_same_key_repairs_it` | AUTO | partial |
| F-3 For capture, assert projection recovery and exactly one searchable artifact | `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/projections/fts.py` | `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_a_projection_failure_refuses_and_the_same_key_repairs_it`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_fts_projection_lifecycle.py::test_lb_l4_an_interruption_at_any_phase_converges_on_the_next_build[_append_documents]`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_fts_projection_lifecycle.py::test_lb_l5_an_interruption_mid_append_resumes_from_the_last_checkpoint` | AUTO | green |
| F-4 For import, assert exactly one job and one execution chain | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/import_execution.py` | `packages/omnivia-core-mcp/tests/test_mcp_import_job_acceptance.py::test_a_staged_import_is_executed_observed_and_survives_revocation`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_import_job_execution.py` | AUTO | green |
| F-5 Concurrent identical calls | `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/migration_files/0037_evidence_source_identity.sql` | `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_a_fenced_write_outside_the_handler_cannot_forge_a_second_source`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_source_identity_migration.py::test_0037_refuses_the_repeat_in_a_live_guarded_workspace` | AUTO | partial |
| F-6 Timeout or connection loss after possible dispatch | `packages/omnivia-core-mcp/src/omnivia_core_mcp/server.py` -- no automatic retry, no new key | `packages/omnivia-core-mcp/tests/test_mcp_server_authority.py::test_a_same_key_replay_is_a_real_call_every_time`, `packages/omnivia-core-mcp/tests/test_mcp_server_authority.py::test_a_client_failure_becomes_a_readable_tool_error` | AUTO | partial |
| F-7 Deliberate response-correlation mismatch after commit | `packages/omnivia-core-mcp/src/omnivia_core_mcp/server.py`, `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/local_control.py` | `packages/omnivia-core-mcp/tests/test_mcp_server_authority.py::test_an_answer_that_does_not_correlate_is_not_published`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_local_control.py::test_an_answer_that_does_not_correlate_to_its_request_never_reaches_a_caller` | AUTO | green |
| F-8 Same-key recovery from a new MCP session | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/mutation.py` | `packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_local_control.py::test_revocation_blocks_the_next_call_and_the_same_key_replay` | AUTO | partial |

Shortfalls named by the five `partial` rows. These are the weakest part of the
Phase 7 evidence and the honest place to say so:

- **F-1** injects faults at the blob step, the projection step and the authority
  step, and at an installation write boundary. No fault is injected inside the
  capture path's own durable commit; that rollback is covered generically by the
  mutation-foundation suite rather than for `evidence.capture`.
- **F-2** shows the shape the specification asks for -- commit stands, the call
  refuses, the same key repairs it and returns the settled result -- but inside
  one process against one router. The service is never terminated and reopened.
- **F-5** holds at the database index and through a live guarded writer. No test
  dispatches two identical capture calls concurrently through the handler.
- **F-6** shows that the adapter never retries with a new key and that a client
  failure surfaces as a tool error. A transport loss injected after dispatch but
  before the response is not simulated.
- **F-8** shows that a same-key replay is re-authorized and blocked after
  revocation across calls. No test opens a second MCP session and replays a key
  minted in the first.

### 13.G Setup, upgrade, and revocation

| Bullet | Implementation | Evidence | Type | Status |
|---|---|---|---|---|
| G-1 Fresh install defaults to restricted | `packages/omnivia-core-mcp/src/omnivia_core_mcp/configuration.py` -- `mutation_enabled` absent reads as `False` | `packages/omnivia-core-mcp/tests/test_mcp_configuration.py::test_mutation_enabled_false_or_absent_is_restricted_whatever_admission_says`, `packages/omnivia-core-cli/tests/test_mcp_administration.py::test_a_restricted_configure_writes_the_restricted_ceiling_and_purposes`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_authority.py::test_restricted_policy_is_exactly_the_manifest_read_surface` | AUTO | green |
| G-2 Legacy `mutation_enabled: false` migrates to restricted without widening | as G-1; the installation migration chain carries every grant row across unchanged | `packages/omnivia-core-mcp/tests/test_mcp_configuration.py::test_mutation_enabled_false_or_absent_is_restricted_whatever_admission_says`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_authority_migration.py::test_0003_carries_every_version_two_grant_row_across_unchanged`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_authority_migration.py::test_clean_version_one_catalogue_upgrades_to_the_head` | AUTO | green |
| G-3 Legacy `mutation_enabled: true` requires explicit confirmation and never silently activates authoring | ceiling and floor are independent: `packages/omnivia-core-mcp/src/omnivia_core_mcp/configuration.py` and `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/installed_mcp.py` | `packages/omnivia-core-mcp/tests/test_mcp_configuration.py::test_mutation_enabled_true_alone_is_still_restricted`, `packages/omnivia-core-mcp/tests/test_mcp_server_authority.py::test_public_intent_alone_never_reaches_a_mutation_tool`, `packages/omnivia-core-mcp/tests/test_mcp_stdio_end_to_end.py::test_the_ceiling_alone_leaves_the_server_restricted_over_the_wire`, `packages/omnivia-core-runtime/tests/phase3/protocol/test_local_control_codec.py::test_a_configure_missing_one_argument_is_refused_not_defaulted` | AUTO | green |
| G-4 A legacy true value with a qualifying protected intent record may preserve authoring, but only when the bounded server grant independently satisfies every authorization check | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/installed_mcp.py`, `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/mcp_control.py` | `packages/omnivia-core-mcp/tests/test_mcp_configuration.py::test_authoring_needs_the_ceiling_and_the_protected_admission_together`, `packages/omnivia-core-mcp/tests/test_mcp_server_authority.py::test_admission_requires_the_protected_answer_to_name_this_very_session`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_authority.py::test_authoring_admission_requires_this_principal_and_this_workspace`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_local_control.py::test_a_forged_role_on_the_wire_is_unavailable` | AUTO | green |
| G-5 Interrupted configure either rolls back or reports a safe resumable state | `packages/omnivia-core-cli/src/omnivia_core_cli/mcp_admin.py` | `packages/omnivia-core-cli/tests/test_mcp_administration.py::test_a_failed_handshake_revokes_the_authority_and_removes_the_local_half`, `packages/omnivia-core-cli/tests/test_mcp_administration.py::test_a_failed_handshake_whose_revocation_also_fails_reports_a_recoverable_state`, `packages/omnivia-core-cli/tests/test_mcp_administration.py::test_a_credential_write_failure_revokes_before_it_discards`, `packages/omnivia-core-cli/tests/test_mcp_administration.py::test_a_configuration_write_failure_revokes_before_it_discards` | AUTO | green |
| G-6 Reconfigure rotates credentials and removes superseded authority | `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/installation_store.py` | `packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_authority.py::test_changed_configuration_rotates_and_invalidates_the_old_credential`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_authority.py::test_repeating_the_live_configuration_does_not_rotate`, `packages/omnivia-core-cli/tests/test_mcp_administration.py::test_a_profile_change_rotates_and_invalidates_the_superseded_material`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_local_control.py::test_a_rotated_bearer_stops_working_on_the_next_call` | AUTO | green |
| G-7 Revoke is idempotent and preserves data, audit, replay records and jobs | `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/installation_store.py`, `packages/omnivia-core-cli/src/omnivia_core_cli/mcp_admin.py` | `packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_authority.py::test_revoke_invalidates_immediately_and_is_idempotent`, `packages/omnivia-core-cli/tests/test_mcp_administration.py::test_revoke_is_idempotent_and_never_touches_workspace_state`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_authority.py::test_lifecycle_evidence_is_recorded_without_credential_material`, `packages/omnivia-core-mcp/tests/test_mcp_import_job_acceptance.py::test_a_staged_import_is_executed_observed_and_survives_revocation` | AUTO | green |
| G-8 Status redacts credentials, grants, paths and content | `packages/omnivia-core-cli/src/omnivia_core_cli/mcp_admin.py`, `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/mcp_control.py` | `packages/omnivia-core-cli/tests/test_mcp_administration.py::test_status_discloses_no_path_endpoint_or_secret`, `packages/omnivia-core-cli/tests/test_mcp_administration.py::test_status_reports_every_host_redacted`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_local_control.py::test_a_redacted_setup_carries_no_field_a_secret_could_be_in`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_authority.py::test_status_is_the_durable_state_and_only_the_durable_state` | AUTO | green |
| G-9 Configuration permissions and symlink defences are verified on macOS | `packages/omnivia-core-client/src/omnivia_core_client/owner_private.py` | `packages/omnivia-core-cli/tests/test_mcp_administration.py::test_the_published_configuration_is_owner_private_in_a_private_directory`, `packages/omnivia-core-cli/tests/test_mcp_administration.py::test_a_symlink_standing_at_the_configuration_path_is_replaced_not_followed`, `packages/omnivia-core-cli/tests/test_mcp_administration.py::test_a_symlinked_configuration_directory_refuses_and_compensates`, `packages/omnivia-core-cli/tests/test_mcp_administration.py::test_a_configure_over_a_substituted_store_refuses_and_writes_nothing_into_it` | AUTO | green |

G-1's "fresh install" is the configuration reader's default, not a CLI default:
`--profile` is required on `omnivia mcp configure`, so there is no unspecified
profile for the command to resolve. The default that matters is the one a server
starting on an absent or false ceiling settles at, which is what the cited
configuration tests hold.

### 13.H Shared conformance and packaging

| Bullet | Implementation | Evidence | Type | Status |
|---|---|---|---|---|
| H-1 Application-contract generation and checks | `scripts/check-application-contracts.py`, `scripts/generate-application-contracts.py` | `scripts/check-application-contracts.py`, `tests/contracts/test_operation_catalogue.py::test_the_fixture_the_annotation_and_the_generated_python_agree_exactly` | AUTO | green |
| H-2 Client, CLI and MCP conformance | `contracts/application/v1/fixtures/application-wire-adapter-conformance-v1.json`, `src/omnivia_core/contracts/v1/conformance.py` | `tests/contracts/test_adapter_conformance.py::test_every_operation_has_a_primary_success_case`, `tests/contracts/test_adapter_conformance.py::test_every_mutation_has_a_replay_and_a_conflict_case`, `packages/omnivia-core-cli/tests/test_v06_6_dispatch.py::test_each_command_dispatches_its_exact_catalogue_claims[evidence/capture]` | AUTO | green |
| H-3 Runtime migrations | `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/migration_files/0037_evidence_source_identity.sql`, `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/installation_migration_files/0003_mcp_role_grants.sql` | `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_source_identity_migration.py::test_0037_applies_cleanly_as_the_consecutive_head`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_authority_migration.py::test_fresh_catalogue_materialises_the_whole_pinned_chain`, `tests/test_migration_allocations.py` | AUTO | green |
| H-4 Generated schema projection is reproducible and clean after regeneration | `scripts/generate-mcp-exposure-schemas.py`, `packages/omnivia-core-mcp/src/omnivia_core_mcp/generated_schema_projection.py` | `packages/omnivia-core-mcp/tests/test_mcp_exposure_manifest.py::test_the_committed_projection_is_exactly_what_the_generator_emits`, `packages/omnivia-core-mcp/tests/test_mcp_exposure_manifest.py::test_the_generator_check_mode_reports_success_without_writing` | AUTO | green |
| H-5 Wheelhouse installation and offline packaging | `scripts/check-package-builds.sh`, `scripts/mcp-wheelhouse-constraints.txt` | none recorded for this branch | WHEEL | pending-phase-8 |
| H-6 Installed service smoke tests over the authoring inventory | `scripts/run-standard-journey.py`, `docs/distribution/mcp-host-interoperability.md` | none recorded; the journey script and that document still state the restricted six-tool manifest and were not revised on this branch | WHEEL | pending-phase-8 |
| H-7 The release artifact uses `mcp==2.0.0` and `mcp-types==2.0.0`, not whichever versions happen to be in a developer venv | `scripts/mcp-wheelhouse-constraints.txt` holds the reviewed pins | none recorded; every `AUTO` row above ran under SDK 2.2.0, which section 4 fact 1 states is not evidence about the pin | WHEEL | pending-phase-8 |

### 13.I Real-host qualification

No row here is green, and none may become green from anything in this tree. Fact
2 of section 4 is the reason: the journeys drive a real server subprocess with
the official SDK's own client, which is not an installed host.

| Bullet | Implementation | Evidence | Type | Status |
|---|---|---|---|---|
| I-1 Install Core from the release artifact on a clean supported macOS account | `scripts/check-package-builds.sh`, `scripts/build-standard-candidate.py` | none recorded | HOST | pending-phase-8 |
| I-2 Configure each profile using documented host settings, for Claude Code and Codex CLI | `packages/omnivia-core-cli/src/omnivia_core_cli/mcp_admin.py` emits the host snippet; `docs/distribution/mcp-host-interoperability.md` records the accepted configuration shapes | none recorded | HOST | pending-phase-8 |
| I-3 Verify initialize and tool discovery under the installed host | `packages/omnivia-core-mcp/src/omnivia_core_mcp/server.py` | none recorded | HOST | pending-phase-8 |
| I-4 Execute the empty-workspace journey under the installed host | `packages/omnivia-core-mcp/tests/test_mcp_standalone_authoring_acceptance.py` is the source-tree analogue only | none recorded | HOST | pending-phase-8 |
| I-5 Execute the import journey under the installed host | `packages/omnivia-core-mcp/tests/test_mcp_import_job_acceptance.py` is the source-tree analogue only | none recorded | HOST | pending-phase-8 |
| I-6 Exercise same-key recovery after an intentionally interrupted response | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/mutation.py` | none recorded; see also F-2 and F-8, which are short of this in the source tree too | HOST | pending-phase-8 |
| I-7 Verify stdout remains valid protocol traffic, restart the host and the Core service, and repeat observation | `packages/omnivia-core-mcp/src/omnivia_core_mcp/server.py` | none recorded under a host; the source-tree analogue is `packages/omnivia-core-mcp/tests/test_mcp_stdio_end_to_end.py::test_every_byte_the_server_writes_to_stdout_is_valid_protocol` | HOST | pending-phase-8 |
| I-8 Revoke authoring and prove mutation tools disappear or fail closed according to the documented restart model | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/installed_mcp.py` | none recorded under a host | HOST | pending-phase-8 |

---

## 8. Security review

The eight reviews Phase 7 of the implementation plan asks for. Where a review's
finding is held by a test, the row cites the test and states `AUTO`; where it is
a human reading source with no test behind it, the row states `REVIEW` and names
what was read.

| Review | Finding | Evidence | Type | Status |
|---|---|---|---|---|
| S-1 Tool inventory and schema review | The advertised surface is two frozen literal inventories, six and eleven, admitted against the canonical catalogue at import; every advertised payload is closed; the three mutation wrappers are `{input, idempotency_key}` with `additionalProperties: false`; annotations are read off the catalogue rather than asserted. | `packages/omnivia-core-mcp/tests/test_mcp_exposure_manifest.py::test_the_exposed_surface_is_exactly_the_reviewed_inventory_in_order`, `packages/omnivia-core-mcp/tests/test_mcp_exposure_manifest.py::test_the_advertised_payloads_are_closed`, `packages/omnivia-core-mcp/tests/test_mcp_exposure_manifest.py::test_the_annotations_land_where_the_requirements_say_they_must`, `packages/omnivia-core-mcp/tests/test_mcp_exposure_manifest.py::test_a_mutation_wrapper_refuses_what_it_promises_to_refuse` | AUTO | green |
| S-2 Confused-deputy review for workspace, principal and purpose derivation | No tool argument can carry workspace, principal, purpose, scope, capability, grant or credential: eighteen reserved names are refused at the wrapper and again inside the nested input. Workspace and principal come from the protected configuration and the service session; purpose and capability come from the frozen catalogue entry. The authoring admission is not asked with caller-supplied identity and must name the very session asking. | `packages/omnivia-core-mcp/tests/test_mcp_server_authority.py::test_no_tool_argument_can_restate_the_configured_authority`, `packages/omnivia-core-mcp/tests/test_mcp_server_authority.py::test_the_request_states_the_catalogue_entrys_own_authority`, `packages/omnivia-core-mcp/tests/test_mcp_server_authority.py::test_admission_requires_the_protected_answer_to_name_this_very_session`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_local_control.py::test_admission_takes_no_principal_or_workspace_from_the_caller`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_a_capture_cannot_reach_another_workspace` | AUTO | green |
| S-3 Credential and configuration filesystem review | The private document is an owner-only regular file at an explicit absolute path, read with `O_NOFOLLOW` and an inode identity pair, refused on group or other access, on an owner mismatch, on a symlink, on a non-regular file, and on replacement during the read. It is written atomically with restrictive permissions from creation. The bearer never enters host configuration, an argument vector, an environment or a stream; it lives in the installation's protected credential store. | `packages/omnivia-core-mcp/tests/test_mcp_configuration.py::test_posix_group_or_other_access_is_refused`, `packages/omnivia-core-mcp/tests/test_mcp_configuration.py::test_a_symlink_is_refused`, `packages/omnivia-core-cli/tests/test_mcp_administration.py::test_the_published_configuration_is_owner_private_in_a_private_directory`, `packages/omnivia-core-cli/tests/test_mcp_administration.py::test_no_command_ever_puts_the_bearer_in_a_stream_or_a_configuration`, `packages/omnivia-core-mcp/tests/test_mcp_server_authority.py::test_no_command_line_option_can_carry_a_credential` | AUTO | green |
| S-4 Replay-after-revocation review | Replay is re-authorized, not served from the stored answer: current authority and a fresh grant are required and the grant is durably spent on a replay attempt. A revoked credential fails the next call rather than the next restart, and blocks the same-key replay. Revocation is not cancellation: a committed import continues under the service's own identity and fencing, and the owner path still observes it. | `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_a_replay_is_re_authorized_rather_than_served_from_the_stored_answer`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_local_control.py::test_revocation_blocks_the_next_call_and_the_same_key_replay`, `packages/omnivia-core-mcp/tests/test_mcp_server_authority.py::test_a_revoked_credential_fails_the_next_tool_call_rather_than_the_next_restart`, `packages/omnivia-core-mcp/tests/test_mcp_import_job_acceptance.py::test_a_staged_import_is_executed_observed_and_survives_revocation` | AUTO | green |
| S-5 Oversized input, base64 and resource-exhaustion review | An encoded-length ceiling is applied before `b64decode` allocates, so a hostile encoded payload is refused without being decoded; the decoded bound is 1..1048576 inclusive; the configuration reader is bounded at 64 KiB with a bounded read; `job_events` pages are bounded at 1,000 events by the projected contract; the local control frame is bounded before buffering. | `tests/contracts/test_semantics_evidence_capture.py::test_encoded_oversize_content_base64_is_rejected_before_decoding`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_capture_admits_one_mebibyte_and_refuses_the_byte_after_it`, `packages/omnivia-core-mcp/tests/test_mcp_configuration.py::test_the_byte_limit_is_checked_with_a_bounded_read`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_local_control.py::test_an_oversized_control_is_refused_before_it_is_buffered` | AUTO | green |
| S-6 SQL uniqueness and concurrency review | Direct-submission identity is enforced by a unique index over the whole five-member tuple, with `COALESCE(..., X'00')` standing in for the two nullable members; SQLite's storage-class ordering and migration `0008`'s type checks together make the sentinel uncollidable with any admissible value. The migration carries no DML and fails closed over colliding legacy rows, leaving the workspace at `0036`. Installation grant rows are append-only, with UPDATE and DELETE aborted by trigger. | `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_source_identity_migration.py::test_0037_index_key_is_the_whole_source_identity_tuple`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_source_identity_migration.py::test_0037_refuses_a_repeated_identity_whose_nullable_members_are_null`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_source_identity_migration.py::test_0037_refuses_to_apply_over_colliding_legacy_rows`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_source_identity_migration.py::test_0037_adds_one_index_and_touches_nothing_else` | AUTO | partial |
| S-7 Log, audit, exception and status redaction review | Every refusal is a fixed payload-free sentence: no path, workspace, bearer, reference, service envelope or answer body. Submitted text never reaches a refusal or an audit row. The capture handler uses sentinel-then-raise so the contract's payload-quoting error is not reachable through `__context__`. Status is the durable state and only the durable state. Logging is stderr-only and stdout is protocol-only, under `redirect_stdout`. | `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_capture_vertical.py::test_no_refusal_or_audit_record_carries_the_submitted_text`, `packages/omnivia-core-mcp/tests/test_mcp_server_authority.py::test_a_contract_refusal_repeats_nothing_of_what_was_sent`, `packages/omnivia-core-mcp/tests/test_mcp_stdio_end_to_end.py::test_every_byte_the_server_writes_to_stdout_is_valid_protocol`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_installed_mcp_authority.py::test_lifecycle_evidence_is_recorded_without_credential_material` | AUTO | partial |
| S-8 Apple privacy separation | Running the local Core service or the stdio MCP server needs no Apple privacy entitlement. Source read: `packages/omnivia-core-mcp/src/omnivia_core_mcp/server.py`, `packages/omnivia-core-mcp/src/omnivia_core_mcp/manifest.py` and the generated projection contain no protected-location access and no tool input accepting a filesystem path, URL or parser choice; the MCP distribution declares only `omnivia-core`, `omnivia-core-client` and `mcp`; the one path-reading lane, `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/source_capture.py`, is maintenance-only and is reachable from no exposed tool. `apps/core-status-menu-macos` is the only Apple-framework component and is not on this path. | `packages/omnivia-core-mcp/tests/test_mcp_architecture_gates.py::test_architecture_gate_base_mcp_no_dev_dependency`, `packages/omnivia-core-mcp/tests/test_mcp_exposure_manifest.py::test_production_mcp_imports_neither_the_runtime_nor_the_cli`, `packages/omnivia-core-runtime/tests/phase3/runtime/test_source_capture.py::test_legacy_publish_blob_facade_raises_source_capture_refused` | REVIEW | green |

Shortfalls named by the two `partial` review rows:

- **S-6** reviewed concurrency at the database boundary and through a live
  guarded writer. It did not exercise two concurrent handler dispatches, which
  is the same shortfall F-5 records.
- **S-7** reviewed audit content by what is written and by the absence of
  submitted text. It did not read back a capture's audit row to confirm the
  recorded principal, operation, purpose and authority, which is the shortfall
  C-18 records.

---

## 9. What Phase 8 still owns

Everything in this list is `pending-phase-8` above. Nothing here has been
exercised on this branch, and no row of this document should be read as saying
otherwise.

1. Build against the reviewed pins and install from the wheelhouse:
   `PYTHON=.venv/bin/python scripts/check-package-builds.sh`. Record that its
   Phase 1 acquisition reaches the index and only its Phase 2 installation is
   index-free. (H-5, H-7)
2. Extend `scripts/run-standard-journey.py` and
   `docs/distribution/mcp-host-interoperability.md` past the restricted six-tool
   manifest, so the installed smoke covers the authoring inventory. (H-6)
3. Run the empty-workspace and import journeys against installed Claude Code
   2.1.269 and Codex CLI 0.146.0 on the macOS 26.5.2 arm64 baseline, or the
   approved release replacements recorded with the results, and retain a
   redacted record of discovery, capture and search, proposed-memory creation,
   import observation, restart and revocation. (I-1 through I-8, and B-12)
4. Close the source-tree recovery shortfalls this record names -- a fault inside
   the capture commit itself (F-1), a real service restart between commit and
   replay (F-2), concurrent identical dispatches (F-5, S-6), a transport loss
   after dispatch (F-6), a same-key replay from a second session (F-8) -- or
   record a decision that the host journey is where they are exercised instead.
5. Close the smaller Phase 7 gaps: two workspaces through the capture handler
   (C-14), instrumented rather than state-based inertness (C-16), audit-row
   read-back for a capture (C-18, S-7), an already-spent grant re-presented
   (E-5), fence loss injected mid-capture (E-13).
