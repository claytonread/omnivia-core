# OmniVia Core Remaining Development Project Plan and Stream B Handoff

Date: 2026-07-30  
Status: Active development plan  
Architecture authority: accepted ADR-036, ADR-037, ADR-038 and OmniVia Core architecture specification v0.6  
Current implementation branch: `codex/ui-residual-risk-closure`  
Current reviewed checkpoint: `a7cd551`  
Primary repository: `/Users/claytonread/Projects/omnivia-core`

## 1. Executive decision

The remaining development programme is divided into two coordinated streams.

| Stream | Lead | Responsibility |
|---|---|---|
| **Stream A — Contracts, compatibility and integration** | Codex / current development task | Complete the public contract migration, the `omnivia-memory` compatibility facade, provider-neutral API and wire conformance, package and release gates, consumer cutover planning, and final integration review. |
| **Stream B — Workspace authority and standalone runtime** | Handoff agent | Independently review the accepted foundation, then implement workspace format, migrations, backup and recovery, locks, leases, fencing and the independently runnable Core Service. After separate Phase 3+ approval, extend the service and implement the standalone CLI and MCP adapters. |

Codex will take **Stream A**.

The handoff agent should take **Stream B** and also perform an independent review of the completed work before relying on it.

The two streams must use separate branches and, while they are active concurrently in the same repository, separate worktrees. Shared files are integration-controller owned and must not be edited independently by both streams.

## 2. Current authorization boundary

The product and architecture owner approved Phases 0–2 on 2026-07-29.

That approval currently authorizes:

- Phase 0 baseline and migration evidence;
- Phase 1 package and contract foundations;
- Phase 2 workspace format, migrations, ownership and fencing;
- the minimal independently runnable Core Service required to prove Phase 2 ownership and readiness.

It does **not** yet authorize broad Phase 3–7 implementation. In particular, the complete provider-neutral service operation catalogue, full standalone CLI, full standalone MCP server, cross-repository consumer cutover and production distribution work require their later task packets and approval gates.

This document plans those later phases, but labels them as gated work rather than treating them as already authorized.

## 3. Status update

### 3.1 Overall position

Estimated completion of the current full OmniVia Core development goal is **approximately 40%**.

This is an engineering estimate, not a simple count of architecture phases. The completed work establishes a large, heavily tested contract and migration foundation, while the highest-risk operational work—workspace authority, fencing, standalone service behavior, adapters, consumer cutover and production hardening—remains.

| Area | Estimated status | Position |
|---|---:|---|
| Phase 0 baseline freeze | 100% | Complete and reproducible |
| Phase 1 package and contract foundation | 85–90% | Substantially implemented; compatibility and closeout work remains |
| Phase 2 workspace, migrations and fencing | 0% operationally | Planned in detail; accepted runtime implementation has not started |
| Phase 3 provider-neutral service | Foundation only | Wire foundations exist; operation-level service behavior is not implemented |
| Phase 4 CLI and MCP | Skeleton only | Distributions exist; operational adapters are not implemented |
| Phases 5–7 | Mostly not started | Consumer cutover, full knowledge workflow and production hardening remain |

### 3.2 Repository state

- The implementation worktree was clean at `a7cd551` before this plan was added; this plan is the only current uncommitted path.
- The branch is 14 commits ahead of `origin/codex/ui-residual-risk-closure`.
- The work is locally accepted and verified but is not yet merged or released.
- The accepted PM copies of ADR-036, ADR-037, ADR-038 and specification v0.6 are authoritative. Copies previously supplied from Downloads must not override the accepted PM versions.

### 3.3 Completed implementation

| Checkpoint | Commit | Completed result |
|---|---|---|
| Phase 0 baseline | `117cf83` | Frozen public exports, dependencies, routes, MCP/CLI surfaces, scenarios, storage schema and legacy database fixture; deterministic drift checks and rollback evidence |
| Package boundaries | `e93fb12` | Independently buildable `omnivia-core`, `omnivia-core-runtime`, `omnivia-core-mcp` and `omnivia-core-cli` distributions; dependency-direction and isolated-install guards |
| Application contract foundation | `f6e84fe` | Draft 2020-12 schemas, canonical fixtures, generated Python and TypeScript artifacts, tolerant production codec, strict conformance validation, version and grant compatibility helpers |
| Canonical domain contracts | `509b0f0`–`68eadc3` | Canonical public models, validators, helpers and barrels across knowledge, Apps contracts, graph, memory, workspace, ingestion, provenance and memory graph |
| Run ledger | `5288573` | Canonical run-ledger models, validation and export behavior |
| Control plane | `d866854`–`a7cd551` | Canonical models, imports, validation and exact public barrel |

The canonical migration now covers:

- 31 registered canonical leaf modules;
- 29 exact legacy contract ports plus the documented shared barrel/search-model exception;
- exact public barrels for knowledge, App Manifest, Component and Module contracts, graph, memory, workspace, ingestion, provenance, memory graph, run ledger and control plane;
- an exact 74-name control-plane public surface, routed to 55 model exports, 14 import exports and 5 validation exports;
- import-order, isolated-module closure, namespace, object-identity and static export tests.

### 3.4 Verification evidence

The latest independent audit and host rerun report:

- 2,019 tests passing in the complete repository suite (`PYTHONPATH=. .venv/bin/pytest -q`);
- 5 pre-existing SWIG deprecation warnings in the PDF ingestion tests;
- 587 application-contract tests passing;
- 456 canonical-migration tests passing;
- all 6 Phase 0 drift checks and 163 Phase 0 tests passing;
- all four wheels built and installed independently from a clean temporary wheelhouse;
- the Core wheel containing the expected 5 schemas and 14 application-contract fixtures, without runtime dependencies;
- Ruff clean over the applicable new source and tests;
- strict mypy clean over the canonical source;
- no blocking findings in the final control-plane barrel review.

The final control-plane review specifically verified exact export order, owner routing, runtime binding identity, star import behavior, child-module identities and closure under all three tested import orders.

### 3.5 What completion has not yet proved

The current result must not be described as a complete standalone Core product. It has not yet proved:

- an identity-preserving `omnivia-memory` compatibility facade across supported root and submodule paths;
- full behavioral parity for the legacy control-plane manifest contract;
- the portable policy compiler behavior currently embedded in legacy registry tests;
- merge-blocking CI for all new acceptance gates;
- operation-level request and response contracts for the first service vertical slice;
- a production service transport or dispatcher;
- authentication, authorization, durable jobs, pagination, streaming or idempotency behavior;
- portable workspace manifests, migrations, backup, restore or rollback;
- authoritative leases, fenced writes or multi-process takeover safety;
- an operational `omnivia-core-service`, CLI or MCP server;
- consumer cutover in Platform, Apps or Dev;
- cross-platform release, upgrade, security and recovery qualification.

## 4. Programme roadmap

| Milestone | Primary stream | Authorization | Exit condition |
|---|---|---|---|
| M0 — Frozen baseline | Complete | Approved | Phase 0 fixtures and drift gates reproducible |
| M1 — Phase 1 closeout | Stream A | Approved | T-0628 compatibility, behavioral, CI and acceptance gaps closed |
| M2 — Workspace authority | Stream B | Approved after M1 | Full T-0629A–G and adversarial matrix pass |
| M3 — Provider-neutral service vertical slice | A contracts; B runtime | Requires Phase 3 approval | In-process and service transports pass the same semantic and wire conformance suite |
| M4 — Standalone CLI and MCP | B implementation; A conformance | Requires Phase 4 approval | Clean install can create/import a workspace and connect Claude without Desktop or Dev |
| M5 — Consumer cutover | A integration | Requires Phase 5 approval and repo-specific cross-repo tasks | Platform, Apps, Dev, CLI and MCP use the Core API and do not directly write Core storage |
| M6 — Complete knowledge workflow | A public contracts; B runtime | Requires later approval | Evidence import, candidate extraction, governance, retrieval and cited Context Pack milestone pass |
| M7 — Production hardening | Joint, integrated by Stream A | Requires later approval | Cross-platform bundles, upgrade/rollback, SBOM, security, performance and recovery gates pass |

## 5. Stream A plan — Codex-owned

### A0 — Land and protect the accepted foundation

Status: ready to execute.

Work:

1. Push or open a pull request for the reviewed 14-commit lineage when publication is authorized.
2. Add merge-blocking CI for:
   - package dependency boundaries;
   - wheel builds and isolated installs;
   - application-contract schema, fixture and generated-artifact drift;
   - canonical migration;
   - Phase 0 drift checks;
   - Ruff and strict mypy.
3. Preserve the informational benchmark workflow but do not treat it as a replacement for merge gates.
4. Record the accepted commit and exact commands in PM evidence through a separate PM-owned change.

Exit:

- the accepted foundation is reviewable as one visible lineage;
- all existing local acceptance gates run in CI;
- no generated or build artifacts remain in the repository.

### A1 — Close T-0628 compatibility and behavior

Status: next active Stream A milestone.

Work:

1. Port the pure behavioral coverage from `services/omnivia-memory/tests/test_control_plane_contract.py` to canonical Core tests without moving runtime behavior.
2. Extract and port the pure `compile_policy_expression` contract tests from the legacy control-plane registry suite.
3. Freeze the supported legacy export map:
   - 183 advertised root exports;
   - 36 contract modules;
   - 32 transitional runtime modules;
   - four runtime-owned root compatibility bindings: `Database`, `MemoryCreate`, `MemoryService` and `MemoryUpdate`.
4. Implement `omnivia_memory` as an identity-preserving compatibility facade over canonical `omnivia_core` contracts.
5. Support the documented legacy submodule paths, not only the package root.
6. Route runtime-only exports deliberately; do not duplicate their models in Core.
7. Add object-identity, import, export-drift and deprecation-metadata tests.
8. Re-run the complete T-0628 acceptance suite and record a formal accepted checkpoint.

Exit:

- direct and compatibility imports resolve to the same supported contract objects;
- no parallel public domain model remains;
- unsupported/runtime-owned paths are explicit;
- T-0628 can move from `In Progress` to `Done`.

### A2 — Complete the provider-neutral application contract

Status: design may be refined now; implementation beyond the Phase 1 foundation requires Phase 3 approval.

Work:

1. Define operation and payload schemas for the first vertical slice:
   - health, readiness and discovery;
   - workspace inspect, create and list;
   - memory create, read, list and search;
   - ingestion/import;
   - evidence and governed-knowledge search;
   - graph traversal;
   - Context Pack creation.
2. Complete version, workspace-format and operation compatibility matrices.
3. Complete stable errors, opaque pagination, mutation preconditions, idempotency and audit correlation.
4. Complete durable job, progress, cancellation, retry, resumability and event schemas.
5. Prove tolerant unknown-field and explicit enum-evolution behavior.
6. Prove provenance and temporal values survive every first-party adapter round trip.
7. Regenerate deterministic Python and TypeScript artifacts from one canonical schema source.
8. Provide one conformance suite that in-process, local IPC/HTTP, CLI, MCP and Desktop adapters must share.

Exit:

- the contract is operation-complete for the first product vertical slice;
- strict conformance and tolerant production decoding are both tested;
- transport adapters cannot define a competing domain API.

### A3 — Consumer inventory and compatibility cutover

Status: inventory and planning only. Dependency, import and write-path changes require both Phase 5 approval and separate repo-specific cross-repository tasks.

Work:

1. Refresh the recorded inventory of 28 legacy import paths in Platform, Apps and Dev.
2. Classify each use as:
   - public contract;
   - runtime implementation;
   - direct storage access;
   - obsolete or removable.
3. Create repo-specific migration packets.
4. Update dependency manifests and imports through separate tasks in each repository.
5. Move Dev MCP/CLI normal operations from direct storage construction to Core Service API clients.
6. Preserve the compatibility facade for at least two supported release trains; remove it only in a major release with migration guidance and export-drift proof.

Exit:

- no consumer directly writes Core storage;
- all first-party clients pass the same contract conformance fixtures;
- compatibility-facade removal criteria are met.

### A4 — Integration and distribution closeout

Status: later-phase, approval gated.

Work:

1. Integrate Stream B checkpoints and run the combined acceptance matrix.
2. Complete standalone distribution composition and clean-machine install tests.
3. Add upgrade, rollback, backup and restore scenarios.
4. Publish compatibility matrices, operator guidance, security guidance and deprecation guidance.
5. Add signed macOS, Windows and Linux bundles, containers/service packages and SBOMs.
6. Run performance, crash, sleep/resume and unreliable-filesystem qualification.

Exit:

- the standalone and Platform-integrated release candidates meet specification v0.6 acceptance gates.

## 6. Stream B plan — handoff agent

### B0 — Independent review and Phase 1 gate

The Stream B agent must first review rather than assume the foundation.

Work:

1. Confirm `a7cd551` is the intended starting checkpoint and inspect all 14 commits.
2. Reproduce package builds and isolated installs.
3. Reproduce dependency, schema, fixture, generated-artifact, canonical-migration and Phase 0 gates.
4. Review the control-plane barrel and compatibility evidence.
5. Report any discrepancy before writing Phase 2 code.
6. Do not start T-0629 operational implementation until Stream A records the T-0628 closeout checkpoint.

The Stream B agent may concurrently prepare read-only design notes, test plans, migration fixture oracles, fake process/clock evidence and adversarial harness design while Stream A completes T-0628.

### B1 — T-0629A: portable workspace manifest and atomic layout

Work:

- add the accepted public manifest, compatibility, projection, encryption, migration-summary and integrity contracts alongside the current legacy compatibility model;
- remove or redirect legacy workspace models only through Stream A's compatibility cutover;
- add the v1 JSON Schema, canonical serialization and checksum rules;
- implement the five-path workspace layout;
- implement atomic fsync/rename manifest writes and zero-write inspection;
- reject traversal, unsafe symlinks and secret-bearing manifest fields;
- prove workspace identity is stable when a workspace is moved.

Exit:

- a portable workspace contains only `workspace.json`, `workspace.sqlite`, `blobs/`, `indexes/` and `locks/`;
- no absolute installation/process identity or secret material is serialized into the portable manifest.

### B2 — T-0629B: versioned storage and Generation-1 bootstrap

Work:

- freeze the Phase 0 database as an immutable SQL/fixture oracle independent of live runtime code;
- enable WAL, foreign keys, busy timeout and integrity checks;
- add checksum-pinned ordered migrations and durable migration-attempt records;
- add reserved workspace-state, lease, open-event, projection-ledger and durable-job tables;
- implement explicit read-only, ephemeral, service-owned and exclusive-maintenance modes;
- ensure ordinary open cannot create or patch a schema;
- implement crash-safe Generation-1 bootstrap under the lifetime lock and sole exclusive SQLite connection.

Exit:

- every bootstrap crash point safely retries or resumes from a valid committed state;
- generation 1 is created exactly once;
- later migrations run under the exact current workspace, generation and service-instance tuple.

### B3 — T-0629C: backup and copy-only legacy migration

Work:

- accept only an explicit legacy source path;
- inspect the source read-only and require the frozen Phase 0 fingerprint;
- create and verify a complete backup;
- migrate a staging copy, never the source or sole backup;
- preserve schema, row counts and value checksums;
- journal attempts outside the portable workspace;
- publish atomically only after database, manifest and integrity validation;
- prove exact rollback.

Exit:

- failed migration cannot corrupt or replace the legacy source;
- rollback restores the exact Phase 0 schema, counts and values.

### B4 — T-0629D: identity, filesystem qualification and locks

Work:

- implement stable installation identity and unique per-start service identity;
- capture process-start and boot evidence;
- implement bootstrap mutex, takeover coordination lock and lifetime storage lock;
- freeze one lock interface with POSIX and Windows implementations;
- add two-process lock probes;
- fail closed for writable use on unreliable or unknown lock semantics.

Exit:

- supported local filesystems pass platform-specific two-process tests;
- NFS, SMB/CIFS, SSHFS, unreliable FUSE and unknown semantics refuse direct writable operation.

### B5 — T-0629E: lease, takeover and discovery

Work:

- atomically increment the monotonic fencing generation during acquisition;
- treat expiry only as a signal, never proof that the owner is dead;
- require endpoint, process-start and storage-lock evidence for takeover;
- implement heartbeat, graceful handover and stale-owner rejection;
- atomically publish discovery/readiness and compare-clean only the current instance.

Exit:

- simultaneous acquisition has exactly one winner;
- stale owners cannot reclaim authority;
- failed startup cannot delete another instance's discovery record.

### B6 — T-0629F: fenced transactions and mutation cutover

Work:

- hold one service-owned exclusive SQLite connection for the entire writable ownership lifetime;
- use `BEGIN IMMEDIATE`;
- validate `(workspace_id, fencing_generation, service_instance_id)` inside every write transaction and immediately before commit;
- enforce guarded DML through persisted triggers and a SQLite authorizer;
- verify exact schema and trigger fingerprints before readiness;
- guard repository, ingestion, projection, scheduler, durable-job and migration writes;
- replace legacy writable database behavior with the accepted runtime delegate;
- remove implicit writable `~/.omnivia/memories.db` fallback;
- prove a second stock SQLite process cannot perform DML or DDL while the service is ready.

Exit:

- no stale generation can commit after takeover, sleep or resume;
- all current mutation paths are fenced;
- ordinary unregistered DML and DDL fail closed.

### B7 — T-0629G: minimal independently runnable Core Service

Work:

- add the `omnivia-core-service` entry point;
- implement discovery → bootstrap mutex → rediscovery → spawn → lease → recovery → readiness;
- implement stopped, starting, recovering, migrating, ready, running, draining, maintenance, failed and stopped states;
- release resources in reverse acquisition order on every failed transition;
- publish writable readiness last.

Readiness requires, for the same service instance:

- compatible manifest;
- qualified filesystem;
- held lifetime storage lock;
- live sole exclusive SQLite connection;
- exact current lease tuple;
- canonical migration checksums;
- successful integrity check;
- exact schema and trigger fingerprint;
- recovered migrations and durable jobs.

Exit:

- one independently runnable service owns and advertises one writable workspace;
- failed or stale instances publish no readiness.

### B8 — Phase 2 adversarial qualification

Convert the accepted matrix into named executable tests:

| Group | Required cases |
|---|---:|
| Workspace and manifest | 12 |
| Bootstrap and discovery | 12 |
| Lease and owner evidence | 22 |
| Filesystem locking | 8 |
| Fencing and mutation | 22 |
| Migration, backup and restore | 28 |
| Lifecycle, scheduler and cleanup | 12 |
| **Total** | **116** |

Exit:

- every applicable row passes;
- POSIX and Windows lock suites pass in CI;
- rollback and data preservation are demonstrated;
- package, application-contract, Phase 0 and pre-existing suites remain green.

### B9 — Full provider-neutral service

Status: requires Phase 3 approval.

After A2 freezes each operation contract, implement the neutral service handlers, authorization boundary, durable jobs and supported transports in `omnivia-core-runtime`. The runtime must consume public contracts and must not redefine them.

Exit:

- in-process and service transports pass Stream A's same conformance suite;
- health/readiness/discovery remain distinct from product operations;
- no adapter has direct storage authority.

### B10 — Standalone CLI and MCP

Status: requires Phase 4 approval and dedicated task packets.

Work after approval:

- implement the base `omnivia` CLI as a Core Service client;
- implement the official first-party MCP adapter in managed stdio and service-client modes;
- allow bootstrap coordination but never workspace-lease ownership;
- enforce fixed principal, workspace allowlist and granted operation set;
- provide equivalent authorized results across managed-local and service-client modes;
- add clean-machine Claude connection and standalone smoke tests.

Restrictions:

- CLI and MCP depend only on public `omnivia-core` contracts;
- neither imports `omnivia-core-runtime`;
- neither owns the authoritative lease;
- neither opens workspace SQLite directly for normal operation;
- Dev-only commands and tools remain in Dev.

## 7. File and branch ownership

### 7.1 Proposed branch/worktree layout

| Stream | Checkout | Branch |
|---|---|---|
| A | `/Users/claytonread/Projects/omnivia-core` | `codex/ui-residual-risk-closure` |
| B | `/Users/claytonread/Projects/worktree-omnivia-core-stream-b` | `agent/omnivia-core-stream-b` |

The Stream B worktree is justified only while the two write-capable streams are concurrent. It should be created from the accepted T-0628 closeout checkpoint, integrated promptly through reviewed commits, and removed after handback.

### 7.2 Ownership matrix

| Path or concern | Owner |
|---|---|
| `src/omnivia_core/contracts/**` | Stream A |
| `contracts/application/**` and generated application artifacts | Stream A |
| canonical compatibility facade contract modules | Stream A |
| new `src/omnivia_core/workspace/manifest.py`, `compatibility.py` and `schemas/workspace-manifest-v1.schema.json` files for T-0629A | Stream B |
| existing `src/omnivia_core/workspace/models.py` | Stream A compatibility surface; Stream B must not edit unilaterally |
| `src/omnivia_core/workspace/__init__.py` | Integration controller |
| `packages/omnivia-core-runtime/**` | Stream B |
| Phase 2 runtime/adversarial tests | Stream B |
| `packages/omnivia-core-cli/**` and `packages/omnivia-core-mcp/**` after approval | Stream B |
| legacy runtime database delegate during T-0629F | Stream B, using an exact file list agreed at that checkpoint |
| root `pyproject.toml`, lockfiles, README and `.github/**` | Integration controller / Stream A |
| PM ADRs, task packets, backlog and decision log | PM-owned; neither implementation stream writes without an explicit PM task |

The legacy `services/omnivia-memory` tree contains both contract facade and runtime transition seams. Before either stream edits it, the integration controller must freeze an exact per-file allocation. No overlapping edits are permitted.

## 8. Dependency and merge rules

1. Stream A closes and commits T-0628 before Stream B begins operational T-0629 code.
2. Stream B may perform independent review and read-only test planning before that gate.
3. Stream B must consume canonical public contracts; it must not create competing public models in the runtime package.
4. Stream A must not change workspace authority or fencing semantics without a superseding ADR.
5. Stream B must not change API wire compatibility or public dependency direction without a superseding ADR.
6. Shared-file edits are prepared as requests and applied by the integration controller.
7. Each implementation slice ends in a small reviewed commit with focused tests and full regression gates.
8. Stream A reviews every Stream B handback for public-contract and dependency impact.
9. Stream B reviews Stream A service contracts for implementability before they are frozen.
10. Integration order is:

   ```text
   T-0628 closeout
   → T-0629A manifest
   → T-0629B migrations/bootstrap
   → T-0629C backup/migration
   → T-0629D locks
   → T-0629E lease/discovery
   → T-0629F fencing/cutover
   → T-0629G service lifecycle
   → Phase 2 adversarial qualification
   → Phase 3 contract/service vertical slice
   → Phase 4 CLI/MCP
   → consumer cutover
   → production hardening
   ```

Lease, fencing, the runtime database and the legacy runtime delegate form one safety chain and must remain serial.

## 9. Safe concurrent work

The following work can run concurrently without creating two authorities for the same behavior:

- Stream A compatibility facade work while Stream B performs read-only Phase 2 exploration and adversarial test planning;
- manifest fixtures and pure compatibility tests alongside migration fixture/schema-delta oracle design;
- POSIX and Windows lock adapters after their common interface freezes;
- fake clock/process evidence and multi-process harness preparation alongside serial ownership implementation;
- static dependency and mutation-call-site audits alongside runtime implementation;
- Stream A operation schemas alongside Stream B service-handler exploration after Phase 3 approval, provided schemas are frozen before handler implementation lands;
- independent review, test execution and documentation alongside a single bounded writer.

The following must not run as competing write lanes:

- two edits to the same public contract;
- lease and fencing implementation on divergent branches;
- runtime database and legacy delegate cutover on divergent branches;
- root packaging/lockfile changes from both streams;
- public API schema changes while a service implementation assumes an unfrozen version.

## 10. Review checklist for the handoff agent

The receiving agent should produce a short review report answering:

1. Do the 14 commits match ADR-036 and the Phase 0/1 task packets?
2. Are any canonical contracts behaviorally different from their frozen legacy source?
3. Does every supported compatibility export preserve object identity?
4. Do all four packages build and install independently?
5. Are dependency guardrails structural rather than text-only?
6. Are generated artifacts deterministic and derived from one schema source?
7. Does tolerant production decoding remain distinct from strict conformance validation?
8. Are any runtime or storage dependencies leaking into public Core contracts?
9. Does the proposed T-0629 implementation protect the real current write seam?
10. Are any Phase 3+ features being attempted without approval?

Any blocker must cite an exact file, test or acceptance criterion. Review observations that do not block T-0629 should be recorded as Stream A follow-up rather than silently broadening Stream B.

## 11. Handoff brief for the Stream B agent

Use the following as the receiving agent's task:

> You own Stream B of OmniVia Core development. First perform an independent, read-only review of commits `117cf83` through `a7cd551` on branch `codex/ui-residual-risk-closure`. Reproduce the package, application-contract, canonical-migration and Phase 0 gates and report discrepancies with exact evidence.
>
> Do not modify PM-owned files. Do not begin operational Phase 2 implementation until Codex records the T-0628 closeout checkpoint. While waiting, prepare the T-0629 test plan, migration fixture oracle, mutation-call-site inventory, fake process/clock evidence and multi-process adversarial harness design.
>
> After the gate, implement T-0629A–G in order in a dedicated Stream B worktree. Keep lease, fencing, runtime database and legacy delegate changes in one serial committed lineage. Use public `omnivia_core` contracts; do not create a competing runtime domain model. Add T-0629A manifest files alongside the existing workspace compatibility surface; do not edit `workspace/models.py` or `workspace/__init__.py` unilaterally. Do not edit other Stream A contract/API paths or shared root packaging files without an integration request.
>
> Full CLI and MCP implementation is not authorized under Phases 0–2. Phase 2 may add only the shared discovery/bootstrap contracts and client simulation necessary to prove that clients never own the authoritative lease. Stop at the minimal independently runnable service and the complete adversarial exit gate unless later approval is provided.
>
> Return after each slice: summary, exact files, diff review, tests and commands, failures or warnings, architecture concerns, dependency impact, and the commit proposed for integration.

Required reading:

- `/Users/claytonread/Projects/omnivia-core/AGENTS.md`
- `/Users/claytonread/Projects/omnivia-pm/docs/operating-model/codex-claude-multirepo-workflow.md`
- `/Users/claytonread/Projects/omnivia-pm/docs/adr/ADR-036-omnivia-core-repository-boundaries-and-distribution.md`
- `/Users/claytonread/Projects/omnivia-pm/docs/adr/ADR-037-core-service-ownership-bootstrap-and-workspace-fencing.md`
- `/Users/claytonread/Projects/omnivia-pm/docs/adr/ADR-038-provider-neutral-application-api-and-wire-compatibility.md`
- `/Users/claytonread/Projects/omnivia-pm/docs/specs/omnivia-core-architecture-spec-v0.6-2026-07-29.md`
- `/Users/claytonread/Projects/omnivia-pm/docs/tasks/2026-07-29-t0628-omnivia-core-phase-1-package-and-contract-skeleton.md`
- `/Users/claytonread/Projects/omnivia-pm/docs/tasks/2026-07-29-t0629-omnivia-core-phase-2-workspace-migrations-and-fencing.md`

## 12. Risks and controls

| Risk | Severity | Control |
|---|---:|---|
| Competing writers corrupt workspace state | Critical | Complete fencing before any multi-client rollout |
| Phase 2 helper protects a new path but leaves legacy writes unfenced | Critical | Mutation-call-site inventory and T-0629F real-seam cutover |
| Migration modifies the only legacy copy | Critical | Read-only source, verified backup, staging copy and exact rollback |
| Two streams redefine the same contract | High | File ownership matrix, frozen checkpoints and integration-controller shared files |
| Compatibility facade becomes a second model | High | Object-identity reexports and export-drift CI |
| Runtime leaks into public Core or adapters | High | AST/TOML dependency guards and isolated installs |
| API and service implementations diverge | High | One schema source and shared cross-transport conformance suite |
| CLI/MCP gain storage authority | Critical | Service-client-only architecture and explicit no-lease tests |
| Unsupported filesystems give false lock confidence | High | Two-process qualification and fail-closed writable behavior |
| Work is described as released before merge | Medium | Distinguish local accepted checkpoint from merged/released status |
| Later-phase scope begins without approval | High | Authorization labels and explicit stop gates in every handoff |

## 13. Definition of done

OmniVia Core development is complete for specification v0.6 only when:

- public contracts and compatibility behavior are stable and conformance tested;
- every canonical record explains evidence and authority;
- workspace identity, backup, migration and rollback are portable and tested;
- only one authoritative service can write a workspace;
- stale generations cannot commit after takeover, sleep or resume;
- CLI, MCP, Desktop and other clients never directly own the lease or write storage;
- a clean installation can run Core and connect Claude through MCP without Desktop or Dev;
- first-party clients pass the same application-contract conformance suite;
- ingestion, governance, projections, retrieval and Context Pack generation recover after interruption;
- unauthorized content never reaches ranking or model context;
- projection loss is rebuildable without canonical-data loss;
- local and future cloud implementations pass contract-parity tests;
- cross-platform release, upgrade, rollback, security and performance gates pass;
- PM tasks, ADR evidence and compatibility guidance are updated through their proper repository-owned workflow.

## 14. Immediate next actions

1. **Stream A / Codex:** complete A1 and record the T-0628 closeout checkpoint.
2. **Handoff agent:** independently review the completed work and prepare B0 evidence plus the T-0629 implementation/test plan.
3. **After T-0628 closeout:** create the temporary Stream B worktree from the accepted checkpoint and begin T-0629A.
4. **Integration controller:** review and integrate each Stream B slice in order.
5. **Architecture owner:** review and authorize the Phase 3 task packet before A2/B9 operational implementation.
