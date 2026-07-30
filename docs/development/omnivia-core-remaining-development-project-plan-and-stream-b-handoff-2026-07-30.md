# OmniVia Core Remaining Development Project Plan and Stream B Handoff

Date: 2026-07-30  
Status: Active development plan  
Architecture authority: accepted ADR-036, ADR-037, ADR-038 and OmniVia Core architecture specification v0.6  
Current implementation branch: `codex/ui-residual-risk-closure`  
Current reviewed committed checkpoint: `10fa17b`

Primary repository: `/Users/claytonread/Projects/omnivia-core`

## 1. Executive decision

The remaining development programme is divided into two coordinated streams.

| Stream | Lead | Responsibility |
|---|---|---|
| **Stream A — Contracts, compatibility and integration** | Codex / current development task | Complete the public contract migration, the `omnivia-memory` compatibility facade, provider-neutral API and wire conformance, package and release gates, consumer cutover planning, and final integration review. |
| **Stream B — Workspace authority and standalone runtime** | Handoff agent | Independently review the accepted foundation, then implement workspace format, migrations, backup and recovery, locks, leases, fencing and the independently runnable Core Service. After separate Phase 3+ approval, extend the service and implement the standalone CLI and MCP adapters. |

Codex will take **Stream A**.

**Stream B is already in progress under a separate agent.** This document is
the coordination and review baseline for that stream; Codex will not assign,
restart or implement Stream B work from the Stream A checkout.

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

Estimated completion of the current full OmniVia Core development goal remains **approximately 40%**.

This is an engineering estimate, not a simple count of architecture phases. The completed work establishes a large, heavily tested contract and migration foundation, while the highest-risk operational work—workspace authority, fencing, standalone service behavior, adapters, consumer cutover and production hardening—remains.

| Area | Estimated status | Position |
|---|---:|---|
| Phase 0 baseline freeze | 100% | Complete and reproducible |
| Phase 1 package and contract foundation | 98% | Local contract, behavior, compatibility-facade and acceptance-CI work is complete; publication, the required GitHub branch rule and formal closeout evidence remain |
| Phase 2 workspace, migrations and fencing | Separate stream active | Stream B is already in progress; no Stream B implementation checkpoint has yet been reviewed or integrated into this Stream A branch |
| Phase 3 provider-neutral service | Foundation only | Wire foundations exist; operation-level service behavior is not implemented |
| Phase 4 CLI and MCP | Skeleton only | Distributions exist; operational adapters are not implemented |
| Phases 5–7 | Mostly not started | Consumer cutover, full knowledge workflow and production hardening remain |

### 3.2 Repository state

- `10fa17b` is the latest independently reviewed and committed implementation checkpoint.
- The initial development and handoff plan is committed at `5679e3c`; the control-plane behavior proof is at `55f2489`; the accepted compatibility-facade foundation is at `4bbed05`; the previous plan refresh is at `a80ef6a`; and the local acceptance workflow is at `10fa17b`.
- The branch contains the reviewed 20-commit implementation lineage through
  `10fa17b` plus this status-document update; none of this lineage has been
  pushed from the current task.
- The working tree is clean at this handoff checkpoint.
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
| Remaining-development plan | `5679e3c` | Stream A/Stream B ownership, authorization boundaries, ordered roadmap and Stream B handoff |
| Control-plane behavior proof | `55f2489` | 54 legacy-equivalent validation cases plus 31 pure policy-compiler cases running against canonical Core |
| Compatibility-facade foundation | `4bbed05` | Five legacy leaves converted to exact canonical re-exports; fail-closed frozen-baseline normalization; exact package dependency; deterministic offline wheel metadata/artifact-install proof |
| Phase 0/1 acceptance CI | `10fa17b` | One stable, unfiltered `Core acceptance` job covering packages, contracts, TypeScript, migration, baseline, full suite, benchmarks, Ruff, mypy and committed-range diff checks; 17 structural regression tests |

The canonical migration now covers:

- 31 registered canonical leaf modules;
- 24 source-parity legacy contract ports, five accepted exact-identity facade leaves, plus the documented shared barrel/search-model exception;
- exact public barrels for knowledge, App Manifest, Component and Module contracts, graph, memory, workspace, ingestion, provenance, memory graph, run ledger and control plane;
- an exact 74-name control-plane public surface, routed to 55 model exports, 14 import exports and 5 validation exports;
- import-order, isolated-module closure, namespace, object-identity and static export tests;
- 85 focused control-plane behavior cases: 54 validation cases and 31 policy-compiler cases.

### 3.4 Verification evidence

The latest independent audit and host rerun report:

- 2,171 tests passing in the complete repository suite (`PYTHONPATH=.:src:services/omnivia-memory/src .venv/bin/pytest -q`);
- 5 pre-existing SWIG deprecation warnings in the PDF ingestion tests;
- 587 application-contract tests passing;
- 531 canonical-migration tests passing after the five converted leaves moved from source-parity coverage to stricter facade-identity coverage;
- 60 compatibility-facade tests passing with no skips;
- 17 acceptance-workflow regression tests passing;
- all 85 focused control-plane behavior tests passing;
- all 6 Phase 0 drift checks and 182 Phase 0 tests passing;
- all four wheels built and installed independently from a clean temporary wheelhouse;
- the Core wheel containing the expected 5 schemas and 14 application-contract fixtures, without runtime dependencies;
- Ruff clean over the applicable new source and tests;
- strict mypy clean over the canonical source;
- no blocking findings in the final control-plane barrel review.

The final control-plane review specifically verified exact export order, owner routing, runtime binding identity, star import behavior, child-module identities and closure under all three tested import orders.

### 3.5 Completed Phase 1 gap audits

#### Merge-blocking acceptance CI

The local repository now contains `.github/workflows/core-acceptance.yml` at
`10fa17b`, with one stable displayed job/check name: `Core acceptance`. It runs
for every pull request without path filtering and on manual dispatch. The
separate `.github/workflows/core-performance-report.yml` remains informational.

The accepted local workflow runs:

1. package dependency-boundary and package tests;
2. all four wheel builds and clean isolated-install checks;
3. application schema, fixture, codec and generated-artifact drift checks;
4. strict TypeScript compilation;
5. the canonical-migration suite;
6. Phase 0 drift checks and frozen-baseline tests;
7. the complete repository suite;
8. benchmark tests;
9. Ruff and strict mypy;
10. a pull-request-range `git diff --check`.

The workflow uses `contents: read`, Python 3.11, Node 22, full-history checkout,
fail-fast steps, PR base/head triple-dot diff checking and a committed
default-branch range for manual runs. Its 17 stdlib-only structural regression
tests bind required commands and configuration to their actual job and steps,
and ignore commented-out directives.

Two external/release controls remain:

- the branch must be published and the workflow must pass on a real GitHub
  runner;
- repository branch protection or a GitHub ruleset must require the stable
  `Core acceptance` check. Adding the YAML file alone does not make it
  merge-blocking.

Python dependency resolution also remains range-based because this repository
does not yet carry a Python lock or constraints file. The workflow records that
risk and deliberately does not present pip caching as reproducibility.

#### Compatibility-facade inventory and invariants

The completed source audit identified 47 supported legacy-to-canonical import-path pairs:

- 40 direct facade modules;
- 6 hybrid barrels: `graph`, `ingestion`, `ingestion.watcher`, `memory`, `memory_graph` and `workspace`;
- the `omnivia_memory` package root.

The legacy root advertises 183 ordered exports: 182 portable contract names plus `__version__`. `MemoryCreate` and `MemoryUpdate` are now portable canonical contracts. The hidden root bindings `Database` and `MemoryService` remain runtime owned.

The following 21 legacy paths are runtime-only and must not be moved into public Core merely to simplify the facade:

- `control_plane.registry`;
- `graph.repository`, `graph.search_service`, `graph.service`;
- `ingestion.chunker`, `ingestion.extractors`, `ingestion.pipeline`, `ingestion.repositories`, `ingestion.scanner`;
- `ingestion.watcher.debouncer`, `ingestion.watcher.tracker`;
- `memory.service`;
- `memory_graph.ingestion_adapter`, `memory_graph.store`;
- `persistence`, `persistence.database`, `persistence.repositories`;
- `search`, `search.service`;
- `workspace.repository`, `workspace.service`.

Facade imports must preserve exact object identity. The implementation must use direct canonical re-exports: no copied classes, subclasses, proxies, wrapper functions, dynamic `__getattr__` routing or `sys.modules` aliases. The canonical root `src/omnivia_core/__init__.py` must remain version-only; the legacy root must aggregate names from the owning canonical barrels.

Root-name collisions route as follows:

| Name | Canonical owner |
|---|---|
| `LifecycleState` | `omnivia_core.control_plane` |
| `ValidationResult` | `omnivia_core._shared.validation` |
| `SourceRef` | `omnivia_core.knowledge` |
| `ProvenanceRequirement` | `omnivia_core.component_contract` |
| `Source`, `SourceType` | `omnivia_core.provenance` |

The safe facade migration order is:

1. freeze the route manifest and facade-identity tests;
2. convert `_shared`, lifecycle, provenance and `memory.models`;
3. convert the remaining direct wrappers;
4. convert the six hybrid barrels while preserving their runtime-only exports locally;
5. convert the legacy root without expanding `omnivia_core.__init__`;
6. verify service-package dependencies, wheel contents and deprecation metadata;
7. move runtime behavior only through later Stream B or consumer-cutover tasks.

The first foundation slice—`_shared.validation`, `lifecycle.models`, `lifecycle.rules`, `provenance.models` and `memory.models`, plus the split copied-leaf/facade test oracle—is independently reviewed, accepted and committed at `4bbed05`.

That checkpoint also:

- preserves the full historical non-private leaf namespaces, including incidental bindings;
- requires every routed symbol to be the exact canonical object;
- keeps the frozen Phase 0 JSON unchanged and applies only verified, fail-closed in-memory ownership normalization;
- declares `omnivia-core>=0.1.0,<0.2.0` as an exact compatibility-distribution dependency;
- builds and installs both local wheel artifacts deterministically with no network or skip path, then verifies the installed metadata;
- deliberately defers a true dependency-resolver and full installed-root import smoke to the release wheelhouse/staging-index gate.

### 3.6 T-0628 closeout boundary

T-0628 requires **compatibility preparation**, not a claim that every legacy consumer has completed final facade cutover. It may close when the supported surface and runtime-only inventory are frozen, identity and export-drift requirements are executable, control-plane behavior is proved, package/application/canonical gates pass, and merge-blocking acceptance CI is in place.

Final facade cutover must be tracked separately and cannot be claimed until every supported direct, hybrid and root route has been converted and verified, runtime-owned behavior remains deliberately routed, and affected first-party consumers have completed their approved migration work. T-0628 closeout therefore unlocks Phase 2 Stream B; it does not mean the compatibility facade or cross-repository consumer migration is finished.

### 3.7 What completion has not yet proved

The current result must not be described as a complete standalone Core product. It has not yet proved:

- a fully converted identity-preserving `omnivia-memory` compatibility facade across all 47 supported root and submodule routes;
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
| M1 — Phase 1 closeout | Stream A | Approved | T-0628 compatibility-preparation, behavioral, CI and acceptance gaps closed; final facade cutover remains explicitly tracked |
| M2 — Workspace authority | Stream B | Approved after M1 | Full T-0629A–G and adversarial matrix pass |
| M3 — Provider-neutral service vertical slice | A contracts; B runtime | Requires Phase 3 approval | In-process and service transports pass the same semantic and wire conformance suite |
| M4 — Standalone CLI and MCP | B implementation; A conformance | Requires Phase 4 approval | Clean install can create/import a workspace and connect Claude without Desktop or Dev |
| M5 — Consumer cutover | A integration | Requires Phase 5 approval and repo-specific cross-repo tasks | Platform, Apps, Dev, CLI and MCP use the Core API and do not directly write Core storage |
| M6 — Complete knowledge workflow | A public contracts; B runtime | Requires later approval | Evidence import, candidate extraction, governance, retrieval and cited Context Pack milestone pass |
| M7 — Production hardening | Joint, integrated by Stream A | Requires later approval | Cross-platform bundles, upgrade/rollback, SBOM, security, performance and recovery gates pass |

## 5. Stream A plan — Codex-owned

### A0 — Land and protect the accepted foundation

Status: local acceptance CI is complete at `10fa17b`; publication, a real
GitHub runner result and the required branch rule remain. Publication is not
yet authorized.

Work:

1. Push or open a pull request for the reviewed 20-commit lineage through `10fa17b` when publication is authorized.
2. **Complete locally:** add acceptance CI for:
   - package dependency boundaries and package tests;
   - all four wheel builds and isolated installs;
   - application schema, fixture, codec and generated-artifact drift;
   - strict TypeScript compilation;
   - canonical migration;
   - Phase 0 drift and baseline tests;
   - the full repository suite;
   - benchmark tests;
   - Ruff and strict mypy;
   - pull-request-range diff checking.
3. **Workflow complete; external setting pending:** use one stable `Core acceptance` check and configure the repository branch ruleset to require it after publication.
4. Preserve the informational performance workflow but do not treat it as a replacement for merge gates.
5. Record the accepted commit and exact commands in PM evidence through a separate PM-owned change.

Exit:

- the accepted foundation is reviewable as one visible lineage;
- all existing local acceptance gates run in CI;
- no generated or build artifacts remain in the repository.

### A1 — Close T-0628 compatibility and behavior

Status: ready for publication-backed closeout. Control-plane behavior proof is
complete at `55f2489`; compatibility-facade preparation is accepted at
`4bbed05`; and local acceptance CI is accepted at `10fa17b`. A real GitHub run,
required branch rule and formal PM evidence remain.

Work:

1. **Complete:** port 54 pure validation cases from the legacy control-plane contract to canonical Core tests without moving runtime behavior.
2. **Complete:** port 31 pure `compile_policy_expression` cases from the legacy registry suite.
3. **Complete:** freeze the supported legacy route and root-export inventory:
   - 47 path pairs: 40 direct, 6 hybrid and the package root;
   - 183 ordered root exports: 182 portable names plus `__version__`;
   - 21 runtime-only legacy paths;
   - hidden root compatibility bindings `Database` and `MemoryService` remain runtime owned.
4. **Complete:** convert the facade foundation (`_shared.validation`, lifecycle, provenance and `memory.models`) and split copied-leaf parity from facade-identity testing.
5. Track conversion of the remaining direct wrappers, six hybrid barrels and the package root as final facade-cutover work after T-0628 compatibility-preparation closeout.
6. Keep `src/omnivia_core/__init__.py` version-only and route legacy root collisions to their frozen canonical owners.
7. Route runtime-only exports deliberately; do not duplicate their models in Core.
8. **Complete for the foundation slice:** add exact object-identity, import, export-drift and deterministic wheel/dependency-metadata tests. Extend the same invariants to every later facade route and add deprecation metadata before publication.
9. **Workflow complete at `10fa17b`; external setting pending:** publish and require the stable `Core acceptance` check through the repository branch rule.
10. Re-run the complete T-0628 acceptance suite and record a formal accepted checkpoint.

T-0628 exit:

- the supported route and runtime-only inventory is frozen;
- exact-identity and export-drift requirements are executable;
- canonical control-plane validation and policy-compiler behavior is proved;
- package, application-contract, canonical-migration, baseline and full-suite gates pass in required CI;
- T-0628 can move from `In Progress` to `Done` without claiming final consumer/facade cutover.

Final facade-cutover exit, tracked after T-0628:

- every supported direct, hybrid and root route resolves to the exact canonical contract object;
- no parallel public domain model remains;
- runtime-owned paths remain explicit and functional;
- affected consumers complete separately authorized migrations;
- deprecation and eventual removal conditions from ADR-036 are preserved.

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

Status: already in progress under the separately assigned Stream B agent.
Codex does not manage or duplicate that implementation from Stream A.

The Stream B agent reviews rather than assumes the foundation.

Work:

1. Confirm `10fa17b` is the current Stream A reviewed checkpoint and inspect all 20 commits from `117cf83` through `10fa17b`.
2. Reproduce package builds and isolated installs.
3. Reproduce dependency, schema, fixture, generated-artifact, canonical-migration and Phase 0 gates.
4. Reproduce the 85 focused control-plane behavior tests, 531-test canonical-migration suite, 60-test compatibility-facade suite, 17-test acceptance-workflow suite and 2,171-test full suite.
5. Review the control-plane barrel, the accepted five-leaf facade foundation, T-0628 compatibility inventory and merge-gate audit.
6. Reproduce the facade identity, namespace, frozen-baseline and deterministic wheel metadata/artifact-install proofs.
7. Report any discrepancy before writing Phase 2 code.
8. Do not start T-0629 operational implementation until Stream A records the T-0628 closeout checkpoint.

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
   T-0628 closeout means its compatibility-preparation and acceptance gates
   pass; it does not claim final facade or consumer cutover.
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
- after T-0628 closeout, Stream A's remaining direct/hybrid/root facade conversion alongside Stream B's Phase 2 runtime work, provided `workspace/models.py`, `workspace/__init__.py`, root packaging and other shared files remain integration-controller owned;
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

1. Do the 20 commits from `117cf83` through `10fa17b` match ADR-036, ADR-038 and the Phase 0/1 task packets?
2. Do the 85 control-plane behavior cases accurately preserve the 54 validation and 31 pure policy-compiler cases from their frozen legacy sources?
3. Does the 47-route compatibility inventory make exact identity, collision routing, hybrid/runtime ownership and the version-only canonical root enforceable without proxies or duplicate models?
4. Do all four packages build and install independently?
5. Are dependency guardrails structural rather than text-only?
6. Are generated artifacts deterministic and derived from one schema source?
7. Does tolerant production decoding remain distinct from strict conformance validation?
8. Are any runtime or storage dependencies leaking into public Core contracts?
9. Does the proposed `Core acceptance` workflow cover every local Phase 0/1 gate, and is the separate branch-ruleset requirement recorded?
10. Does the proposed T-0629 implementation protect the real current write seam?
11. Are any Phase 3+ features being attempted without approval?

Any blocker must cite an exact file, test or acceptance criterion. Review observations that do not block T-0629 should be recorded as Stream A follow-up rather than silently broadening Stream B.

## 11. Handoff brief for the Stream B agent

Use the following as the receiving agent's task:

> You own Stream B — Workspace Authority and Standalone Runtime — for OmniVia Core. Codex owns Stream A — Contracts, Compatibility and Integration. The architecture authority is the accepted PM copies of ADR-036, ADR-037, ADR-038 and architecture specification v0.6. The owner approved Phases 0–2 only. Do not implement broad Phase 3 service operations, the full CLI/MCP, consumer cutover or production distribution without their later approved task packets.
>
> The current Stream A review baseline is the 20 committed checkpoints from `117cf83` through `10fa17b` on branch `codex/ui-residual-risk-closure`. `10fa17b` is the current reviewed committed checkpoint. The plan itself is committed at `5679e3c` and refreshed at `a80ef6a`; the control-plane behavior proof is at `55f2489`; the accepted facade foundation is at `4bbed05`; and the local acceptance workflow is at `10fa17b`. Stream B is already in progress independently; do not restart or redirect it merely because this Stream A baseline advanced.
>
> Reproduce the package-boundary tests, all four wheel builds and isolated installs, application schema/fixture/codec/generated-artifact gates, strict TypeScript compilation, Phase 0 drift/baseline gates, Ruff, strict mypy and diff checks. Reproduce the 85 focused control-plane behavior cases (54 validation and 31 policy-compiler), the 531-test canonical-migration suite, the 60-test compatibility-facade suite, the 17-test acceptance-workflow suite and the 2,171-test full suite; the known full-suite result includes five pre-existing SWIG deprecation warnings and no skipped tests. Report discrepancies with exact files, commands and outputs.
>
> Review the completed audits as evidence, not assumptions. There are 47 supported legacy-to-canonical route pairs: 40 direct, six hybrid barrels and the package root. The legacy root advertises 183 ordered exports: 182 portable names plus `__version__`. Twenty-one legacy paths remain runtime only. Exact facade identity forbids copies, subclasses, proxies, wrapper functions, dynamic `__getattr__` routing and `sys.modules` aliases. `src/omnivia_core/__init__.py` remains version-only. The frozen collision routes are `LifecycleState` → control plane, `ValidationResult` → shared validation, `SourceRef` → knowledge, `ProvenanceRequirement` → Component Contract, and `Source`/`SourceType` → provenance. Verify the recorded migration order and the accepted five-leaf facade foundation at `4bbed05`. Do not claim final facade cutover: the remaining direct, hybrid and root routes are still tracked work.
>
> Also review the accepted local workflow at `.github/workflows/core-acceptance.yml`. Its stable `Core acceptance` job covers package tests, four-wheel isolated installs, application schema/fixture/codec/generated drift, strict TypeScript, canonical migration, Phase 0 drift/baseline, full suite, benchmarks, Ruff, strict mypy and committed-range diff checking. The separate performance workflow remains informational. The acceptance workflow has not yet run on a published GitHub branch, and a repository branch rule or ruleset must separately require the stable check before T-0628 closes.
>
> T-0628 compatibility preparation may close before final facade and consumer cutover. Do not begin operational T-0629 implementation until Codex records that formal closeout checkpoint. While waiting, prepare only read-only Phase 2 evidence: the T-0629 test plan, migration fixture oracle, mutation-call-site inventory, fake process/clock evidence and multi-process adversarial harness design. Do not modify PM-owned files.
>
> After the gate, create a dedicated Stream B worktree from the accepted T-0628 closeout checkpoint and implement T-0629A–G in order. Keep lease, fencing, runtime database and legacy delegate changes in one serial committed lineage. Use public `omnivia_core` contracts; do not create a competing runtime domain model. Add T-0629A manifest files alongside the existing workspace compatibility surface; do not edit `workspace/models.py` or `workspace/__init__.py` unilaterally. Do not edit Stream A contract/API paths, `.github/**`, root packaging, lockfiles or other shared files without an integration request.
>
> Full CLI and MCP implementation is not authorized under Phases 0–2. Phase 2 may add only the shared discovery/bootstrap contracts and client simulation necessary to prove that clients never own the authoritative lease. Stop at the minimal independently runnable service and the complete 116-case adversarial exit gate unless later approval is provided.
>
> Return after the initial review and after every implementation slice: summary, exact files, diff review, tests and commands, failures or warnings, architecture concerns, dependency impact, and the commit proposed for integration. Cite blockers to exact files, tests or acceptance criteria; do not silently broaden the task.

Required reading:

- `/Users/claytonread/Projects/omnivia-core/docs/development/omnivia-core-remaining-development-project-plan-and-stream-b-handoff-2026-07-30.md`
- `/Users/claytonread/Projects/omnivia-core/AGENTS.md`
- `/Users/claytonread/Projects/omnivia-pm/AGENTS.md`
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
| Compatibility facade becomes a second model | High | Exact-identity direct re-exports; forbid copies, subclasses, proxies and dynamic routing; enforce export-drift CI |
| Informational CI is mistaken for a merge gate | High | Add the stable `Core acceptance` workflow and require it through branch protection/rulesets |
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

1. **Complete — Stream A / Codex:** independently review, repair, accept and commit the five-leaf facade foundation at `4bbed05`.
2. **Local work complete; external action pending — Stream A / Codex:** publish the `10fa17b` lineage when authorized, obtain a real `Core acceptance` runner result, configure the required branch rule and record the formal T-0628 closeout evidence.
3. **In progress independently — Stream B:** continue under its existing agent and scope. Stream A does not restart, duplicate or redirect that work; `10fa17b` is available as the next reviewed coordination baseline.
4. **After T-0628 closeout:** create the temporary Stream B worktree from the accepted checkpoint and begin T-0629A while Stream A continues non-overlapping facade conversion.
5. **Integration controller:** review and integrate each Stream B slice in order.
6. **Architecture owner:** review and authorize the Phase 3 task packet before A2/B9 operational implementation.
