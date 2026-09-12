# OmniVia Core Organisational Model Phase 1 Closeout and Phase 2 Implementation Plan

Date: 2026-09-12
Status: Proposed execution plan
Specification authority: `SPEC-CORE-SEM-001`, version 0.2, dated 2026-09-11
Phase 1 implementation commit: `dd72159`
Phase 1 acceptance handoff commit: `99472c1`

## 1. Objective

Close the conditional Phase 1 acceptance cleanly, obtain explicit Phase 2
authorisation, and then deliver the Phase 2 evidence, observation, candidate and
temporal-governance capabilities without introducing external-worker or automated
publication authority.

The plan has two independently gated programmes:

1. **Programme A — Phase 1 acceptance closeout:** resolve inherited repository
   blockers, obtain a green hosted acceptance run and record formal acceptance.
2. **Programme B — Phase 2 implementation:** implement WP-SEM-06 only after the
   Phase 1 checkpoint is accepted and Phase 2 is explicitly authorised.

Programme B must start from the accepted integration branch, not from an
unaccepted Phase 1 worktree.

## 2. Non-negotiable boundaries

- SQLite remains the canonical authority.
- Only the fenced Core semantic writer may mutate canonical semantic tables.
- Evidence content is untrusted data and never carries instructions or authority.
- All identifiers, uniqueness rules, reads, writes and deduplication are workspace
  scoped.
- Temporal storage must distinguish stated, evidence-attested, unknown and open
  boundaries.
- Missing temporal values must not be interpreted as infinity.
- One versioned Core contract must calculate effective valid intervals.
- Candidate conversion must enter the existing change-set review path.
- Approval, publication and activation remain human governed.
- External ontology, extraction, validation, projection and LLM workers are not
  part of the Phase 2 critical path.
- No optional worker receives canonical database credentials or direct mutation
  methods.

## 3. Programme A — Phase 1 acceptance closeout

### A1. Resolve ownership of inherited failures

Actions:

1. Assign the Phase 0 baseline drift to the owner of
   `ControlPlaneRegistry.project_redacted_otel_observability`.
2. Decide whether that public method is accepted or accidental.
3. If accepted, regenerate only the affected public-export inventory and add the
   required review rationale. If accidental, revert the public export.
4. Correct the pre-existing import order in
   `tests/canonical_migration/test_control_plane_barrel.py`.
5. Review and commit these corrections separately from the semantic-registry
   implementation.

Exit criteria:

- The accepted Phase 0 inventory and source surface agree.
- Repository-wide Ruff is clean.
- Neither correction changes semantic-registry behaviour.

### A2. Integrate and rerun local acceptance

Actions:

1. Rebase or merge the reviewed baseline correction into
   `codex/core-organisational-model-v02` without rewriting commit `dd72159`.
2. Provision the documented Python 3.11 environment and run `npm ci`.
3. Run `./scripts/preflight` without filters or skipped steps.
4. Preserve the complete command output as acceptance evidence.
5. Verify the commit range with `git diff --check`.

Exit criteria:

- Complete local preflight passes.
- The worktree is clean.
- The exact reviewed commit range is recorded.

### A3. Hosted acceptance and sign-off

Actions:

1. Push the reviewed branch.
2. Open a pull request identifying specification version 0.2 and the accepted
   Phase 1 scope.
3. Require a green hosted `Core acceptance` check.
4. Complete the acceptance record in the Phase 1 handoff.
5. Obtain architecture, runtime/storage, security/privacy and product-owner
   decisions.
6. Merge only the accepted commit range.

Exit criteria:

- Hosted `Core acceptance` passes.
- Acceptance decision is `GO` with no expired exception.
- Phase 1 is merged and its migration order is preserved.

## 4. Phase 2 authorisation gate

Before implementation begins, record an explicit Phase 2 authorisation covering
WP-SEM-06 and the following decisions.

| Decision | Required resolution |
|---|---|
| Evidence authority | Allowed source types, locator schemes and integrity requirements |
| Evidence storage | Canonical metadata versus protected content/blob ownership |
| Classification | Initial classification vocabulary and inheritance rules |
| Retention | Workspace default, allowed overrides, legal hold and deletion receipts |
| Permissions | Capabilities for metadata, sensitive spans, observation and candidate access |
| Temporal precision | Canonical UTC encoding and truncation for year through second |
| Source time | Trusted timezone contracts and the recorded-time fallback policy |
| Candidate lifecycle | Draft, active, proposed, rejected, suppressed and reconsidered transitions |
| Deduplication | Exact evidence and semantic-observation signatures and versioning |
| Suppression | Rejection equivalence signature, expiry and evidence-change conditions |
| Contradictions | Representation and query behaviour without destructive consolidation |
| Rule generation | Approved deterministic rules and rule-version ownership |

Required artefacts:

- Phase 2 decision record or ADR addendum.
- Initial evidence-retention and classification policy.
- Capability matrix for evidence and candidate commands/queries.
- Temporal conformance corpus with expected results.
- Phase 2 threat model covering evidence disclosure, prompt injection, cross-
  workspace leakage and unauthorised canonical writes.

Exit criteria:

- Architecture, security/privacy and product owners approve the bounded Phase 2
  scope.
- No unresolved decision changes storage identity, temporal meaning, retention or
  access-control semantics.

## 5. Programme B — Phase 2 implementation

### B1. Freeze acceptance fixtures before schema work

Implement OmniVia-owned fixtures for:

- exact duplicate evidence within one workspace;
- identical evidence in two workspaces;
- multiple observations supported by one evidence item;
- contradictory observations that must remain visible;
- rejected equivalent candidates and each permitted reconsideration trigger;
- permission-filtered evidence metadata and sensitive content;
- stated, absent, attested, unknown and open temporal boundaries;
- year, month, day, hour, minute and second precision;
- explicit timezone, trusted source timezone and timezone-less source text;
- historical backfill using source time instead of ingestion time; and
- correction/supersession without rewriting recorded history.

Deliverables:

- Public canonical fixture documents.
- Expected effective-valid-interval results.
- Negative fixtures for invalid temporal and permission combinations.

Exit criteria:

- Fixtures are independently reviewed before implementation makes them pass.
- Every Phase 2 exit criterion maps to at least one executable fixture.

### B2. Implement the public Phase 2 domain contract

Add standard-library-only public modules for:

- `EvidenceItem` and evidence source/span/integrity metadata;
- `EvidenceExtraction` metadata without worker authority;
- `SemanticObservation` and observation-to-evidence support;
- deterministic `ObservationFeature` values;
- governed knowledge assertions, supersession and retraction records;
- `SemanticCandidate`, candidate contributions and suppression records;
- temporal boundary state, precision and provenance types;
- candidate lifecycle and reconsideration reasons; and
- stable structured errors for temporal, evidence, permission and candidate
  conflicts.

Implement versioned canonical projections and digests for each immutable record.
Sensitive content must not appear in general error messages, event payloads or
unfiltered projections.

Suggested package boundary:

```text
src/omnivia_core/semantic_registry/
  evidence.py
  observations.py
  assertions.py
  candidates.py
  temporal.py
```

Exit criteria:

- Public contracts remain standard-library only.
- Canonical ordering and digest fixtures pass across input-order variations.
- Invalid state combinations fail at construction.
- Public exports and package-boundary tests pass.

### B3. Implement the shared temporal contract

Create one versioned function or service that computes:

```text
effective_from = valid_from when stated, otherwise attested_from

effective_to = valid_to when end state is stated
             = attested_to when end state is unknown
             = positive infinity when end state is open
```

Required behaviour:

- half-open intervals `[effective_from, effective_to)`;
- no negative-infinity interpretation for a missing stated start;
- no open-ended interpretation for an unknown end;
- canonical truncation to declared precision;
- no guessed timezone for timezone-less clock text;
- preserved original source text when structured precision is reduced;
- an explicit, queryable ingestion-time fallback when authorised source time is
  unavailable; and
- the same results for domain code, SQL projections, APIs and export fixtures.

Exit criteria:

- The full temporal conformance corpus passes.
- SQL and Python results are identical for every fixture.
- Invalid or indeterminate boundaries fail closed.

### B4. Add the Phase 2 SQLite migration

Create the next ordered migration after semantic registry migration `0037` with workspace-scoped tables for:

- evidence items and protected content references;
- evidence extractions;
- semantic observations;
- observation-to-evidence links;
- deterministic observation features;
- governed assertions, assertion history, supersessions and retractions;
- semantic candidates;
- candidate-to-observation contributions; and
- candidate suppression/reconsideration state.

Required database controls:

- append-only history and correction-by-successor;
- writer-fence checks on every canonical mutation;
- workspace-scoped foreign keys and uniqueness;
- content/integrity hash constraints;
- exact-duplicate uniqueness inside, but not across, workspaces;
- valid temporal state/field combinations;
- precision vocabulary and canonical timestamp constraints;
- immutable aggregation, normalisation and policy-version references;
- indexes for source, candidate, temporal and evidence-history queries; and
- outbox references that contain IDs rather than sensitive evidence spans.

Exit criteria:

- Migration succeeds from every supported pre-Phase-2 schema state.
- Direct unfenced, cross-workspace and historical-update attempts fail.
- Backup/restore retains all digests, temporal anchors and suppression state.

### B5. Implement the fenced repository

Add storage operations that:

- ingest or deduplicate evidence atomically;
- append observations and their evidence links;
- calculate and persist versioned deterministic features;
- append or supersede governed assertions;
- append candidates and contribution records;
- record rejection suppression and reconsideration;
- query evidence and assertion history at exact recorded and valid times; and
- verify canonical digests after restore.

Every write must accept the existing writer lease/fencing context. Repository
methods must expose typed neutral records, not raw database rows or worker-facing
SQL access.

Exit criteria:

- Stale writers and stale candidate bases cannot commit.
- Failed multi-record writes leave no partial rows or outbox events.
- Concurrent duplicate ingestion resolves deterministically.

### B6. Implement permission-checked commands and queries

Add service operations for:

- evidence registration and metadata inspection;
- separately authorised sensitive-content/span retrieval;
- manual observation creation;
- deterministic rule-generated observation creation;
- candidate aggregation and inspection;
- candidate rejection, suppression and reconsideration;
- candidate-to-change-set conversion;
- governed assertion history; and
- temporal queries with resolved axes echoed in the response.

Transport-supplied workspace and actor context must be verified against command
claims. Permission checks must occur before retrieving sensitive content and be
rechecked inside canonical write transactions where authority could change.

Exit criteria:

- Metadata and sensitive-content capabilities are independently enforced.
- Cross-workspace IDs do not disclose existence through results or errors.
- Candidate conversion produces an ordinary Phase 1 change set requiring normal
  human review and publication.

### B7. Implement deterministic aggregation and suppression

Implement versioned, explainable rules for:

- text and identifier normalisation;
- exact evidence deduplication;
- observation equivalence;
- candidate support, novelty and risk bands;
- contradictory evidence retention;
- rejected-candidate equivalence signatures;
- suppression expiry and evidence-change conditions; and
- reconsideration receipts.

These rules must be pure and replayable. A different rule or policy version must
not silently reinterpret a historical candidate.

Exit criteria:

- Identical inputs and versions produce identical candidates and digests.
- Contradictory evidence remains linked and queryable.
- Suppression ends only when its recorded condition is satisfied.
- Machine-generated material cannot approve or publish its resulting change set.

### B8. Wire events, retention and recovery

Actions:

- Emit versioned outbox events containing immutable record references.
- Apply classification inheritance to projections and exports.
- Implement retention/deletion planning across canonical metadata, protected
  content, caches, projections, logs and backups.
- Receipt deletion work without retaining deleted sensitive content in audit
  messages.
- Extend backup/restore verification for Phase 2 tables and content references.

Exit criteria:

- Event consumers can deduplicate by event ID and validate workspace/generation.
- Unsupported event versions fail closed.
- Retention and legal-hold fixtures pass.
- Restore produces the same canonical and temporal query results.

### B9. Phase 2 acceptance and handoff

Run:

- focused domain, migration, repository and service suites;
- temporal conformance across Python and SQL;
- concurrency, fencing and failure-atomicity tests;
- permission and cross-workspace adversarial tests;
- backup/restore and projection replay;
- package builds and isolated installs;
- complete repository preflight; and
- hosted `Core acceptance`.

Phase 2 exit criteria:

1. Every proposal traces to authorised evidence sources and spans.
2. Exact duplicate evidence is suppressed only within its workspace.
3. Contradictory evidence remains visible.
4. Rejected equivalent candidates remain suppressed until a recorded condition
   changes.
5. Unstated, unknown, open and stated temporal bounds remain distinct across
   reads and derivations.
6. Historical backfill uses authorised evidence time when it exists.

Produce a Phase 2 acceptance handoff with the exact commit range, migrations,
test evidence, known limitations, rollback procedure and reviewer sign-off.

## 6. Recommended commit sequence

Keep changes reviewable through the following checkpoints:

1. `test(core): add phase 2 evidence and temporal fixtures`
2. `feat(core): add evidence observation and candidate contracts`
3. `feat(core): add effective valid interval contract`
4. `feat(runtime): add phase 2 semantic storage migration`
5. `feat(runtime): add fenced evidence and candidate repository`
6. `feat(runtime): add phase 2 semantic commands and queries`
7. `feat(core): add deterministic aggregation and suppression`
8. `feat(runtime): add phase 2 retention events and recovery`
9. `docs(core): add phase 2 acceptance handoff`

Each checkpoint must pass its focused tests, Ruff, strict mypy and
`git diff --check` before the next begins.

## 7. Dependency and parallel-work plan

```text
Programme A: Phase 1 closeout
  A1 inherited corrections -> A2 local preflight -> A3 hosted acceptance
                                                       |
                                                       v
Phase 2 authorisation and decision freeze -> B1 fixtures
                                              |
                      +-----------------------+-----------------------+
                      v                       v                       v
                B2 contracts            B3 temporal             threat model
                      |                       |
                      +-----------+-----------+
                                  v
                             B4 migration
                                  |
                                  v
                             B5 repository
                                  |
                        +---------+---------+
                        v                   v
                  B6 API/permissions   B7 aggregation
                        +---------+---------+
                                  v
                         B8 events/recovery
                                  |
                                  v
                         B9 acceptance handoff
```

B2 and B3 may proceed in parallel after fixture review. Security/privacy review
may refine the threat model in parallel but must finish before B4 schema
acceptance. B5 depends on B4. B6 and B7 depend on B5 and may proceed in parallel.

## 8. Definition of done

The work is complete when:

- Phase 1 has a recorded `GO` and a green hosted acceptance run;
- Phase 2 has explicit approval and an accepted decision record;
- all Phase 2 deliverables and exit criteria in section 5 are demonstrated;
- every canonical mutation remains fenced and workspace scoped;
- temporal results are identical across storage, APIs and projections;
- sensitive evidence is permission filtered and absent from general events and
  errors;
- candidate conversion uses the existing human-governed publication path;
- full local and hosted acceptance passes; and
- the final handoff identifies the exact accepted commit and migration range.
