# OmniVia Core MCP standalone authoring and ingestion implementation plan

**Date:** 2026-09-12

**Status:** ready for implementation

**Source specification:** `omnivia-core-mcp-standalone-authoring-and-ingestion-requirements-2026-09-12-v1.3.md`

**Working branch:** `codex/core-mcp-completion-integration`

**Current planning baseline:** `54edeb8`

## 1. Outcome

Implement the v1.3 specification as one contract-first Core capability with two
explicit MCP profiles:

- `restricted`, preserving the existing six read-only tools;
- `authoring`, adding `memory_create`, `evidence_capture`, `import_start`,
  `job_get`, and `job_events`.

The implementation is complete only when a newly configured, empty workspace
can be populated and queried through a real supported MCP host, while all
workspace selection, authority, audit, idempotency, persistence, projection,
and job execution remain owned by the Core service.

This plan does not create a production workspace, issue a production grant,
change a user's live Claude Code or Codex configuration, or publish a release.

## 2. Delivery principles

1. Add `evidence.capture` to the public application contract before any
   adapter exposes it.
2. Keep MCP thin. It validates the MCP wrapper and delegates to the canonical
   Core application operation; it never writes storage directly.
3. Reuse the existing mutation coordinator, authority checks, audit path, and
   idempotency ledger.
4. Fail closed. Missing or invalid authoring configuration advertises no
   authoring tools.
5. Preserve restricted behavior on upgrade unless a human owner or
   administrator explicitly enables authoring.
6. Make database-enforced identity and recovery guarantees authoritative;
   application checks may improve diagnostics but cannot replace them.
7. Keep one writable implementation lane. Contract, migration, runtime, MCP,
   setup, and qualification changes have ordering dependencies and should not
   be written concurrently in the same worktree.
8. Use the current dedicated worktree while the feature is active, reconcile
   it with the canonical checkout before integration, and retire it after the
   branch is merged.

## 3. Current repository map

### 3.1 Public contracts and generators

- `contracts/application/v1/schemas/evidence.schema.json`
- `contracts/application/v1/schemas/operations.schema.json`
- `contracts/application/v1/fixtures/application-wire-adapter-conformance-v1.json`
- `src/omnivia_core/contracts/v1/semantics_evidence.py`
- `src/omnivia_core/contracts/v1/semantics_operations.py`
- `src/omnivia_core/contracts/v1/conformance.py`
- `src/omnivia_core/contracts/v1/generated.py`
- `src/omnivia_core/contracts/v1/__init__.py`
- `scripts/generate-application-contracts.py`
- `scripts/generate-mcp-exposure-schemas.py`

The generated operation catalogue currently has 27 operations and does not
contain `evidence.capture`. Capability identifiers are carried by shared
operation metadata, so `evidence.write@1.0` belongs in that catalogue and its
generated projections rather than in an MCP-private registry.

### 3.2 Runtime and persistence

- `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/application.py`
- `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/handlers/evidence.py`
- `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/handlers/memory.py`
- `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/handlers/jobs.py`
- `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/mutation.py`
- `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/source_capture.py`
- `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/installation.py`
- `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/authorization.py`
- `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/installation_store.py`
- `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/projections/fts.py`
- `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/migration_files/0008_blobs_staged_sources_and_evidence.sql`
- `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/migration_files/0012_evidence_search_projection.sql`
- `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/migration_files/0036_workflow_control_cancellation_lineage.sql`
- `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/installation_migration_files/0001_installation_authority.sql`

The next workspace migration is expected to be `0041`, but its number must be
rechecked after rebasing immediately before implementation. The current source
identity index is not unique. The current full-text projection lifecycle is
maintenance/startup-oriented and therefore needs an explicit design decision
before synchronous capture can promise lexical search visibility.

### 3.3 MCP, CLI, client, and packaging

- `packages/omnivia-core-mcp/src/omnivia_core_mcp/manifest.py`
- `packages/omnivia-core-mcp/src/omnivia_core_mcp/configuration.py`
- `packages/omnivia-core-mcp/src/omnivia_core_mcp/server.py`
- `packages/omnivia-core-mcp/pyproject.toml`
- `packages/omnivia-core-mcp/README.md`
- `packages/omnivia-core-cli/src/omnivia_core_cli/surface.py`
- `packages/omnivia-core-cli/src/omnivia_core_cli/main.py`
- `packages/omnivia-core-client/src/omnivia_core_client/service_client.py`
- `scripts/mcp-wheelhouse-constraints.txt`

The MCP manifest is version 1.1 and admits only the six read tools. The config
already contains `mutation_enabled`, but the server does not enforce it as the
authoring exposure ceiling. The CLI has application-operation and service
lifecycle command families, but no installed MCP administration family.

### 3.4 Primary existing test targets

- `packages/omnivia-core-runtime/tests/phase3/runtime/test_source_capture.py`
- `packages/omnivia-core-runtime/tests/phase3/runtime/test_evidence_search_vertical.py`
- `packages/omnivia-core-runtime/tests/phase3/runtime/test_blobs_staged_sources_and_evidence_migration.py`
- `packages/omnivia-core-runtime/tests/phase3/runtime/test_fts_projection_migration.py`
- `packages/omnivia-core-runtime/tests/phase3/runtime/test_v06_5_s0_mutation_foundation.py`
- `packages/omnivia-core-runtime/tests/phase3/runtime/test_v06_5_c1_application_admission.py`
- `packages/omnivia-core-mcp/tests/test_mcp_exposure_manifest.py`
- `packages/omnivia-core-mcp/tests/test_mcp_configuration.py`
- `packages/omnivia-core-mcp/tests/test_mcp_server_authority.py`
- `packages/omnivia-core-mcp/tests/test_mcp_stdio_end_to_end.py`
- `packages/omnivia-core-mcp/tests/test_mcp_architecture_gates.py`
- `packages/omnivia-core-cli/tests/test_v06_6_surface.py`
- `packages/omnivia-core-cli/tests/test_v06_6_main_parser.py`
- `packages/omnivia-core-cli/tests/test_v06_6_main_execution.py`
- `packages/omnivia-core-cli/tests/test_v06_6_dispatch.py`

Extend these suites where their existing fixture boundaries fit. Add a focused
new module only when combining profile setup, runtime capture, and recovery in
an existing test file would obscure the acceptance journey.

## 4. Design gates

These decisions must be recorded before their dependent implementation starts.

### Gate A: synchronous lexical-search availability

Trace the current `build_search_projection`, projection ledger/checkpoint,
service startup, and `open_search_projection` paths and document their
transaction boundaries. Select the smallest design that satisfies all of the
following:

- a successful `evidence.capture` is lexically searchable before its success
  response is returned;
- a crash after the durable business commit but before projection publication
  is recoverable by replaying the same idempotency key;
- the service never reports success while the evidence remains unavailable to
  `evidence.search`;
- search does not fall back to scanning primary evidence tables;
- a temporary FTS mutation is not represented as a durable guarantee merely
  because it occurred inside a database transaction.

The expected shape is a service-owned projection publication/recovery step
after the business commit, with same-key replay completing that step before
returning the stored canonical result. If repository exploration disproves
that design, record the alternative and its crash proof before coding.

### Gate B: installed MCP authority and protected intent

Trace the installation authority database, session creation, credential
handling, and local owner/admin authorization path. Decide whether the existing
installation store can represent dedicated MCP principals and protected
authoring intent or whether a narrowly scoped installation migration is
required. The design must prove that:

- configure is authorized by a human owner or administrator;
- the model cannot create, expand, renew, or inspect its own grants;
- the server obtains a fresh grant for each mutation attempt and replay;
- revocation blocks new calls and same-key replays immediately;
- workspace and principal identity come only from protected configuration and
  the service session;
- host configuration contains no credential material;
- upgrade preserves restricted exposure unless explicit protected authoring
  intent is present.

An installation migration such as
`0002_mcp_principals_and_authoring_intent.sql` is expected only if the existing
authority model cannot meet those invariants. Confirm the migration head and
schema need first.

## 5. Implementation sequence

### Phase 0: rebase, baseline, and decision records

**Purpose:** make the implementation start from a known repository state and
close Gates A and B.

Work:

1. Reconcile the branch with its intended integration base and confirm the
   working tree is clean.
2. Recheck application and installation migration heads.
3. Record the Python, MCP SDK, package-lock, and wheelhouse state. The release
   path must qualify `mcp==2.0.0` and `mcp-types==2.0.0`, even if a developer
   environment contains a newer SDK.
4. Run the existing contract generators in check mode, MCP tests, runtime
   mutation/evidence/search tests, CLI tests, and the repository preflight.
5. Complete Gate A and Gate B as short design notes in the eventual
   implementation PR or in a dedicated development note if the decisions need
   durable explanation.
6. Split each subsequent phase into a bounded implementation packet with exact
   acceptance commands.

Exit criteria:

- baseline checks pass or every pre-existing failure is documented;
- projection publication/recovery has an agreed transaction and replay model;
- installed MCP principal/grant storage has an agreed authority model;
- migration numbers and pinned dependency versions are confirmed.

Suggested commit: no product commit unless a durable design note is added.

### Phase 1: canonical application contract

**Purpose:** make `evidence.capture` a first-class provider-neutral Core
operation.

Work:

1. Add `EvidenceCaptureInput`, `EvidenceCaptureResult`, direct-submission
   `SourceReference`, media-type, size, timestamp, and exactly-one-content-field
   constraints to `evidence.schema.json`.
2. Add the `evidence.capture` operation to `operations.schema.json` with:
   `memory:write`, `evidence.write@1.0`, `content_ingestion`, mutation audit,
   synchronous completion, required idempotency, no mutation precondition, and
   the exact allowed error set from v1.3.
3. Encode semantic checks that JSON Schema cannot express safely, including
   strict base64 UTF-8 decoding, decoded byte length, and exact content choice.
4. Add positive and negative wire fixtures, including Unicode content, exact
   1 MiB input, over-limit encoded input, invalid base64, invalid UTF-8, and
   forbidden additional fields.
5. Regenerate `generated.py`, package exports, and the MCP schema projection.
6. Assert that all operation metadata is identical across the canonical
   catalogue and generated consumers.

Exit criteria:

- generated artifacts are reproducible and clean after regeneration;
- all contract/conformance tests pass;
- no runtime, MCP, or CLI package contains a private copy of the new schema or
  operation metadata.

Suggested commit: `feat(contracts): add evidence capture operation`

### Phase 2: storage identity and provider-neutral byte publication

**Purpose:** create the persistence boundary required by direct submission
without exposing filesystem capture as an application API.

Work:

1. Add the next workspace migration to enforce the direct-submission source
   identity tuple:
   `(workspace_id, source_kind, source_native_id, locator, retrieved_at)`.
2. Handle SQL `NULL` semantics explicitly so `locator = NULL` and
   `retrieved_at = NULL` cannot admit duplicate direct-submission identities.
3. Make migration fail closed, with a useful diagnostic, if legacy rows violate
   the new invariant. Do not silently merge potentially distinct evidence.
4. Extract or introduce a private provider-neutral byte-publication primitive
   that can store verified bytes and checksum metadata. Reuse the proven blob
   publication mechanics from `source_capture.py` without calling its
   maintenance-only path from the public handler.
5. Preserve atomic cleanup and recovery behavior for staged and published
   blobs under faults.
6. Add migration, collision, deduplication, and blob fault-injection tests.

Exit criteria:

- the database prevents duplicate canonical direct-submission sources under
  concurrent attempts;
- exact duplicate bytes can be reused safely;
- storage failures leave no authoritative partial evidence record;
- existing local-file source capture behavior remains unchanged.

Suggested commit: `feat(runtime): enforce direct evidence source identity`

### Phase 3: `evidence.capture` runtime operation and projection barrier

**Purpose:** execute the canonical operation through the standard Core service
path and meet the synchronous retrieval guarantee.

Work:

1. Register the new handler in `service/application.py` and implement it in the
   evidence handler family.
2. Decode and validate content with an encoded-size guard before allocation or
   unbounded base64 decoding. Compute SHA-256 and decoded byte length inside
   the trusted runtime.
3. Use source kind `direct_submission`, with `locator` and `retrieved_at` null.
   Do not include principal identity in source identity.
4. Implement exact reuse only when checksum, length, media type,
   `source_version`, `event_at`, and `observed_at` match. Return
   `already_captured` for an exact match and canonical `conflict` for a
   difference. Treat multiple canonical matches as an invariant failure.
5. Execute through the existing durable mutation coordinator using tuple
   `(workspace_id, principal_id, operation, idempotency_key)` and the canonical
   request digest.
6. Require current authority and a fresh `evidence.write@1.0` grant for every
   initial attempt and replay. Preserve stored-result replay for an identical
   digest and return `idempotency_conflict` for a different digest.
7. Complete the Gate A projection publication/recovery step. A call must not
   return success until a subsequent lexical `evidence.search` can find the
   captured text.
8. Emit the normal mutation audit record without storing submitted content,
   credentials, or other secrets in audit metadata.
9. Return the exact canonical result including evidence ID, source reference,
   media type, checksum, byte length, and disposition.
10. Exercise the handler through in-process, IPC, and HTTP adapters so no
    transport-specific path is introduced.

Tests must cover:

- plain text and Markdown;
- raw UTF-8 and strict base64 input;
- byte boundaries and hostile encoded size;
- exact replay and changed-payload replay;
- concurrent same-source and same-key calls;
- same source with changed metadata or content;
- inert submitted text containing URLs, local paths, and reserved-looking JSON
  keys, with instrumentation proving no fetch, path read, execution, or
  authority change;
- grant expiry and revocation between attempts;
- crash before commit, after commit, after projection publication, and before
  response serialization;
- lexical visibility before successful completion;
- projection-unavailable and stale-projection canonical errors;
- no semantic or graph projection requirement;
- audit redaction.

Exit criteria:

- all runtime and wire-adapter conformance tests pass;
- fault injection proves durable replay and projection recovery;
- no MCP code was needed to implement storage or ingestion behavior.

Suggested commit: `feat(runtime): implement evidence capture operation`

### Phase 4: shared client and CLI application surface

**Purpose:** ensure `evidence.capture` is usable through the ordinary Core
application adapters before MCP exposure.

Work:

1. Add the operation to the CLI application surface and generated/help
   metadata while preserving the canonical request envelope.
2. Confirm the generic service client carries the operation without a private
   translation. Add a typed convenience method only if that is the existing
   client policy; do not create a one-off abstraction.
3. Verify idempotency-key input, result rendering, and canonical error mapping
   through CLI, IPC, and HTTP paths.
4. Extend shared application wire fixtures so all adapters execute the same
   success and failure vectors.

Exit criteria:

- CLI and client callers reach the same runtime handler and receive identical
  canonical results/errors;
- application conformance remains adapter-neutral.

Suggested commit: `feat(cli): expose evidence capture application command`

### Phase 5: MCP manifest 2.0 and authoring adapter

**Purpose:** expose exactly the two specified tool inventories through a thin,
fail-closed MCP adapter.

Work:

1. Bump the MCP exposure manifest from 1.1 to 2.0.
2. Represent the exact restricted and authoring inventories. Admission must
   still compare every exposed binding against the canonical operation
   catalogue.
3. Treat existing `mutation_enabled` as an enforced exposure ceiling:
   absent/false means restricted; true may admit authoring only when protected
   explicit authoring intent and valid authority state also exist.
4. Keep `allowed_purposes` as a separate per-call admission check. It must not
   select or widen the inventory returned by `tools/list`, and no new
   unversioned profile field is added to `omnivia.mcp-config.v1`.
5. Determine `tools/list` once from validated startup configuration. Do not
   vary it based on prompts or individual arguments.
6. Add thin bindings for `memory_create`, `evidence_capture`, `import_start`,
   `job_get`, and `job_events`.
7. For mutation tools, enforce the outer `{input, idempotency_key}` wrapper,
   reject additional fields, use the catalogue's fixed purpose/capability, and
   acquire a fresh grant on each call.
8. For job reads, accept only canonical `JobGetInput` and `JobEventsInput` and
   preserve stable pagination and the 1,000-event maximum.
9. Preserve canonical `structuredContent` and text result encoding, error semantics,
   safe tool annotations, and existing read-tool behavior.
10. Preserve MCP protocol version `2025-06-18`, initialize negotiation,
    newline-delimited JSON-RPC framing, protocol-only stdout, and stderr-only
    logging.
11. Mark the three mutations `readOnlyHint=false` and
    `destructiveHint=false`; mark the eight authoring-profile reads
    `readOnlyHint=true` and `destructiveHint=false`. Use `idempotentHint=true`
    only where identical complete tool input, including the stable key, is
    proven to return the settled result.
12. Ensure no tool argument accepts workspace, principal, purpose, capability,
   arbitrary paths, URLs, credentials, parser choice, or runtime flags.
13. Never automatically retry a mutation with a new key. Retry before dispatch
    only when the adapter can prove Core did not receive the request; otherwise
    return the canonical ambiguous outcome and direct same-key replay.

Tests must cover:

- exact names and count for both profiles;
- fail-closed startup for invalid, unsafe, or incomplete configuration;
- upgrade defaulting to restricted;
- wrapper validation and catalogue-derived metadata;
- fresh authorization and grant checks on replay;
- all excluded operations remaining absent;
- MCP stdio end-to-end calls for every new tool;
- cached host discovery after server restart and revocation.

Exit criteria:

- manifest admission proves the authoring set contains only the three named
  mutations and two job reads;
- MCP remains a transport adapter with no direct persistence dependency;
- all restricted-profile regression tests remain green.

Suggested commit: `feat(mcp): add explicit authoring exposure profile`

### Phase 6: installed MCP administration and authority lifecycle

**Purpose:** let a human securely configure, inspect, rotate, and revoke a
dedicated MCP principal without a UI.

Work:

1. Add a distinct CLI administration family rather than disguising setup as a
   model-callable application operation:
   - `omnivia mcp configure --host <claude-code|codex> --workspace <id> --profile restricted|authoring`
   - `omnivia mcp status`
   - `omnivia mcp revoke`
2. Implement the Gate B storage/service changes for dedicated principal,
   credential reference, bounded grants, and protected authoring intent.
3. Require owner/admin authorization and verify the selected workspace before
   changing MCP state.
4. Generate the least-privilege grants implied by the selected profile. Never
   let the caller supply arbitrary scopes or capabilities through these
   commands.
5. Write the private Core MCP configuration atomically with owner-only
   permissions. Defend against symlink substitution and partial replacement.
6. Write or print the minimum supported host configuration containing only the
   executable command and protected config path. Never embed credential
   material in host configuration or command-line arguments.
7. Validate the installed service handshake and exact `tools/list` result
   before configure reports success.
8. Make configuration compensating and restart-safe. A failure after principal
   creation, credential rotation, private-config publication, or host-config
   update must either restore the prior usable state or leave a clear,
   recoverable status.
9. Make repeated configure idempotent where the requested state matches and
   rotate credentials safely when it does not.
10. Make status useful but redacted: installation/service state, workspace,
   principal identifier, profile, grant health, config health, and host target;
   no secret values.
11. Make revoke invalidate authority before removing or disabling local config.
    Revocation blocks new calls and replays but does not cancel already
    committed import jobs. Owner/operator job observation remains available.
12. Define upgrade behavior for existing `mutation_enabled: true`: it remains
    restricted unless the protected explicit authoring record was created by
    the new authorized setup path.

Tests must cover:

- owner/admin success and unauthorized rejection;
- nonexistent or mismatched workspace rejection;
- exact least-privilege grants for both profiles;
- file mode, atomic replacement, symlink resistance, and redaction;
- partial-failure compensation at every write boundary;
- configure repetition, profile change, credential rotation, and revoke;
- revocation while an import job is queued/running;
- supported macOS behavior plus repository-supported Windows path/ACL behavior
  where the existing CLI contract requires it.

Exit criteria:

- a human can configure and revoke MCP without editing secrets into host files;
- authoring cannot be enabled by editing the public MCP config alone;
- an old or cached server session cannot mutate after revocation.

Suggested commits:

- `feat(runtime): persist installed mcp authority`
- `feat(cli): add installed mcp administration commands`

### Phase 7: end-to-end, security, and recovery acceptance

**Purpose:** prove the complete standalone workflow and the negative security
boundary before real-host qualification.

Automated acceptance scenarios:

1. Start from a new empty test workspace.
2. Configure a dedicated authoring principal through the installed CLI path.
3. Confirm the restricted inventory before authoring is explicitly enabled.
4. Start MCP in authoring mode and capture evidence.
5. Search and retrieve the evidence lexically before capture is considered
   successful.
6. Create proposed memory using canonical source and evidence references;
   confirm no private `evidence_id` shortcut was introduced.
7. Confirm default memory search does not publish the proposal, then find the
   proposed candidate using the explicit `view: "candidates"` search.
8. Replay all three mutations with the same key and verify stable canonical
   results; change a payload and verify `idempotency_conflict`.
9. Start an import from an already-staged descriptor and observe it through
   `job_get` and paginated `job_events`.
10. Revoke the MCP principal, prove new reads/mutations and mutation replays are
    blocked, and prove an already committed import is not cancelled.
11. Restart service and MCP at each meaningful fault boundary and prove
    recovery without duplicate authoritative records.
12. Verify audit completeness and absence of submitted content or secrets.

The journey must populate application data only through the real MCP calls. It
must not pre-seed evidence or memory through runtime internals, direct SQL,
fixtures, maintenance capture, or CLI mutation commands. After the MCP session
closes, the independently owned Core service must remain healthy.

Security review:

- tool inventory and schema review;
- confused-deputy review for workspace/principal/purpose derivation;
- credential/config filesystem review;
- replay-after-revocation review;
- oversized/base64 input and resource-exhaustion review;
- SQL concurrency and uniqueness review;
- log, audit, exception, and status redaction review;
- confirmation that no Apple privacy permission is requested merely to run
  Core/MCP; access to protected user folders remains a separate staging or
  connector concern.

Exit criteria:

- the v1.3 requirement-to-test matrix has no uncovered MUST/MUST NOT item;
- fault, concurrency, authorization, and redaction suites pass;
- a fresh reviewer finds no unbounded MCP or filesystem authority.

Suggested commit: `test(mcp): cover standalone authoring workflow`

### Phase 8: packaging, documentation, and real-host qualification

**Purpose:** validate the shipped artifacts rather than only the source tree or
SDK simulation.

Work:

1. Pin and build against `mcp==2.0.0` and `mcp-types==2.0.0` using the release
   wheelhouse constraints.
2. Build/install Core runtime, client, CLI, and MCP packages into a clean
   qualification environment.
3. Update MCP and CLI documentation with profile selection, setup, status,
   revoke, upgrade, credential rotation, staging boundary, and Apple permission
   behavior.
4. Document that `import_start` accepts only an existing staged descriptor and
   does not stage arbitrary local paths or URLs through MCP.
5. Add redacted host configuration examples for Claude Code and Codex.
6. Qualify the installed artifacts against:
   - Claude Code 2.1.269;
   - Codex CLI 0.146.0;
   - macOS 26.5.2 build 25F84 on arm64;
   or explicitly approved release replacements recorded with the results.
7. Use isolated temporary host profiles, credentials, configs, and test
   workspaces. Do not touch production workspaces or the user's normal host
   configuration.
8. Retain redacted evidence for tool discovery, capture/search, proposed-memory
   creation, import observation, restart, and revocation. SDK simulation alone
   is not acceptance evidence.
9. Run the complete repository preflight and package smoke tests from the
   installed artifacts.

Exit criteria:

- both real hosts discover the exact authoring inventory and pass the complete
  standalone workflow;
- the pinned release environment passes independently of a developer's newer
  SDK;
- docs and examples contain no secrets or unsafe default paths;
- `./scripts/preflight` passes.

Suggested commits:

- `build(mcp): pin qualified sdk release`
- `docs(mcp): document standalone authoring setup`

### Phase 9: integration and completion

**Purpose:** land the feature with traceable acceptance evidence.

Work:

1. Rebase or merge the current integration base as required and rerun generated
   checks after resolving any contract or migration movement.
2. Run a fresh implementation review, emphasizing Gates A and B, database
   concurrency, authorization replay, config atomicity, and excluded tools.
3. Run the complete preflight plus all targeted release and real-host checks.
4. Update the requirements/implementation status only after every acceptance
   gate is satisfied.
5. Commit all accepted work, open the PR, and wait for required CI checks.
6. Merge only when the branch is green and review findings are resolved.
7. Remove or archive the feature worktree after the merged commit is present in
   the canonical checkout.

Exit criteria:

- every v1.3 requirement links to implementation and automated or recorded
  qualification evidence;
- required CI is green;
- the merged canonical branch contains the feature;
- no unfinished feature worktree or live test credential remains.

## 6. Verification matrix

| Area | Minimum verification |
|---|---|
| Contract | schema validation, semantic validation, generated-artifact check, operation catalogue conformance |
| Migration | clean upgrade, legacy-conflict failure, uniqueness under concurrency, rollback/fault behavior |
| Runtime | handler unit tests, mutation replay, current grant checks, audit, IPC/HTTP parity |
| Projection | capture-to-search barrier, crash/replay recovery, unavailable/stale errors, no table-scan fallback |
| MCP | exact inventories, startup fail-closed, wrapper schemas, canonical dispatch, stdio end to end |
| CLI setup | owner/admin authorization, least privilege, atomic private config, rotation, revoke, redaction |
| Jobs | staged-only import, get/events pagination, revoke without committed-job cancellation |
| Packaging | clean pinned wheelhouse install, package import/smoke checks, no dependency drift |
| Hosts | real Claude Code and Codex discovery and full isolated workflow |
| Repository | formatting, lint, type checks, targeted tests, full `./scripts/preflight` |

The final requirement traceability table should use the section and normative
statement identifiers from v1.3 rather than relying only on phase-level test
names.

## 7. Review and execution model

The writing order is serial:

`contracts -> storage -> runtime/projection -> shared adapters -> MCP -> setup/authority -> end-to-end -> packaging/hosts`

Parallel work is limited to non-overlapping read-only or review activity after
the relevant contract boundary is stable, for example:

- security threat review while runtime tests are being completed;
- real-host qualification harness preparation after the MCP manifest is stable;
- documentation review after CLI syntax and configuration shapes are frozen.

Do not run parallel writers against the same worktree or split tightly coupled
contract/runtime files across agents. For implementation packets that cross
more than three meaningful files, use the repository's explore, plan, execute,
review workflow. Routine bounded changes can use the normal implementation
model; projection/recovery and installed authority work should receive the
highest-reasoning implementation and independent review because they carry the
largest correctness and security risks.

## 8. Risk register

| Risk | Consequence | Mitigation and gate |
|---|---|---|
| Projection and business commit are split | capture reports success but search cannot find content | Gate A; crash/replay tests at every boundary |
| Public config can imply authoring without protected intent | privilege escalation on upgrade or file edit | Gate B; protected authority record; fail-closed startup |
| Direct-source identity is application-only | duplicate authoritative evidence under concurrency | database uniqueness with explicit NULL handling |
| Encoded input is decoded before bounding | memory/resource exhaustion | encoded-length precheck plus decoded 1 MiB validation |
| Idempotent replay trusts stale authority | revoked principal can continue mutating | fresh session/grant validation on every replay |
| Host caches a former tool set or process | revoked or downgraded tools appear usable | authority checked per call; restart/status guidance; cached-host tests |
| SDK development version differs from release pin | source tests pass but installed release fails | clean pinned wheelhouse build and real-host qualification |
| Installation writes fail midway | orphan credential or unusable host configuration | ordered atomic writes, compensation, status repair path, fault injection |
| Migration head advances | conflicting migration number or ordering | recheck after rebase immediately before migration work |
| Legacy duplicate sources exist | unsafe automatic merge or upgrade corruption | detect and fail with operator-visible diagnostics |
| CLI filesystem assumptions are macOS-only | supported platform regression | isolate permission abstraction and retain platform CI coverage |
| Feature worktree diverges from canonical repository | accepted code is stranded or duplicated | one integration lane; reconcile before PR; retire after merge |

## 9. Definition of done

The feature is done only when all of the following are true:

- the public catalogue contains `evidence.capture` and
  `evidence.write@1.0`;
- the runtime implements exact direct-submission identity, byte limits,
  deduplication/conflict rules, audit, and durable idempotency;
- successful capture is lexically searchable and crash-recoverable;
- MCP manifest 2.0 advertises exactly six restricted or eleven authoring tools;
- mutation wrappers, purposes, capabilities, workspace identity, and principal
  identity cannot be selected by the model;
- installed configure/status/revoke commands provision a dedicated,
  least-privilege, revocable principal with private atomic configuration;
- upgrade remains restricted without explicit protected authoring intent;
- all excluded governance, filesystem, network, staging, and grant-management
  operations remain unexposed;
- an empty isolated workspace passes evidence capture/search, proposed-memory
  authoring, staged import, job observation, replay, restart, and revocation;
- pinned release artifacts pass both real supported hosts;
- the complete v1.3 traceability matrix, repository preflight, review, and CI
  gates are green;
- merged work is present in the canonical checkout and temporary credentials,
  profiles, workspaces, and the completed feature worktree are cleaned up.
