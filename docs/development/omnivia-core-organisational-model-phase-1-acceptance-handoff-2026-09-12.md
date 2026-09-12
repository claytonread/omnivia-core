# OmniVia Core Organisational Model Phase 1 Acceptance Handoff

Date: 2026-09-12
Status: Conditional GO for review and merge acceptance
Specification authority: `SPEC-CORE-SEM-001`, version 0.2, dated 2026-09-11
Approved implementation scope: Phase 1 — Manual Semantic Registry
Implementation branch: `codex/core-organisational-model-v02`
Implementation commit: `dd72159` (`feat(core): implement phase 1 semantic registry`)
Base commit: `5797d52`

Primary repository: `/Users/claytonread/Projects/omnivia-core`
Acceptance worktree: `/Users/claytonread/Projects/worktree-omnivia-core-organisational-model-v02`

## 1. Acceptance decision requested

Review and accept commit `dd72159` as the implementation of the approved Phase 1
Manual Semantic Registry scope in `SPEC-CORE-SEM-001` v0.2.

The implementation is locally complete and its focused and repository-wide
executable test evidence is green. Merge acceptance is conditional on one of the
following outcomes for each inherited repository hygiene failure recorded in
section 7:

1. correct the stale Phase 0 public-export baseline and the unrelated Ruff import
   ordering finding in separately reviewed work; or
2. document an authorised, time-bounded acceptance exception.

A hosted `Core acceptance` run is still required before treating the change as
merged or released. This handoff does not authorise Phase 2 or any later phase.

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

## 7. Known acceptance blockers outside this change

### 7.1 Frozen Phase 0 export inventory drift

`./scripts/preflight` reaches the Phase 0 baseline check and reports that
`ControlPlaneRegistry.project_redacted_otel_observability` exists in the source
surface but not in `baseline/inventories/public-exports.json`.

This method is already present in base commit `5797d52`. Commit `dd72159` does not
modify either:

- `services/omnivia-memory/src/omnivia_memory/control_plane/registry.py`; or
- `baseline/inventories/public-exports.json`.

The owner of that control-plane change must either update the frozen inventory
with an accepted review note or revert the unaccepted public export. The semantic
registry change must not silently recapture that baseline.

### 7.2 Existing Ruff import-order finding

A repository-wide `ruff check` reports an import-order finding in:

`tests/canonical_migration/test_control_plane_barrel.py:17`

That file is unchanged by `dd72159`. Focused Ruff over every new semantic-registry
source and test file passes.

### 7.3 Hosted acceptance evidence

No branch was pushed and no hosted `Core acceptance` job was run as part of this
implementation task. Local verification is evidence for review, not a substitute
for the required protected-branch check.

## 8. Acceptance procedure

1. Review commit `dd72159` against `SPEC-CORE-SEM-001` v0.2 Phase 1 only.
2. Resolve or formally waive both inherited issues in section 7.
3. Rebase or merge the resulting baseline correction into the implementation
   branch without rewriting the semantic-registry commit.
4. Run `./scripts/preflight` from a clean Python 3.11 environment with `npm ci`
   completed.
5. Push the branch and require a green hosted `Core acceptance` check.
6. Record one of the decisions in section 10.
7. Merge only after the accepted commit range, migration ordering and hosted
   evidence are all identified in the review record.

## 9. Rollback and recovery

Before release or database migration, rollback is a normal revert of commit
`dd72159`.

After migration `0037` has been applied to a workspace, do not drop or rewrite
semantic-registry tables to roll back application behaviour. Published content
and governance history are append-only. Restore a prior active model through a
new authorised activation/publication decision, or ship a forward migration that
preserves existing rows and digests.

## 10. Acceptance record

Complete this section in the pull request or accepted review note.

| Field | Value |
|---|---|
| Decision | `GO`, `CONDITIONAL GO` or `NO-GO` |
| Accepted commit/range | |
| Hosted `Core acceptance` run | |
| Architecture reviewer | |
| Runtime/storage reviewer | |
| Security/privacy reviewer | |
| Product owner | |
| Accepted exceptions and expiry | |
| Follow-up tasks | |

## 11. Phase 2 gate

Phase 2 may begin only after this Phase 1 checkpoint is accepted and the later
phase is explicitly authorised. Before treating Phase 2 governed knowledge as
production-ready, WP-SEM-06 must provide:

- the evidence and observation ledger;
- governed-assertion stated and evidence-attested temporal bounds;
- distinct stated, unknown and open end states;
- precision through seconds without guessed timezones;
- one versioned effective-valid-interval contract shared by storage, APIs,
  reasoning inputs and projections;
- permission-filtered evidence inspection;
- deterministic normalisation, deduplication, aggregation and suppression; and
- candidate-to-change-set conversion through the existing human governance path.

External extraction and suggestion workers are not prerequisites for beginning
Phase 2. They remain removable capabilities and receive no canonical database,
approval, publication or activation authority.
