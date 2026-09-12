# OmniVia Core Organisational Model Phase 1 Acceptance Handoff

Date: 2026-09-12
Status: **GO — accepted and merged**
Specification authority: `SPEC-CORE-SEM-001`, version 0.2, dated 2026-09-11
Approved implementation scope: Phase 1 — Manual Semantic Registry
Implementation branch: `codex/core-organisational-model-v02`
Implementation commit: `dd72159` (`feat(core): implement phase 1 semantic registry`)
Base commit: `5797d52`
Accepted integrated tip: `943b4111d86396ff924cd295d995c3c1f2de7008`
Pull request: `https://github.com/claytonread/omnivia-core/pull/105`
Merge commit: `b030dfbd7b8eb45889190d8aeae62a18b9a7f27e`

Primary repository: `/Users/claytonread/Projects/omnivia-core`
Acceptance worktree: `/Users/claytonread/Projects/worktree-omnivia-core-organisational-model-v02`

## 1. Acceptance decision

The product owner approved a formal Phase 1 `GO`, together with architecture,
runtime/storage and security/privacy acceptance, for integrated tip `943b411`
on 2026-09-12. Pull request #105 passed the required hosted checks and merged to
`main` as `b030dfb`. Commit `dd72159` remains the independently identifiable
Phase 1 implementation commit within that accepted lineage.

## 2. Scope boundary

### 2.1 Implemented

- Immutable typed Semantic Model, version and element contracts.
- Concept, Property, Relationship, Constraint, Alias, Vocabulary Member and
  Action Type elements.
- The 18 normative typed change-operation kinds from specification section 11.
- Deterministic operation ordering and compatibility classification.
- Canonical UTF-8 JSON, NFC normalisation and stable SHA-256 content digests.
- UUIDv7 allocation through a standard-library-only public seam.
- Consumer, dependency, exact binding, review, decision, publication, activation
  and history records.
- SQLite migration `0037_semantic_registry.sql` with workspace isolation,
  append-only guards, immutable history, publication records, activation
  compare-and-swap, consumer bindings and an outbox.
- Fenced repository writes using the existing Core writer authority.
- Manual model creation, proposal, review, approval/rejection, preview,
  publication, activation, projection, export and digest-verification services.
- Approval invalidation, stale-base rejection, idempotent proposal/publication,
  consumer-impact blocking and rejected-proposal suppression.
- Backup/restore verification of version digests and historical consumer
  bindings.

### 2.2 Deliberately deferred

The following remain gated by the specification and are not part of this
acceptance:

- Phase 2 evidence, observation, candidate and governed-assertion storage.
- Phase 2 effective-valid-interval and temporal precision implementation.
- External ontology, extraction, validation or projection workers.
- LLM-assisted suggestion generation.
- SHACL/OWL validation workers and advanced reasoning.
- Migration planning and execution against consumer data.
- Automated mutation governance, precedent, budgets, receipts and fuse state.
- Automatic approval, publication or activation, which remain prohibited by the
  specification.

## 3. Delivered implementation

### 3.1 Public domain package

`src/omnivia_core/semantic_registry/` contains the standard-library-only public
domain layer:

| File | Responsibility |
|---|---|
| `models.py` | Immutable Semantic Model, version and element types |
| `operations.py` | Typed operation union and constructors for all 18 operations |
| `ordering.py` | Deterministic dependency-aware operation order |
| `canonical.py` | Canonical projection, serialisation and digest calculation |
| `diff.py` | Patch/minor/major compatibility impact classification |
| `consumers.py` | Consumer, dependency and exact-binding contracts |
| `records.py` | Review, publication, activation, history and finding records |
| `ids.py` | UUIDv7 allocation and injectable ID allocation protocol |
| `errors.py` | Stable semantic-registry error taxonomy |
| `__init__.py` | Explicit public API |

The public layer has no persistence, network, worker, model or third-party
dependency.

### 3.2 Authoritative persistence

`packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/migration_files/0037_semantic_registry.sql`
adds the authoritative SQLite schema. It includes:

- 18 semantic-registry tables;
- 54 append-only/fencing guards plus the controlled activation trigger;
- workspace-scoped primary and foreign-key relationships;
- immutable versions, changes, reviews, decisions, publications and activations;
- one activation-mediated path for moving the current-version pointer;
- change-set digest uniqueness within a workspace/model;
- historical exact-version consumer bindings; and
- immutable publication/outbox references.

`packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/semantic_registry.py`
provides neutral projection and fenced storage primitives for the schema. It does
not give workers or callers raw canonical-write authority.

### 3.3 Manual governance service

`packages/omnivia-core-runtime/src/omnivia_core_runtime/service/semantic_registry.py`
implements the Phase 1 authoring and publication lifecycle:

1. create a model and initial pointer;
2. propose or deduplicate a typed change set;
3. request review against the current digest;
4. approve or reject without rewriting history;
5. preview and validate the resulting immutable model;
6. classify consumer impact;
7. publish and activate under one fenced transaction with final compare-and-swap;
8. project, export and independently verify stored content.

Publication content digests exclude volatile database identities, timestamps,
labels and rationale. Operation rationale remains stored for audit but does not
alter semantic identity. Partial-operation `before` values are canonical
preconditions, including set-like identifier collections.

## 4. Phase 1 exit-criteria evidence

| Specification exit criterion | Evidence | Result |
|---|---|---|
| Publish an initial version and a second compatible version | End-to-end service tests publish `1.0.0`, then a compatible alias addition as `1.0.1` | PASS |
| Retain and query a rejected change | Rejection history and equivalent repeat suppression tests | PASS |
| Prove stale-base conflict and approval invalidation | Concurrent-generation, base-digest and post-edit approval tests | PASS |
| Restore canonical state from backup and rebuild no-op projections | Backup/restore digest, projection and binding verification tests | PASS |
| Verify workspace isolation and writer fencing | Migration trigger, repository transaction and stale-fence tests | PASS |

The demonstrable vertical slice also registers App and Workflow consumers,
publishes a compatible alias, blocks a breaking cardinality change, retains a
rejection, rejects stale publication, rebuilds a deterministic neutral JSON
projection and verifies restored bindings.

## 5. Verification evidence

All results below were obtained from the isolated worktree on Python 3.11.15.

| Gate | Result |
|---|---|
| Focused public/runtime semantic-registry suite | 185 passed |
| Runtime package suite | 5,185 passed, 5 skipped |
| Remaining repository suites | 15,657 passed, 27 skipped |
| Application-contract suite | 9,255 passed, 19 skipped |
| Canonical-migration and compatibility suites | 1,686 passed, 2 skipped |
| Benchmark suite | 30 passed |
| Strict mypy | 232 source files, no issues |
| Focused Ruff scope | Clean |
| Generated TypeScript contract compilation | Passed under strict TypeScript |
| Distribution builds and isolated installs | All five distributions passed |
| macOS status-menu companion | Build passed; 48 tests passed |
| Staged diff whitespace check | Clean |

Primary focused verification command:

```bash
.venv/bin/python -m pytest -q \
  packages/omnivia-core-runtime/tests/semantic_registry \
  tests/semantic_registry
```

Repository test partitions used after preflight reached the inherited baseline
failure:

```bash
.venv/bin/python -m pytest packages/omnivia-core-runtime/tests -q

.venv/bin/python -m pytest \
  services tests \
  packages/omnivia-core-cli/tests \
  packages/omnivia-core-client/tests \
  packages/omnivia-core-mcp/tests -q

.venv/bin/python -m pytest benchmarks/tests -q
```

The process-evidence tests require normal macOS process access because they call
`ps`. Running them in a restricted shell produces `Operation not permitted` and
is not valid acceptance evidence.

## 6. Review focus

The acceptance reviewer should concentrate on the following high-risk
properties:

1. **Canonical identity:** repeat serialisation produces the same digest across
   volatile IDs, timestamps, rationale and input ordering.
2. **Append-only authority:** no service or repository path updates published
   semantic content or historical governance records.
3. **Fencing:** every canonical write checks the current writer generation, and a
   stale writer cannot commit.
4. **Publication atomicity:** approval, base digest, compatibility and pointer
   generation are checked again inside the publication transaction.
5. **Activation authority:** the current pointer can move only through an
   activation record and compare-and-swap.
6. **Consumer safety:** incompatible changes cannot publish while an exact-bound
   affected consumer remains unresolved.
7. **Workspace isolation:** identifiers, deduplication and evidence returned to a
   caller cannot cross workspace boundaries.
8. **Failure atomicity:** rejected, stale or incompatible publication attempts
   leave no partial version, activation or outbox rows.

## 7. Resolved acceptance conditions

- The inherited Phase 0 export inventory and Ruff findings were corrected in
  the integrated lineage.
- The semantic migrations were reconciled with current `main` and allocated as
  the consecutive range `0037`–`0040`.
- `./scripts/preflight` passed at exact tip `943b411`, including 24,423 tests in
  the full repository suite, Ruff and strict mypy.
- Hosted `Core acceptance` run
  `https://github.com/claytonread/omnivia-core/actions/runs/34678950410`
  passed on the same exact tip.

## 8. Completed acceptance procedure

1. Phase 1 and the integrated Phase 2 lineage were reviewed against
   `SPEC-CORE-SEM-001` v0.2.
2. Inherited repository hygiene issues and migration allocation conflicts were
   resolved without rewriting the Phase 1 implementation commit.
3. Local preflight and all required hosted checks passed at `943b411`.
4. The accountable owner recorded all required approvals.
5. Pull request #105 merged to `main` as `b030dfb`.

## 9. Rollback and recovery

Before release or database migration, rollback is a normal revert of commit
`dd72159`.

After migration `0037` has been applied to a workspace, do not drop or rewrite
semantic-registry tables to roll back application behaviour. Published content
and governance history are append-only. Restore a prior active model through a
new authorised activation/publication decision, or ship a forward migration that
preserves existing rows and digests.

## 10. Acceptance record

| Field | Value |
|---|---|
| Decision | `GO` |
| Accepted commit/range | Integrated tip `943b4111d86396ff924cd295d995c3c1f2de7008`; Phase 1 commit `dd72159` |
| Hosted `Core acceptance` run | `34678950410` — PASS |
| Architecture reviewer | `claytonread`, approved 2026-09-12 |
| Runtime/storage reviewer | `claytonread`, approved 2026-09-12 |
| Security/privacy reviewer | `claytonread`, approved 2026-09-12 |
| Product owner | `claytonread`, approved 2026-09-12 |
| Accepted exceptions and expiry | None |
| Follow-up tasks | Phase 2 delivered in the same accepted PR; external optional workers remain separately gated |

## 11. Phase 2 gate outcome

The Phase 2 gate was satisfied in the same accepted lineage. WP-SEM-06 provides:

- the evidence and observation ledger;
- governed-assertion stated and evidence-attested temporal bounds;
- distinct stated, unknown and open end states;
- precision through seconds without guessed timezones;
- one versioned effective-valid-interval contract shared by storage, APIs,
  reasoning inputs and projections;
- permission-filtered evidence inspection;
- deterministic normalisation, deduplication, aggregation and suppression; and
- candidate-to-change-set conversion through the existing human governance path.

External extraction and suggestion workers remain separately gated removable
capabilities and receive no canonical database, approval, publication or
activation authority.
