# OmniVia Core Remaining Development Project Plan and Stream B Handoff

Date: 2026-07-30
Status: Active development plan
Architecture authority: accepted PM ADR-036, PM ADR-037, PM ADR-038 and OmniVia Core architecture specification v0.6
Current implementation branch: `codex/ui-residual-risk-closure`
Current reviewed committed checkpoint: `a1b1466`

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

Completing all 47 compatibility routes at `a1b1466` finishes the Phase 1 local
implementation, but it does not move the overall estimate, because the remaining
work is dominated by external Phase 1 closeout and by Phases 2–7, none of which
the facade cutover advances.

| Area | Estimated status | Position |
|---|---:|---|
| Phase 0 baseline freeze | 100% | Complete and reproducible |
| Phase 1 package and contract foundation | Local implementation complete | Contract, behavior, full 47-route compatibility-facade cutover and acceptance-CI work is complete and locally verified at `a1b1466`; publication or PR, a hosted `Core acceptance` run, the required GitHub branch rule and formal closeout evidence remain |
| Phase 2 workspace, migrations and fencing | Separate stream active | Stream B is already in progress; no Stream B implementation checkpoint has yet been reviewed or integrated into this Stream A branch |
| Phase 3 provider-neutral service | Foundation only | Wire foundations exist; operation-level service behavior is not implemented |
| Phase 4 CLI and MCP | Skeleton only | Distributions exist; operational adapters are not implemented |
| Phases 5–7 | Mostly not started | Consumer cutover, full knowledge workflow and production hardening remain |

### 3.2 Repository state

- `a1b1466` is the latest independently reviewed and committed implementation checkpoint.
- The initial development and handoff plan is committed at `5679e3c`; the control-plane behavior proof is at `55f2489`; the accepted compatibility-facade foundation is at `4bbed05`; the previous plan refresh is at `a80ef6a`; the local acceptance workflow is at `10fa17b`; the frozen compatibility route registry is at `611df60`; the hybrid barrels are accepted at `f3774ae`; and the compatibility root facade is complete at `a1b1466`.
- Seventeen further commits after `10fa17b` — `63299a8` through `a1b1466` —
  completed the compatibility facade cutover. The branch now contains the
  reviewed 37-commit implementation lineage from `117cf83` through `a1b1466`
  plus this status-document update; none of the implementation lineage has been
  pushed from the current task.
- `a1b1466` was a clean implementation checkpoint, and this status document was
  the only change prepared on top of it for the terminal handoff record.
- The work is locally accepted and verified but is not yet merged, published or released.
- The accepted PM copies of PM ADR-036, PM ADR-037, PM ADR-038 and specification v0.6 are authoritative. Copies previously supplied from Downloads must not override the accepted PM versions.

**Stream B baseline reconciliation.** Sections 6, 10, 11 and immediate action 4
deliberately retain `10fa17b` as the Stream B coordination baseline, with the
commit counts and suite sizes recorded there at that checkpoint. They are not
edited here, because changing an in-flight stream's instructions is a separate
coordinated action, not a status refresh. Section 3 is authoritative for current
Stream A state: where the two differ, section 3 is the later measurement and
`a1b1466` is the current Stream A checkpoint.

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
| Frozen compatibility route registry | `611df60` | Hand-maintained `compatibility/facade-routes.v1.json` and its schema, the `scripts/check-facade-routes.py` checker and the `baseline/facade_manifest.py` oracle binding every legacy module, canonical owner, pair kind, shape and migration state |
| Direct facade conversion | `e660d97`–`6631c5c` | All 40 direct routes converted to exact canonical re-exports — App Shell bridge, App Manifest, Component Contract, Module Manifest, run ledger, control plane, knowledge, memory graph, graph, ingestion and workspace models — with typed-consumer and export-drift coverage |
| Hybrid barrel conversion | `f3774ae` | `graph`, `ingestion`, `ingestion.watcher`, `memory`, `memory_graph` and `workspace` barrels converted while their runtime-only exports stay legacy-owned |
| Compatibility root facade | `a1b1466` | `omnivia_memory` package root converted to an exact ordered canonical re-export surface, plus deprecation metadata, dependency/wheel provenance proofs and the standalone current-index resolver smoke |

The compatibility facade cutover is complete locally. All 47 supported routes
are converted: 40 direct, 6 hybrid barrels and the package root. The frozen
registry records `direct_facade` 29, `split_facade` 1, `transitive_facade` 10,
`hybrid_facade` 6 and `root_facade` 1; every `pending_*` count, together with
`source_parity` and `canonical_subset`, is zero, and the checker's remaining-work
line reads `0 leaves, 0 barrels and 0 roots still to convert`. The root is
counted in that line rather than excluded from it, so the proof cannot read
"nothing remaining" while the route publishing the whole advertised surface is
still a duplicate.

The canonical migration now covers:

- 31 registered canonical leaf modules;
- 47 converted compatibility routes and 21 deliberately runtime-only legacy paths, all bound by the frozen registry and its checker;
- exact public barrels for knowledge, App Manifest, Component and Module contracts, graph, memory, workspace, ingestion, provenance, memory graph, run ledger and control plane;
- an exact 74-name control-plane public surface, routed to 55 model exports, 14 import exports and 5 validation exports;
- import-order, isolated-module closure, namespace, object-identity and static export tests;
- typed facade-consumer modules compiled under strict mypy for the direct, hybrid and root surfaces;
- 85 focused control-plane behavior cases: 54 validation cases and 31 policy-compiler cases.

### 3.4 Verification evidence

The final local results at `a1b1466` are:

- 3,271 tests passing and 2 skipped in the complete repository suite (`PYTHONPATH=.:src:services/omnivia-memory/src .venv/bin/python -m pytest -q`), with the five existing SWIG deprecation warnings in the PDF ingestion tests;
- 1,653 passing and 2 skipped in the combined canonical-migration and compatibility suites (`python -m pytest tests/canonical_migration tests/compatibility -q`);
- 67 distribution-provenance tests passing (`tests/compatibility/test_root_facade_distribution.py`);
- 587 application-contract tests passing, and the application schema, fixture, codec and generated-artifact conformance check passing;
- all 6 Phase 0 baseline drift checks and 746 baseline tests passing;
- 23 benchmark tests passing;
- the route-registry checker reporting 47 routes (40 direct, 6 hybrid_barrel, 1 root), 30 leaves, 16 barrels, 1 root, `direct_facade` 29, `split_facade` 1, `transitive_facade` 10, `hybrid_facade` 6, `root_facade` 1, zero for every pending state and for `source_parity` and `canonical_subset`, 21 runtime-only modules, and `0 leaves, 0 barrels and 0 roots still to convert`;
- the standalone current-index resolver smoke passing (`scripts/check-root-facade-resolver.py`);
- all four wheels built and installed independently from a clean temporary wheelhouse, the Core wheel carrying the expected 5 schemas and 14 application-contract fixtures without runtime dependencies, and no sibling, legacy or validation module importable from the isolated Core environment;
- strict TypeScript compilation of the generated application contracts passing;
- Ruff clean over the accepted scope;
- strict mypy clean over 98 source files;
- the package dependency-boundary check passing;
- `git diff --check` clean.

The earlier plan recorded 1,617 passing for the combined canonical-migration and
compatibility suites. The measured result at this checkpoint is 1,653 passing
and 2 skipped; the higher number is the verified one and supersedes it.

The final independent semantic review and the final independent test review both
returned GO for this checkpoint, with no blocking findings.

### 3.5 Completed Phase 1 gap audits

#### Merge-blocking acceptance CI

The local repository contains `.github/workflows/core-acceptance.yml`, introduced
at `10fa17b` and extended through `a1b1466`, with one stable displayed job/check
name: `Core acceptance`. It runs for every pull request without path filtering
and on manual dispatch. The separate
`.github/workflows/core-performance-report.yml` remains informational.

The accepted local workflow runs:

1. package dependency-boundary and package tests;
2. all four wheel builds and clean isolated-install checks;
3. application schema, fixture, codec and generated-artifact drift checks;
4. strict TypeScript compilation;
5. the compatibility route-registry check;
6. the canonical-migration and compatibility suites;
7. the standalone current-index resolver and installed-root smoke;
8. Phase 0 drift checks and frozen-baseline tests;
9. the complete repository suite;
10. benchmark tests;
11. Ruff and strict mypy;
12. a pull-request-range `git diff --check`.

The workflow uses `contents: read`, Python 3.11, Node 22, full-history checkout,
fail-fast steps, PR base/head triple-dot diff checking and a committed
default-branch range for manual runs. Its 25 stdlib-only structural regression
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

The completed source audit identified 47 supported legacy-to-canonical
import-path pairs, and all 47 are now converted at `a1b1466`:

- 40 direct facade modules;
- 6 hybrid barrels: `graph`, `ingestion`, `ingestion.watcher`, `memory`, `memory_graph` and `workspace`;
- the `omnivia_memory` package root.

The hand-maintained registry `compatibility/facade-routes.v1.json` is the
authority for per-route state, and `scripts/check-facade-routes.py` fails
whenever the registry and the package trees disagree. Its current partition is
`direct_facade` 29, `split_facade` 1, `transitive_facade` 10, `hybrid_facade` 6
and `root_facade` 1; `source_parity`, `canonical_subset` and every `pending_*`
state are zero, and the checker reports `0 leaves, 0 barrels and 0 roots still
to convert`. The registry is deliberately hand-edited: a registry regenerated
from the tree it constrains would check nothing.

The legacy root advertises exactly 183 ordered exports: 182 portable contract
names plus `__version__`, in a frozen literal order pinned by
`ROOT_FACADE_ALL`. `__version__` is imported from the canonical root rather than
restated, and `__all__` is the module's only assignment. Four non-advertised
compatibility bindings remain importable and stay out of `__all__`:
`MemoryCreate` and `MemoryUpdate`, which are canonical Core objects, and
`Database` and `MemoryService`, which are the exact legacy objects owned by
`omnivia_memory.persistence` and `omnivia_memory.memory.service` and are
declared runtime-only. The importable non-module binding set is therefore
exactly 187 — `__all__` plus those four — so no extra public binding, including
an `annotations` future feature, can appear.

Those 187 bindings are imported from 13 modules: 11 canonical owners
(`omnivia_core`, `omnivia_core._shared.validation`, `omnivia_core.app_manifest`,
`omnivia_core.component_contract`, `omnivia_core.control_plane`,
`omnivia_core.knowledge`, `omnivia_core.memory.models`,
`omnivia_core.memory_graph`, `omnivia_core.module_manifest`,
`omnivia_core.provenance`, `omnivia_core.run_ledger`) plus the two runtime
owners `omnivia_memory.persistence` and `omnivia_memory.memory.service`. Every
advertised name resolves to the exact canonical object, and the canonical root
`src/omnivia_core/__init__.py` remains version-only — checked in the same pass,
because that fact is the root state's whole justification. Converting the root
moved no runtime code into Core: `Database` and `MemoryService` are still
legacy-owned, still resolved out of `services/omnivia-memory`, and still absent
from the Core wheel.

The root source is constrained to a docstring, absolute unaliased non-star
from-imports and one literal `__all__` last: no relative or star import, no
alias, no plain `import x`, no definition, no second assignment, no module
`__getattr__`/`__dir__` and no `sys.modules` routing.

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

The planned facade migration order was followed and is now complete through
step 6:

1. freeze the route manifest and facade-identity tests — done at `611df60`;
2. convert `_shared`, lifecycle, provenance and `memory.models` — done at `4bbed05`;
3. convert the remaining direct wrappers — done through `e660d97`–`6631c5c`;
4. convert the six hybrid barrels while preserving their runtime-only exports locally — done at `f3774ae`;
5. convert the legacy root without expanding `omnivia_core.__init__` — done at `a1b1466`;
6. verify service-package dependencies, wheel contents and deprecation metadata — done at `a1b1466`;
7. move runtime behavior only through later Stream B or consumer-cutover tasks — **not started, and deliberately out of scope here.**

The foundation slice at `4bbed05` also established the invariants that every
later route inherits:

- preserves the full historical non-private leaf namespaces, including incidental bindings;
- requires every routed symbol to be the exact canonical object;
- keeps the frozen Phase 0 JSON unchanged and applies only verified, fail-closed in-memory ownership normalization;
- declares `omnivia-core>=0.1.0,<0.2.0` as an exact compatibility-distribution dependency;
- builds and installs both local wheel artifacts deterministically with no network or skip path, then verifies the installed metadata.

#### Deprecation metadata, dependency and wheel proofs

`services/omnivia-memory/pyproject.toml` now carries the PM ADR-036 deprecation
notice in release metadata rather than at runtime. The distribution summary
announces the deprecated facade and names `omnivia_core` as the replacement;
`[project.urls] Migration` pins a non-placeholder repository URL intended to hold
the README migration section; and the declared runtime requirements remain exactly
`omnivia-core>=0.1.0,<0.2.0` and `sqlalchemy>=2.0.0`. The notice is deliberately
not a `warnings.warn`, log line or anything else that runs at import time and
pollutes a consumer's output, and importing the packaged root is asserted to stay
silent.

The 67-test distribution-provenance suite
(`tests/compatibility/test_root_facade_distribution.py`) proves the built wheel's
summary, pinned migration URL, non-placeholder URL target, exact declared
requirements, packaged migration guidance and import-time silence; that the
resolver gate is a standalone script outside pytest collection; that no
pytest-collected test performs a resolving install; that the resolver script is
fail-closed with no offline fallback and gives every subprocess an explicit
timeout; the full accept/reject matrix for pip's resolution report; and that the
route registry supplies exactly 47 module pairs.

#### Standalone current-index resolver smoke

`scripts/check-root-facade-resolver.py` is a standalone acceptance gate kept
outside pytest collection, because it performs a resolving install that may
contact the configured index and must run exactly once per acceptance run. It
builds both wheels fresh outside the repository tree, creates an isolated
Python 3.11 virtual environment, installs `omnivia-memory` normally — no
`--no-deps`, no `--no-index` — with the locally built Core wheel offered through
`--find-links`, and asks pip for a machine-readable resolution report. From that
report it proves the `omnivia-core` entry is a `file://` URL with a strictly
empty authority resolving to the exact Core wheel this run built, with a
present, well-formed SHA-256 archive hash matching that file's bytes; a missing
or malformed hash fails closed. It then re-reads the installed distribution
metadata to confirm the deprecation notice and exact dependency contract survived
the install round trip, imports all 47 legacy route modules and their 47
canonical counterparts out of the installed tree, and runs the root
identity/export audit there against the frozen in-repository contract. It is
fail-closed: no skip path, no offline fallback, explicit timeouts throughout.

Its limitations are stated honestly and must not be overread. The SQLAlchemy
origin check asserts only that pip's report attributed that distribution to an
`http`/`https` candidate URL with a nonempty parsed host, a usable port and no
whitespace or ASCII control characters in the host. It does not contact the host,
validate DNS resolvability, establish artifact identity, distinguish a local
HTTP-cache hit from a fresh download, or prove any network transfer occurred. It
is a *current-index* smoke, not a locked or reproducible supply-chain proof: this
repository still has no Python lockfile or constraints file, and the
`pyproject.toml` pins are ranges. It is also not evidence about published
artifacts — everything installed is either built from this checkout or supplied
by whatever candidate the configured index offers at run time. A proof against
published artifacts on a controlled staging index, with their final metadata and
signatures, remains an external release gate.

### 3.6 T-0628 closeout boundary

T-0628 requires **compatibility preparation**, not a claim that every legacy consumer has completed final facade cutover. It may close when the supported surface and runtime-only inventory are frozen, identity and export-drift requirements are executable, control-plane behavior is proved, package/application/canonical gates pass, and merge-blocking acceptance CI is in place.

Final facade cutover must be tracked separately and cannot be claimed until every supported direct, hybrid and root route has been converted and verified, runtime-owned behavior remains deliberately routed, and affected first-party consumers have completed their approved migration work. T-0628 closeout therefore unlocks Phase 2 Stream B; it does not mean the compatibility facade or cross-repository consumer migration is finished.

At `a1b1466` the first three of those conditions are met locally: all 47 routes
are converted and verified, and runtime-owned behavior remains deliberately
routed and legacy-owned. The fourth is not. **No first-party consumer migration
has been performed**, in this repository or any other, and none is authorized.
Cross-repository consumer cutover remains Phase 5 work behind its own approval
gate, so the compatibility facade must stay in place under the PM ADR-036
deprecation and removal conditions.

### 3.7 What completion has not yet proved

The current result must not be described as a complete standalone Core product.
The 47-route compatibility facade is converted and identity-preserving as a
**local, unpublished implementation**; that is the extent of the claim. The
current result has not yet proved:

- the same facade against published artifacts on a controlled staging index, with final metadata and signatures;
- merge-blocking CI for all new acceptance gates, on a real GitHub runner;
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
| M1 — Phase 1 closeout | Stream A | Approved | T-0628 compatibility-preparation, behavioral, CI and acceptance gaps closed. Local implementation complete at `a1b1466`, including the full 47-route facade cutover; publication, the hosted acceptance run, the required branch rule and formal evidence remain, and consumer cutover stays separately gated |
| M2 — Workspace authority | Stream B | Approved after M1 | Full T-0629A–G and adversarial matrix pass |
| M3 — Provider-neutral service vertical slice | A contracts; B runtime | Requires Phase 3 approval | In-process and service transports pass the same semantic and wire conformance suite |
| M4 — Standalone CLI and MCP | B implementation; A conformance | Requires Phase 4 approval | Clean install can create/import a workspace and connect Claude without Desktop or Dev |
| M5 — Consumer cutover | A integration | Requires Phase 5 approval and repo-specific cross-repo tasks | Platform, Apps, Dev, CLI and MCP use the Core API and do not directly write Core storage |
| M6 — Complete knowledge workflow | A public contracts; B runtime | Requires later approval | Evidence import, candidate extraction, governance, retrieval and cited Context Pack milestone pass |
| M7 — Production hardening | Joint, integrated by Stream A | Requires later approval | Cross-platform bundles, upgrade/rollback, SBOM, security, performance and recovery gates pass |

## 5. Stream A plan — Codex-owned

### A0 — Land and protect the accepted foundation

Status: local acceptance CI is complete and extended through `a1b1466`;
publication, a real GitHub runner result and the required branch rule remain.
Publication is not yet authorized.

Work:

1. Push or open a pull request for the reviewed 37-commit lineage from `117cf83` through `a1b1466` when publication is authorized.
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

Status: local implementation complete at `a1b1466` and independently reviewed
GO on both semantics and tests. Control-plane behavior proof is complete at
`55f2489`; the facade foundation is accepted at `4bbed05`; local acceptance CI is
accepted at `10fa17b`; and the full 47-route facade cutover is complete at
`a1b1466`. Publication or PR, a hosted `Core acceptance` run, the required branch
rule and formal PM evidence remain.

Work:

1. **Complete:** port 54 pure validation cases from the legacy control-plane contract to canonical Core tests without moving runtime behavior.
2. **Complete:** port 31 pure `compile_policy_expression` cases from the legacy registry suite.
3. **Complete:** freeze the supported legacy route and root-export inventory:
   - 47 path pairs: 40 direct, 6 hybrid and the package root;
   - 183 ordered root exports: 182 portable names plus `__version__`;
   - 21 runtime-only legacy paths;
   - hidden root compatibility bindings `Database` and `MemoryService` remain runtime owned.
4. **Complete:** convert the facade foundation (`_shared.validation`, lifecycle, provenance and `memory.models`) and split copied-leaf parity from facade-identity testing.
5. **Complete:** convert the remaining direct wrappers, the six hybrid barrels and the package root. The registry reports `direct_facade` 29, `split_facade` 1, `transitive_facade` 10, `hybrid_facade` 6, `root_facade` 1, zero pending in every state, and `0 leaves, 0 barrels and 0 roots` remaining.
6. **Complete:** `src/omnivia_core/__init__.py` remains version-only, and legacy root collisions route to their frozen canonical owners.
7. **Complete:** the 21 runtime-only exports are routed deliberately and stay legacy-owned; no runtime model was duplicated in Core.
8. **Complete:** exact object-identity, import, export-drift, typed-consumer and deterministic wheel/dependency-metadata tests cover every route, and deprecation metadata is in place on the compatibility distribution.
9. **Workflow complete and extended at `a1b1466`; external setting pending:** publish and require the stable `Core acceptance` check through the repository branch rule.
10. **Complete locally; formal record pending:** the complete T-0628 acceptance suite was re-run at `a1b1466` with the results in section 3.4. Recording the formal accepted checkpoint in PM evidence is a separate PM-owned change.

Remaining external closeout for A1, none of it performed here:

- publish the branch or open the pull request when authorized;
- obtain a passing hosted `Core acceptance` run on a real GitHub runner;
- configure the required branch protection or ruleset for the stable check;
- record the formal T-0628 closeout evidence through the PM-owned workflow;
- release and consumer cutover, which stay behind their own later gates.

T-0628 exit:

- the supported route and runtime-only inventory is frozen;
- exact-identity and export-drift requirements are executable;
- canonical control-plane validation and policy-compiler behavior is proved;
- package, application-contract, canonical-migration, baseline and full-suite gates pass in required CI;
- T-0628 can move from `In Progress` to `Done` without claiming final consumer/facade cutover.

Final facade-cutover exit, tracked after T-0628:

- **Met locally at `a1b1466`:** every supported direct, hybrid and root route resolves to the exact canonical contract object;
- **Met locally at `a1b1466`:** no parallel public domain model remains;
- **Met locally at `a1b1466`:** runtime-owned paths remain explicit and functional;
- **Not met:** affected consumers complete separately authorized migrations — none has been authorized or started;
- **Preserved, not yet exercised:** deprecation and eventual removal conditions from PM ADR-036 are declared in the compatibility distribution's metadata and README.

### A2 — Complete the provider-neutral application contract

Status: **NOT completed.** Design may be refined now; implementation beyond the
Phase 1 foundation requires Phase 3 approval, which has not been given. None of
the work items below has been implemented. Nothing at `a1b1466` implements
provider-neutral service operations, a service runtime or transport, or the CLI
and MCP adapters.

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

1. Do the 20 commits from `117cf83` through `10fa17b` match PM ADR-036, PM ADR-038 and the Phase 0/1 task packets?
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

> You own Stream B — Workspace Authority and Standalone Runtime — for OmniVia Core. Codex owns Stream A — Contracts, Compatibility and Integration. The architecture authority is the accepted PM copies of PM ADR-036, PM ADR-037, PM ADR-038 and architecture specification v0.6. The owner approved Phases 0–2 only. Do not implement broad Phase 3 service operations, the full CLI/MCP, consumer cutover or production distribution without their later approved task packets.
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
2. **Complete — Stream A / Codex:** convert, review and accept all 47 compatibility routes through `a1b1466`, with both the final independent semantic review and the final independent test review returning GO.
3. **External action pending — Stream A / Codex:** publish the `117cf83`–`a1b1466` lineage or open its pull request when authorized, obtain a passing hosted `Core acceptance` runner result, configure the required branch protection or ruleset, and record the formal T-0628 closeout evidence through the PM-owned workflow. Release and consumer cutover follow later, behind their own gates.
4. **In progress independently — Stream B:** continue under its existing agent and scope. Stream A does not restart, duplicate or redirect that work; `10fa17b` remains the recorded coordination baseline until a separate coordinated update advances it.
5. **After T-0628 closeout:** create the temporary Stream B worktree from the accepted checkpoint and begin T-0629A.
6. **Integration controller:** review and integrate each Stream B slice in order.
7. **Architecture owner:** review and authorize the Phase 3 task packet before A2/B9 operational implementation.
