# OmniVia Core Organisational Model Phase 2 Acceptance Handoff

Date: 2026-09-12
Specification authority: `SPEC-CORE-SEM-001`, version 0.2, dated 2026-09-11
Implementation plan: `omnivia-core-organisational-model-phase-1-closeout-and-phase-2-implementation-plan-2026-09-12.md`
Branch: `codex/core-organisational-model-v02`
Local implementation status: complete and preflight-clean
Acceptance status: **REVIEW REQUIRED — not a release or merge GO**

## 1. Decision requested

Review the Phase 2 implementation range and decide whether it is acceptable for
hosted `Core acceptance` and formal architecture, runtime/storage,
security/privacy and product-owner review.

This handoff does not claim those external decisions, does not open or merge a
pull request, and does not authorise optional workers or automatic publication.

## 2. Exact review range

Phase 2 implementation commits:

```text
3c7024b..5a7833a
```

Phase 2 base:

```text
fda9bcf  fix(core): close phase 1 acceptance hygiene
```

Reviewed implementation tip:

```text
5a7833ac3849700988bfa3a116e4c45890142e97
```

Commit sequence:

```text
3c7024b test(core): add phase 2 evidence and temporal fixtures
f863410 docs(core): freeze phase 2 semantic policy
63b5e53 feat(core): add phase 2 semantic errors
969c06e feat(core): add effective valid interval contract
1257f8e feat(core): add evidence observation and candidate contracts
c1dd3fc feat(runtime): add phase 2 semantic storage migration
16f5b9b feat(runtime): add fenced semantic evidence repositories
cce3d3d feat(core): add deterministic aggregation and suppression
931308e feat(runtime): add phase 2 semantic commands and queries
5a7833a feat(runtime): add phase 2 retention events and recovery
```

The Phase 1 implementation remains independently identifiable at `dd72159`.

## 3. Delivered scope

### 3.1 Fixtures and frozen decisions

- Added the public Phase 2 acceptance corpus covering workspace-scoped evidence,
  contradictory observations, suppression/reconsideration, permission filtering,
  temporal precision, timezone handling, historical backfill and correction by
  successor.
- Added the Phase 2 decision record covering evidence authority, classification,
  retention, capabilities, temporal semantics, candidate lifecycle,
  deduplication, suppression and the Phase 2 threat model.

### 3.2 Public standard-library contracts

- Added evidence source, item, span, extraction and evidence-link records.
- Added observations, deterministic features and evidence bundles.
- Added governed assertions, evidence, supersession and retraction records.
- Added semantic candidates, contribution records, suppression and
  reconsideration receipts.
- Added stable structured semantic errors and public exports.
- Added canonical projections and digests with deterministic ordering.

### 3.3 Shared temporal semantics

- Added canonical UTC parsing and truncation for year through second precision.
- Added explicit temporal provenance and stated, unknown and open end states.
- Added the versioned effective-valid-interval calculation with half-open
  intervals and fail-closed indeterminate bounds.
- Added timezone-less sub-day handling that reduces or rejects rather than
  guessing UTC.
- Added historical source-time and explicit ingestion-fallback behavior.

### 3.4 Canonical SQLite and fenced repositories

- Added workspace-scoped, append-only evidence, observation, assertion,
  candidate, suppression and reconsideration relations.
- Added workspace-scoped foreign keys, hash/temporal checks, deduplication and
  query indexes.
- Applied the existing writer lease and fencing guard to every new canonical
  mutation.
- Added typed repositories for evidence/observation and assertion/candidate
  history, including digest verification after restore.
- Failed multi-row writes roll back without partial canonical rows or outbox
  events.

### 3.5 Permission-checked application boundary

- Added independently enforced metadata and protected-content reads.
- Added separate manual and deterministic-rule observation capabilities.
- Added candidate aggregation, inspection, rejection, suppression,
  reconsideration and conversion commands.
- Candidate conversion creates an ordinary unapproved Phase 1 change set; it
  cannot approve, publish or activate it.
- Authority is rechecked inside fenced writes, including inside the Phase 1
  change-set transaction used by candidate conversion.
- Cross-workspace and actor mismatches return stable non-disclosing errors.
- Assertion history and temporal queries return typed records with both resolved
  axes echoed.

### 3.6 Aggregation, events, retention and recovery

- Added pure versioned normalization, aggregation, contribution scoring,
  evidence snapshot and candidate-band rules.
- Preserved contradictory observations as distinct queryable contributions.
- Added deterministic rejection signatures, suppression termination rules and
  reconsideration receipts.
- Added atomic versioned outbox events containing only workspace, fence and
  immutable record identifiers; protected content and spans are excluded.
- Added a fail-closed Phase 2 event reader that validates event version, payload
  digest, workspace, aggregate identity and fencing generation.
- Added content-free retention policies, legal holds/releases, deletion plans,
  targets and receipts.
- Deletion planning covers canonical metadata, protected content, source spans,
  raw completions, worker scratch, search/graph/vector projections, caches, logs
  and backups.
- A retention override may only shorten the workspace default; legal hold wins
  over due date; only ready plans can be receipted.
- Backup/restore tests recompute evidence and retention digests from restored
  canonical rows.

## 4. Migration range

```text
0026_semantic_registry.sql                 Phase 1 semantic registry
0027_semantic_evidence_observations.sql    Phase 2 evidence and governance
0028_semantic_retention_recovery.sql       Phase 2 retention and recovery
```

`0027` and `0028` are the Phase 2 additions. Migration order and checksums are
owned by the existing migration ledger.

## 5. Local acceptance evidence

Final command:

```text
./scripts/preflight
```

Final result at implementation tip `5a7833a`: **PASS**.

Key evidence from the final run:

```text
Package-boundary tests:                  51 passed
Application-contract tests:          9,255 passed, 19 skipped
Canonical migration/compatibility:   1,686 passed, 2 skipped
Phase 0 baseline:                       749 passed
Full repository suite:               21,209 passed, 32 skipped
Benchmark suite:                         30 passed
Ruff:                                  passed
Strict mypy:                           273 source files passed
Distribution build/install checks:       5 distributions passed
macOS status-menu companion:             48 tests passed
```

Focused Phase 2 storage/service suite:

```text
.venv/bin/python -m pytest packages/omnivia-core-runtime/tests/semantic_registry -q
171 passed
```

The final preflight emitted seven dependency/platform deprecation warnings only;
it emitted no test, build, lint or type-check failure.

## 6. Acceptance criteria trace

| Criterion | Evidence |
|---|---|
| Authorised evidence and spans | Evidence-link contracts, FK guards, permission service and evidence repository tests |
| Workspace-only exact deduplication | Evidence digest uniqueness and cross-workspace migration/repository fixtures |
| Contradictions remain visible | Aggregation and acceptance-corpus contradiction tests |
| Rejected equivalents stay suppressed | Suppression activity, expiry, new-evidence and rule-version tests |
| Temporal bound states remain distinct | Temporal corpus, assertion repository and service temporal-query tests |
| Historical backfill uses source time | Source-time/fallback temporal fixtures |
| Canonical writes remain fenced | Migration guard, stale-generation and transaction rollback tests |
| Sensitive content is filtered | Independent metadata/content capability and content-free event/retention tests |
| Conversion stays human governed | Candidate-to-unapproved-change-set and in-transaction revocation tests |
| Restore retains canonical evidence | Backup/restore row comparison and digest verification tests |

## 7. Security and privacy posture

- Evidence bytes remain outside canonical semantic metadata behind
  `content_ref` and a separately authorised resolver.
- Evidence is treated as untrusted data and gains no command or tool authority.
- General events, errors, candidate views and deletion receipts do not embed
  protected bytes or sensitive source spans.
- Workspace and actor claims are compared to server-established authority before
  access or mutation.
- Optional extractors, reasoners and projection workers receive no canonical
  SQLite writer API or database credential from this implementation.

## 8. Deliberate boundaries and review notes

- Physical blob/cache/projection/log/backup deletion is an executor concern
  outside Core. Core now owns the authoritative, fenced plan and content-free
  receipt contract; an external executor must resolve targets under its own
  capability and report completion through that contract.
- Protected-content storage and backup are provided by the injected content
  owner. Core verifies canonical `content_ref` and digest metadata; this change
  does not introduce a blob store.
- The in-process Phase 2 service is the permission-checked application seam. No
  new HTTP, MCP or CLI exposure is granted by this range.
- The persisted outbox uses the existing lowercase dotted event-kind convention
  (for example `semantic.candidate.created.v1`), while retaining the specification's
  past-tense/versioned semantics. Reviewers should confirm this established wire
  convention is the accepted representation of the specification's display names.
- Canonical temporal persistence retains UTC value, precision and provenance.
  The public temporal parser also preserves original source text and trusted
  source timezone in `TemporalInstant`; reviewers should decide whether those
  auxiliary parser fields require dedicated SQLite projection beyond the
  evidence/observation source text already retained.
- The repository-wide mutation/idempotency seam remains the transport owner of
  caller-scoped idempotency. The Phase 2 in-process service uses immutable record
  IDs, content digests and Phase 1 change-set deduplication; transport adapters
  must not bypass the existing mutation seam when these commands are exposed.

The final three notes require explicit reviewer disposition before an
unconditional specification-conformance `GO` is recorded.

## 9. Rollback procedure

1. Stop the canonical writer and acquire the installation storage lock.
2. Preserve the failed/current database and external evidence store for forensic
   review; do not edit append-only rows in place.
3. Restore the verified pre-Phase-2 backup through the existing atomic restore
   path.
4. Confirm SQLite integrity and foreign-key checks.
5. Confirm the restored migration ledger/fingerprint and Phase 1 model/version
   digests.
6. Restore or reconcile protected blobs according to the content owner's backup
   policy and the canonical evidence references in the selected backup.
7. Rebuild optional projections from the restored canonical state and resume
   outbox delivery idempotently.
8. Reacquire the writer lease with a new fencing generation before resuming
   mutation.

There is intentionally no destructive down-migration that rewrites Phase 2
history into Phase 1 tables.

## 10. Outstanding external gates

- [ ] Architecture sign-off on the frozen Phase 2 decisions and review notes.
- [ ] Runtime/storage sign-off on migrations `0027` and `0028`, fencing and
      rollback.
- [ ] Security/privacy sign-off on content separation, permission checks,
      events and retention.
- [ ] Product-owner approval of the bounded Phase 2 behavior.
- [ ] Green hosted `Core acceptance` for the exact reviewed range.
- [ ] Pull-request and merge approval.

Until those boxes are completed, the correct decision is **local implementation
complete; formal acceptance pending**.

## 11. Reviewer record

| Review | Decision | Reviewer | Date | Notes |
|---|---|---|---|---|
| Architecture | Pending |  |  |  |
| Runtime/storage | Pending |  |  |  |
| Security/privacy | Pending |  |  |  |
| Product owner | Pending |  |  |  |
| Hosted Core acceptance | Pending |  |  |  |
| Merge | Pending |  |  |  |
