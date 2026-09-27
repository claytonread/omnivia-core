# Engineering memory: source coverage and qualified whole-file applicability

Date: 2026-09-27

Originated on `codex/engineering-applicability-evidence` and continued on
`codex/engineering-memory-completion`. This is a bounded
implementation candidate for review, not a release. It does not claim all
AC-001 through AC-064 scenarios as complete. Migrations 0050, 0051 and 0052,
the captured-source migration 0056 and the producer-queue migration 0058 are
pinned to reviewed content.

This slice delivers one bounded vertical:

1. a trusted source producer records immutable snapshots with
   `engineering.source.record`;
2. an engineering `memory.create` proposal names its whole-file dependencies
   against one recorded snapshot;
3. a `current_safe` read serves a record only when one evaluator proves it
   `matched` at an explicitly requested target that coverage has reached.

Only whole-file SHA-256 digests are compared. No symbol, span, config key or
rename is ever resolved.

## Captured working-tree source

`engineering.source.capture.commit` accepts only repository, stream, sequence,
predecessor and sealed snapshot identities, plus an optional expected rich-manifest
digest. The authenticated installation, checkout binding, file index, capture status,
counts and digests come from the sealed 0056 header. A request cannot carry a local
path, file list, manifest body, content, authority or audit identity.

The trusted `engineering.snapshot.capture` maintenance path freezes the working tree,
publishes its blobs and rich manifest evidence, records the snapshot, writes the
path-to-digest index, and inserts the capture header last. That header seals the index.
The accepted commit then binds one stream to the authenticated installation and exact
registered checkout, appends a `captured_v1` event, and advances the same contiguous
coverage barrier used by `engineering.source.record`. The captured event's inline `{}`
body is a representation sentinel; readers branch on `manifest_format` and never treat
it as evidence of an empty repository. `capture_status: incomplete` and the rich
manifest omissions remain authoritative, so incomplete baselines and targets evaluate
as `unknown`.

The installed local service runs a small bounded polling executor from its managed
service tick, independently of local-socket or HTTP requests. It uses only
installation-local registered checkout roots, renews the existing workspace lease
during Git, file-read and blob-publication loops, and holds the shared SQLite gate only
for short reads and fenced settlement. An eventless seal is reused without rereading
only when it initializes an absent stream or fills the exact named gap; otherwise the
producer fails closed and rereads/revalidates the checkout before appending a new
head. When coverage has a gap, the executor commits only the missing snapshot named by
the earliest successor's predecessor link; it does not append another head. Stable
derived stream, snapshot and idempotency identities make retries and lost replies
converge. Its pass result contains counts only and
application/capture results contain no local path, checkout hint, file list or raw
manifest.

Migration 0058 gives every capture an atomic, payload-free producer queue row keyed by
workspace, installation and snapshot. Live captures persist the exact source frontier
and predecessor observed before the filesystem effect. Recovery revalidates that intent
and refuses a stale head after a competing append. Pending/retry selection, checkout
rotation and lane priority use persisted per-installation keyset cursors, so fairness
survives restart. A finite, indexed legacy seeder processes at most 64 pre-0058 headers
per pass up to a frozen watermark; ordinary scheduling reads the next-eligible queue
index rather than scanning capture history. Retry timing is persisted and capped, and a
successful captured event settles its matching queue projection in the same transaction.

The trusted CLI route is `engineering capture`; the operation is deliberately omitted
from model-facing MCP with reason `mutation`. Platform filesystem notifications remain
external integration work, so Core polling is the recovery source of truth. Dev still
owns semantic parser/indexer and symbol/span adapters; this Core slice supplies only
captured whole-file coverage.

A proposal's sealed set is carried to the exact versions that the
claim-preserving `knowledge.propose` and `candidate.approve` mint (migration
0051), so the same vertical reaches accepted knowledge. No other transition
carries a set; see "Deferred and unsupported".

## `engineering.source.record`

| Posture | Value |
|---|---|
| Scope kind / side effect | workspace / `create` |
| Required scope | `engineering:source` |
| Required capability | `engineering.source` 1.0 |
| Purpose / role | `engineering_source` / `workspace_contributor` |
| Idempotency | key required; no mutation precondition |
| Allowed errors (`ENG_SOURCE_MUT`) | `CREATE_MUT` plus `conflict` and `size_limit_exceeded` |

### Who holds the grant

- **Who holds it:** the local-owner engineering family session. The local owner
  is its own source producer in Personal mode.
- **Contributors do not:** the memory family session that contributes
  observations lacks the operation, the scope and the capability.
- **MCP profiles do not:** neither `restricted` nor `authoring` holds any part of
  it, and the traceability ledger lists the operation as omitted (`mutation`).
- **The contributor role is not enough:** a session with that role but without
  the scope is refused `authorization_denied`, and one without the capability is
  refused `capability_not_granted`.
- **Surfaces:** the CLI exposes it as `engineering source --input-json …`.
  Clients use the generated `EngineeringSourceRecordInput` and
  `EngineeringSourceRecordResult` bindings (Python and TypeScript).

### Input

Unknown keys are refused at every level.

```json
{
  "repository_id": "erepo-app",
  "stream_id": "estream-main",
  "sequence": 3,
  "predecessor": {"sequence": 2, "snapshot_id": "esnap-a1"},
  "snapshot_id": "esnap-b",
  "snapshot_kind": "git_commit",
  "base_commit": "c0ffee01",
  "capture_status": "complete",
  "manifest": [{"path": "src/auth.py", "digest": "sha256:<64 lowercase hex>"}],
  "manifest_digest": "sha256:<optional; verified when present>"
}
```

- **Identifiers:** `repository_id`, `stream_id`, `snapshot_id` and
  `predecessor.snapshot_id` are contract Identifiers. The repository is
  registered on first use with its id as its label; no label is ever guessed.
- **Sequence and predecessor:** `sequence` is 1..2147483647. `predecessor` is
  present exactly when `sequence > 1`, and it names `sequence - 1` and a
  snapshot other than this event's own.
- **Snapshot kind:** `git_commit` must state `base_commit`. `working_tree` (a
  dirty tree) and `source_archive` must not. A base commit is provenance only
  and never makes two snapshots equivalent.
- **Capture:** `capture_status` is `complete` or `incomplete`. An incomplete
  manifest is recorded, but it can never qualify `matched`.
- **Paths:** repository-relative and `/`-separated, at most 512 code points,
  kept exactly as given; Unicode and case are never normalized. These are
  refused: an absolute path, a drive prefix such as `C:` or `c:x`, a backslash,
  an empty, `.` or `..` segment, a control character, a lone surrogate, and a
  repeated path.
- **Digests:** `sha256:` followed by 64 lowercase hex characters.
- **Caps:** at most 256 entries and 65536 canonical bytes, otherwise
  `size_limit_exceeded`.
- **Canonical manifest:** the JSON object `{path: digest}`, serialized as
  `json.dumps(sort_keys=True, separators=(",", ":"))` with ASCII escapes.
  `manifest_digest` is `sha256:` plus the SHA-256 of those UTF-8 bytes. This is
  the same canonicalization that the 0047 snapshot row's `manifest_digest` has
  always used. It is not RFC 8785.

### Result

```json
{
  "repository_id": "erepo-app", "stream_id": "estream-main", "sequence": 3,
  "snapshot_id": "esnap-b", "manifest_digest": "sha256:…", "capture_status": "complete",
  "disposition": "recorded",
  "coverage": {"state": "pending", "covered_sequence": 1, "announced_sequence": 3},
  "recorded_at": "2026-09-27T00:00:00.000000Z",
  "audit_reference": "aud-…"
}
```

`disposition` is `already_recorded` when an identical event was delivered
earlier under another idempotency key. No new source event, snapshot, head or
barrier is written in that case, and `recorded_at` is the original time. The
call is still a settled mutation: its audit event and the new key's idempotency
settlement are recorded, so `audit_reference` is the new call's.

### Streams, ordering and the coverage barrier

- **Ownership:** a stream is bound to the authenticated principal and the
  repository of its first event. Another principal is refused
  `authorization_denied`. The owner naming another repository is refused
  `conflict`. A stream is never replaced.
- **Commit together:** repository registration, the stream row, the announced
  head, the snapshot, the event and the coverage barrier commit in one fenced,
  audited mutation. Any failure rolls all of it back.
- **Order:** coverage follows the producer's sequence and predecessor chain,
  never capture time.
  - An event may arrive ahead of a gap. It is stored and `pending` while it lies
    within the 64-event pending window past the covered sequence; beyond that it
    is refused `size_limit_exceeded`.
  - When the gap fills, one bounded drain (at most 64 steps) advances coverage
    through every contiguous event whose predecessor link is valid.
  - The window guarantees that the drain always reaches the end of the chain.
    Nothing needs a later sweep, even after a restart.
- **Chain integrity:** a stored neighbour must agree with an event's link. A
  predecessor whose snapshot differs, or a successor that names a different
  predecessor, makes the event `conflict`. The stored chain is therefore always
  consistent.
- **Immutable identities:** an identical redelivery is `already_recorded`. A
  different event under a used (stream, sequence), or a used snapshot id (in any
  stream), is `conflict` whichever key it arrives under. The same key with a
  different request is `idempotency_conflict`.
- **Separate streams:** each worktree or checkout has its own stream, and
  streams never share coverage.
- **Retention:** all history is kept. Events, streams and dependency sets are
  append-only.

## `dependency_manifest` content profile (`memory.create`)

This applies to engineering observations: record types
`knowledge.{finding,risk,decision}` under domain `engineering.codebase`. The
profile is written as an optional `content.dependency_manifest`:

```json
{
  "repository_id": "erepo-app", "stream_id": "estream-main", "snapshot_id": "esnap-a",
  "producer": "omnivia-dev-indexer", "producer_version": "1.0.0",
  "coverage": "complete",
  "dependencies": [
    {"selector_type": "whole_file", "selector": "src/auth.py",
     "meaning": "must_match", "expected_digest": "sha256:…"},
    {"selector_type": "whole_file", "selector": "src/util.py",
     "meaning": "requires_revalidation_on_change", "expected_digest": "sha256:…"},
    {"selector_type": "whole_file", "selector": "README.md",
     "meaning": "context_only", "expected_digest": "sha256:…"}
  ]
}
```

- **Required keys:** all seven, and no others. `coverage` is `complete` or
  `partial`. There are at most 64 dependencies. A dependency carries
  `selector_type`, `selector` and `meaning`, plus `expected_digest`, which is
  required for `whole_file`.
- **Vocabularies:** `selector_type` and `meaning` use the 0049 vocabularies.
  `whole_file` selectors follow the manifest path rules. A repeated
  (`selector_type`, `selector`) pair, an unknown vocabulary value such as
  `rename`, or any malformed value is refused `invalid_request`. That includes a
  wrong-typed value (a list or object where a vocabulary string belongs), which
  is refused rather than failing internally, with no proposal or dependency row
  written. A dependency is never silently dropped.
- **Consistency:** if `content.applicability` states a `repository_id` or
  `snapshot_id`, it must agree with the profile, otherwise `invalid_request`.
- **Baseline:** the baseline must already be a recorded source event of the
  stated repository and stream, otherwise `dependency_unavailable`
  (`retryable_after_delay`) and nothing is written.
- **Persistence:** the dependency rows (0049 `omnivia_engineering_dependencies`,
  now with `expected_digest`) and one sealing `omnivia_engineering_dependency_sets`
  row are written in the same fenced mutation as the proposal.
- **Claims only:** the client's digests and coverage are stored as claims. They
  attest nothing until the evaluator checks them against the recorded baseline
  manifest.
- **Compatibility:** observations without the profile keep working and remain
  `unknown` under `current_safe`.

## The evaluator (`storage.engineering_source.evaluate_applicability`)

This is one deterministic, bounded function for an exact record version at one
covered target. It reads only and writes nothing.

| Condition | Result |
|---|---|
| No dependency set; or set's repository or stream differs from the target | `unknown` (nothing is inherited across streams or repositories) |
| Baseline not covered | `unknown` |
| Stored dependency rows differ from the set's sealed count | `unknown` (fails closed; at most count + 1 rows are read) |
| A required whole-file digest that the baseline attests is absent from a complete target | `invalid` |
| A required whole-file digest that the baseline attests changed at the target | `potentially_stale` |
| Unsupported selector type; required digest not attested by the baseline; incomplete baseline or target capture; `partial` coverage; no resolved evidence; no required dependency (empty or `context_only` only) | `unknown` |
| Otherwise (every required digest attested and equal) | `matched` |

- **Precedence:** `invalid`, then `potentially_stale`, then `unknown`, then
  `matched`. Adverse findings need only one attested dependency. `matched`
  needs every condition to hold.
- **Evidence:** it counts only when the record version was saved with
  `evidence_disposition: available` and resolved sources, through the existing
  evidence resolution and label grant.
- **What never counts:** reviews, review evidence ids, labels, base commits,
  branch names and recency.
- **Reverts:** a revert matches again because its digests are equal, and the
  intervening stale snapshot stays stale.

## `current_safe` reads

`applicability_mode` is a closed enum, `diagnostic` or `current_safe`, on
`EngineeringSearchInput` and `EngineeringContextBuildInput`. Other values are
`invalid_request`.

- **`diagnostic`:** the default. Applicability behaviour is unchanged; its
  frontier is now read under the same evidence-label authorization as
  `current_safe` (below). One visible consequence: the `candidates` view no
  longer lists proposal or candidate versions of a record that a governance
  transition has since moved on (for example, approved), matching the
  authorized reader's view policy that `current_safe` and memory reads already
  use.
- **Coverage first:** `current_safe` checks authoritative coverage before any
  frontier read or ranking. The target is refused when:
  - it was never recorded;
  - it lies beyond a gap;
  - it names another repository;
  - its manifest body no longer matches its digest.

  The refusal is `dependency_unavailable` with the fixed message
  `applicability_pending` and the frozen retry class `retryable_after_delay`.
  This is the compatibility-preserving refusal signal, not a newly ratified
  error code, and a read is never downgraded to `diagnostic`.
- **Authorization before evaluation:** in both modes, the frontier of pack
  build is read through the existing
  `storage.memory.read_authorized_memory_snapshot`, and the frontier of search
  (`accepted`, `candidates`, `history`) through
  `storage.engineering_preview.read_authorized_previews`, which reads the same
  frozen authorised frontier and then only the admitted versions' bounded
  preview rows, hydrating no body (see
  `engineering-search-preview-projection.md`). Both resolve identities and
  evidence-label grants first and read only admitted versions. The grant is
  computed with `local_owner_label_grant` for the
  effective authenticated caller (`context.principal`) and the server binding's
  granted workspace, never for the principal the handler was composed for,
  because a session dispatch runs this owner-composed handler as another
  principal. A denied version is never hydrated, evaluated, scored, counted
  toward the candidate cap or reported as an omission, and no preview, section,
  citation or text from it reaches the caller. Search page totals and the
  continuation's snapshot digest are computed from admitted versions only, so a
  label change between pages restarts the continuation as `invalid_request`.
- **Exact references:** `engineering.expand`, `context.priority.set` and
  `engineering.review.record` resolve their exact version under the same grant,
  across the `accepted`, `candidates` and `history` views. A hidden version is
  `not_found` with the same message as a nonexistent one. Expand emits only
  supersession edges whose both endpoints are visible, so hidden neighbours add
  no node, edge or cap usage. The priority and review writes recheck visibility
  inside the fenced mutation (review before its stated assessment version is
  compared), so a revocation after the preliminary check writes nothing and
  rolls back the audit.
- **Search:**
  - It requires `repository_target`, with view `accepted` or `candidates`.
  - Each candidate is evaluated directly before scoring, and only proven
    `matched` versions enter the frontier. Candidates keep their view
    partitions.
  - The coverage block reports `applicability: current`.
  - Unknown, stale and invalid records are omitted. The search result contract
    has no field to count them.
  - At most 1000 admitted candidates are checked, otherwise
    `size_limit_exceeded`.
- **Pack build:**
  - `targets` must be non-empty (otherwise `invalid_request`, never a silent
    `diagnostic` pack) and hold at most 16 entries (otherwise
    `size_limit_exceeded`). Both are checked in the handler before any coverage
    or frontier read; `diagnostic` builds are unchanged.
  - Every target must be covered.
  - A record enters only if it is `matched` at every target.
  - Omitted records produce an `applicability_unproven` omission and a
    mandatory uncertainty notice.
  - Per-target status is `matched` when records were included, otherwise
    `not_evaluated`.
  - `normalized_request.applicability_mode` and
    `reproducibility.source_coverage` pin the inputs.
  - `context_pack.build` v1 is unchanged.
- **Partition fix:** pack sections now keep each record's own partition. The
  builder previously reused the last frontier value's partition for every
  section. A version whose `governance_state` is `accepted` renders under
  `accepted_knowledge` (unless its `assertion_basis` is `hypothesis`); every
  other version, such as a `candidate`, renders under `candidate_findings`. The
  builder previously compared against `canonical`, which is an authority level,
  not a governance state, so accepted knowledge rendered as candidate findings.
  Accepted knowledge is never dropped to fit a budget: when it cannot fit, the
  build refuses with `token_limit_exceeded`.
- **No writes:** reads persist no pack, no assessment and no source history.

## Migration 0050 (`0050_engineering_source_coverage.sql`)

| Family | Shape |
|---|---|
| `omnivia_engineering_source_streams` | Stream binding (principal, repository), announced head, covered barrier. Identity is immutable and both sequences only advance. A new stream starts at barrier 0. The old barrier was validated when written and never decreases, so each advance validates only the newly covered range (OLD, NEW]: it must hold exactly NEW − OLD present events and span at most one 64-event pending window. That is one primary-key range scan, independent of the stream's lifetime history. Writers must be the owner's audited `engineering.source.record` or `engineering.source.capture.commit`. No DELETE. |
| `omnivia_engineering_source_events` | Immutable event: snapshot (FK to 0047), predecessor link and canonical manifest body. The digest must equal the snapshot row's, and the entry count must match the body. The event must lie within the announced head and agree with stored neighbours. Unique snapshot per workspace. Append-only. |
| `omnivia_engineering_dependency_sets` | One per exact record version: baseline (a recorded event of the stated stream and repository), producer and version, coverage. It seals exactly its dependency rows, and every whole-file row must carry a digest. It is written only by `memory.create`'s audited mutation, or carried by 0051 (below). Append-only. |
| `omnivia_engineering_dependencies` (0049) | `ADD COLUMN expected_digest` (nullable, `sha256:` format), an index on (workspace, record, version), and a 0050 insert guard: once a version's set row exists, no further dependency row is accepted for it, so a sealed set never changes. |

Every table has the standard fenced-writer guard trigger, and no existing
migration is rewritten.

### Migration pin

Allocation 50 is a candidate owned by Engineering Memory, with predecessor 49.
Its pinned content commit is `c3aca0a50fd0b5d675b4d5389a3ff99cfb765b7f` and its
normalized SHA-256 is
`5d01353103797cca5bc37cea77e7d9b52e65e9bf87c9bac1dccf953682f1ec41`.
The allocation guard and all 73 allocation tests pass. `accepted_commit` stays
null until the normal acceptance process records a landing.

## Dependency carry (migrations 0051 and 0052)

`knowledge.propose` and `candidate.approve` mint a new exact version whose
content, claim and evidence are byte copies of the engineering observation they
transition. Inside that same fenced, audited settlement the runtime
(`storage.engineering_source.carry_dependency_set`) copies the source version's
sealed set to the new version: same baseline (repository, stream, snapshot),
producer, producer version, coverage and dependency rows, under fresh row
identities and the transition's own audit, sealed at once.

- **Qualified sources only:** a source with no set, or whose stored rows
  disagree with its seal, its audit or its recorded baseline, carries nothing,
  and the new version stays `unknown`.
- **Attests nothing:** approval and review never qualify an observation. The
  evaluator still checks baseline and target coverage and manifests, evidence,
  stream and repository identity and every digest on each read, so a carried
  set is served `matched` only when the evaluator proves it, and revocation of
  the evidence grant still hides it.
- **Database guard:** 0051 replaces 0050's dependency-set INSERT guard under its
  own name. It keeps every 0050 check and admits exactly one more writer: a set
  carried by that transition's own unsettled settlement whose two versions agree
  on content, claim, evidence disposition and linked evidence, and which repeats
  the source's consistent sealed set. Every other operation, an insert outside
  the settlement, and a missing or inconsistent source set is refused. The 0050
  sealed, UPDATE and DELETE guards apply to a carried set unchanged.
- **Bounded lookup:** 0052 replaces the same guard again, differing from 0051
  only in naming 0050's version index for the two exact-version dependency
  reads, so each is bounded by the 64-dependency cap rather than the
  workspace's whole dependency table. The runtime's carry and evaluator reads
  name the same index. What the guard admits and refuses, and every refusal
  message, are unchanged.

No table, column or index is added, and no existing migration is rewritten.

### Migration pins

| Allocation | File | Predecessor | Introducing commit | Normalized SHA-256 |
|---|---|---|---|---|
| 51 | `0051_engineering_dependency_carry.sql` | 50 | `3fcc8d5a9c618b201c9223c19f4b381d555911d9` | `1c80cf1a3141f1aa8758af2f3acb55901017df8a124145a83545a6861d0ca508` |
| 52 | `0052_engineering_dependency_lookup.sql` | 51 | `4e4a6ed3ef4b1d8ce0145e8ffe52325fde0f80be` | `b4e11f6c8a84753f6b3914d76883e61d2ff487047767d5c656dfab279b4cc1f3` |

Both are candidates owned by Engineering Memory, with `accepted_commit` null
until the normal acceptance process records a landing.

## Source invalidation (migration 0059; spec §15.3; AC-057/AC-061)

The gap this slice closes: recording a new source event always advanced
coverage, but nothing durable ever re-assessed the dependents of a covered
change until a review or a fresh `memory.create` happened to touch them.
`current_safe` was never affected by this (it proves each version directly, on
every read, from the same evaluator described above), but `diagnostic` search
had no better answer than `not_evaluated` or a stale legacy row.

- **No new queue table.** `omnivia_engineering_source_streams` gains
  `processed_sequence` (the worker's watermark) and a nullable
  `pending_dependent_record_id` / `pending_dependent_version` pair: a durable
  *keyset* cursor into the *next* unprocessed event's scoped dependency sets,
  for an event whose fan-out does not fit one bounded step.
  The flat `record_source_event` and captured
  `record_captured_source_event` writes announce new work the instant
  `covered_sequence` advances past the watermark: that gap *is* the durable
  invalidation queue, announced and enqueued in the same atomic write as the
  covered source event.
- **One bounded step at a time, paged by a stable keyset.**
  (`storage.engineering_invalidation.advance_invalidation`): for a bounded
  `flat_v1` event, diff its inline manifest against its stored predecessor. For
  every event, page raw dependency sets through the new
  `(workspace_id, repository_id, stream_id, record_id, version)` scope index
  and inspect only each set's at-most-64 indexed whole-file selectors. Inline
  events compare those selectors with their bounded changed-path set. A
  `captured_v1` manifest can hold 10,000 paths, so captured events probe only
  those selectors in the two captured-file indexes; they never hydrate or scan
  the captured manifest in Python. Same-path history in another repository or
  stream is skipped by the scope-index prefix. A transition with an incomplete
  capture or a representation change
  rechecks every scoped dependency conservatively. The unmodified evaluator
  runs for each selected `(record, version)` against the event target, and
  every result is appended as a `basis: "deterministic"` row in the
  0049 assessment history (the value the schema reserved and left unused until
  now). A batch too large for one step persists the *last* scoped (record_id,
  version) it durably scanned, not a row count, and the next page resumes
  strictly past that key. The watermark itself holds as the
  durable, explicit statement that work remains -- never a silent skip. A row
  count was tried first and rejected: it counts positions in a query re-run
  fresh on every page, and a dependency set some other fenced write (an
  unrelated `memory.create`) seals between two pages shifts every later
  position by one, so the next page silently re-reads a row it already
  checked and, one page later, drops one it had not reached yet -- a
  duplicate that becomes a skip. A keyset cursor has no position to shift: a
  row at or behind it was already checked (and assessed when affected) or fell outside this
  event's cohort because it did not exist when this page ran, and
  current-state evaluation of that row is never this history's job --
  `current_safe` proves it directly, from the same evaluator, on every read --
  and the very next covered event whose diff touches one of its dependencies
  re-runs the lookup fresh and finds it. A row ahead of the cursor, by
  contrast, is checked by the very next page and assessed when affected, never
  skipped and never assessed twice for an event already finished with it.
- **The same evaluator, so the same rules.** A changed digest is
  `potentially_stale`, an absence under complete capture is `invalid`, and an
  incomplete capture or an unqualified set is `unknown` -- identically to a
  live `current_safe` check. There is still no rename field: a renamed file
  reads as an ordinary absence at its recorded dependency's old path, not as a
  specially reconciled move.
- **Generation-fenced like every other writer.** Each step runs inside
  `ownership.fencing.fenced_transaction` under the caller's own fencing
  generation, so a writer whose authority has been superseded is refused
  before anything commits, exactly as any other guarded write is. Duplicate or
  replayed source events touch neither the watermark nor the assessment
  history (an `already_recorded` disposition changes nothing for either to
  advance from). Recovery after a restart is simply calling the worker again:
  every fact it needs is a durable column on the guarded stream row, so
  nothing in memory is required to resume. A caller's clock reading behind
  the stream row's own last `updated_at_us` -- a real clock stepping
  backward, not only a hypothetical -- is clamped forward to that stored
  value (the same clamp `record_source_event` already applies to this
  column) rather than raised into the guard trigger that would otherwise
  reject the write and roll back the batch of assessments just computed;
  every assessment keeps the caller's own, unclamped reading, so nothing is
  backdated, and only this liveness bookkeeping column is held to its
  existing monotonic invariant.
- **Where it runs.** Both
  `service.handlers.engineering.engineering_source_record` and
  `engineering_source_capture_commit` best-effort drain the just-advanced
  stream after their own mutation commits;
  `service.runner.ServiceRunner._recover` best-effort drains, on startup, up to
  `TICK_STREAM_LIMIT` streams with outstanding work, each up to
  `DRAIN_STEP_LIMIT` bounded steps; and
  `service.runner.ServiceRunner.drain_pending_invalidation` takes one further
  bounded step for up to `TICK_STREAM_LIMIT` pending streams on every tick of
  the main serve loop's existing 250ms poll (`main._serve_until_stopped`) --
  the only scheduler seam this service has. Both bounds select streams through
  `storage.engineering_invalidation.select_pending_streams`, which reads a
  fixed `TICK_STREAM_LIMIT` *raw* stream rows in primary-key order per call
  rather than filtering for lagging ones in SQL: `pending_streams`'s own
  `processed_sequence < covered_sequence` filter is residual against the
  streams table's only applicable index, so using it to bound this selection
  would let a caught-up workspace's stream count, not `TICK_STREAM_LIMIT`,
  decide how many rows one call reads. `select_pending_streams` carries a
  fair keyset cursor forward tick to tick and wraps once it runs past every
  stream, so a persistently failing or merely low-sort stream is never
  starved of its own turn, and a workspace with more lagging streams than
  either bound leaves the rest to converge over later ticks rather than
  making one startup pass or one tick unbounded in stream count. The tick is
  what keeps a backlog beyond either the per-stream step bound or the
  per-pass stream-count bound converging in an otherwise idle service: neither
  the live trigger nor startup recovery runs again on its own, so without it a
  backlog past one drain budget, past `TICK_STREAM_LIMIT` streams, or past
  however many events land while the service is down, would sit forever. All
  three are deliberately not a readiness precondition: `current_safe` does not
  depend on this history, so falling behind costs staleness of the
  `diagnostic`-mode assessment history, never correctness, and a per-tick or
  startup failure is written to the service's own diagnostic output -- a
  fixed, bounded code, never a source identifier or raw exception text --
  rather than silently absorbed or folded into any readiness signal.

### Migration pin

Allocation 59 is a candidate owned by Engineering Memory, with predecessor 58.
Its allocation record is added only after the migration exists at its immutable
introducing commit, following the repository's two-step migration-pin workflow.
`accepted_commit` stays null until the normal acceptance process records a
landing.

## Producer → consumer map

| Producer | Writes | Consumers |
|---|---|---|
| `engineering.source.record` and `engineering.source.capture.commit` (trusted source, `engineering:source`) | repository/stream bindings, flat or captured snapshot event, head and barrier | `covered_snapshot` (targets and baselines), the evaluator, `current_safe` search and build, the dependency-set trigger, the invalidation worker |
| `memory.create` with `dependency_manifest` (contributor) | governed proposal, 0049 dependency rows, dependency set | the evaluator, the invalidation worker's scoped lookup |
| `knowledge.propose`, `candidate.approve` (claim-preserving governance) | the new exact version's carried dependency rows and set, when the source's set is consistent | the evaluator |
| Evaluator (read-only) | nothing | `current_safe` search (frontier admission, preview `matched`) and `current_safe` pack build (sections, per-target status, omissions); also called by the invalidation worker |
| `engineering.review.record` (unchanged) | attestation plus conservative assessment | `diagnostic` search only; never the evaluator |
| Invalidation worker (migration 0059, service-owned, generation-fenced) | `deterministic` assessments; the stream's `processed_sequence` and its keyset cursor (`pending_dependent_record_id`/`pending_dependent_version`) | `diagnostic` search's re-assessment of stored rows; never `current_safe`, which still proves every version directly |

## Deferred and unsupported

None of these is ever served as `matched`. Those that bear on applicability
evaluate `unknown`; the rest are limitations that this slice leaves as they were.

- **Transitions that do not carry:** only the claim-preserving
  `knowledge.propose` and `candidate.approve` carry a set. `record.supersede`,
  `candidate.reject` and any transition that changes content, claim or evidence
  carry nothing, so the version they mint has no set and stays `unknown` until
  it has its own qualified set; no path records one for it. An accepted version
  whose source had no consistent set is likewise `unknown`.
- **Other selector types:** `symbol`, `config_key`, `source_span`,
  `schema_contract` and `external_evidence` are recorded but not evaluated.
- **Renames:** there is no rename field. A renamed required file reads as absent
  at the target, which is `invalid` under complete capture.
- **No serving projection beyond the assessment history itself:** migration
  0059 adds the background invalidation worker (see above), but there is
  still no separate cache or projection it serves reads from -- it only
  appends to the same 0049 assessment history `diagnostic` search already
  reads through `assess_against_registered_head`. A stored `invalid` or
  `potentially_stale` -- `deterministic` or `review` alike -- is surfaced
  there unchanged; a stored `matched` is not, and is re-derived the same
  conservative way it always was. `diagnostic` search still never shows
  `matched` for anything, by that same existing rule.
- **No lineage reasoning:** there is no cross-stream equivalence, ancestry or
  merge-base reasoning. Equal digests in another stream prove nothing here.
- **Continuity access:** `working_context` (search view and the `resume` pack
  section) reads the continuity checkpoint index, which carries no evidence
  labels. Sessions, checkpoints and that index are read only for the effective
  principal's own sessions. Another principal's session is indistinguishable
  from a missing one. There is no sharing grant, so continuity is
  same-principal only.
- **Known conflicts:** context packs do not yet produce known-conflict warnings.
- **Search omissions:** `current_safe` search omissions are not counted in the
  result, because the contract has no field for them.
- **Production wiring fix:** `EngineeringHandlers` is now composed with its
  family session, binding and clock. The `context.priority.set` and
  `engineering.review.record` mutations previously lacked them in the
  production surface.

## Verification

The complete local preflight passed on 2026-09-27 with this worktree's virtual
environment first on `PATH`:

```sh
PATH="$PWD/.venv/bin:$PATH" ./scripts/preflight
```

- Full repository suite: 27,281 passed, 53 skipped; seven dependency and TLS
  deprecation warnings.
- Application contracts: 10,186 passed, 19 skipped.
- Canonical migration and compatibility: 1,686 passed, two skipped.
- Phase 0 baseline: 749 passed; benchmarks: 23 passed.
- Package builds, installed-root resolver, migration allocations, generated
  contracts, TypeScript, Ruff and strict mypy (347 files) passed.
- macOS companion build and all 59 Swift tests passed.

The virtual environment must supply both Python packages and console scripts.
A globally installed `omnivia-core-service` on `PATH` can serve a different
schema and correctly fail the readiness fingerprint check. The complete pass
above used the service executable from this checkout.

The full run also exposed an existing POSIX blob-publication replacement race.
Its separate repair retries only detected replacement, up to four attempts,
while retaining file identity, link-count and exact-byte validation. Five
regressions cover the race and unsafe replacement refusal; the original
concurrency test passed 1,000 repeated runs after the repair.
