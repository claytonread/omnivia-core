# OmniVia Core V06-8: Connector SDK and incremental synchronisation foundation

- **Date:** 2026-08-14
- **Slice:** V06-8, architecture specification section 8
- **Status:** Foundation implemented; the V06-8 A6 lane is **not** complete.
  What is built is the contract, the durable state and the run coordinator, and
  this document records that, who owns it, and what was deliberately left out.
  Accepted A6 scope that is still absent is listed under
  [Remaining A6 scope](#remaining-a6-scope) — that section, not this one, is the
  answer to "is the lane done".

## Why this document exists

The connector work spans two distributions and reuses three pieces of durable
infrastructure that already existed. Without a record, the next reader has four
plausible and wrong conclusions available:

- that `omnivia_core.connector` is a connector *implementation* rather than a
  contract, and that Core therefore ships integrations;
- that the connector cursor is missing durable state, because migration 0017 has
  no checkpoint table;
- that `system.connector_sync` is an application operation, because it appears
  in the audit trail beside operations that are;
- that bidirectional synchronisation is unfinished rather than out of scope.

## Ownership

| Concern | Owner | Location |
| --- | --- | --- |
| Connector protocol and wire/state values | `omnivia-core` | `src/omnivia_core/connector/` |
| Durable connector state | `omnivia-core-runtime` | `storage/migration_files/0017_connector_sync_state.sql`, `storage/connectors.py` |
| Run coordination | `omnivia-core-runtime` | `service/ingestion_coordinator.py` |
| Connector implementations | **Nobody, here** | out of repository |

`omnivia_core.connector` is standard library plus this distribution's own
contract package, and nothing else. It has no storage, no scheduling, no
transport and no credential handling. The runtime depends on it; it depends on
no runtime. That direction is checked structurally by
`scripts/check-package-boundaries.py` and behaviourally by
`tests/contracts/test_connector_contract.py`, which measures the import in a
fresh interpreter.

## What was reused rather than rebuilt

Three existing authorities do the heavy lifting, and adding a second one for any
of them would have meant two answers to a question that must have exactly one.

**Runs and attempts are durable jobs.** A synchronisation run is an
`ingestion.import` row in `omnivia_durable_jobs` with the full V06-1 M4
application-metadata, attempt and event history. That job kind is not incidental:
it is the only one `omnivia_evidence_artifacts.import_run_id` accepts, so the
evidence a run captures names the run that captured it.

**The cursor is a job checkpoint.** It is an `omnivia_job_checkpoints` row of
kind `connector.cursor`, under the run's own attempt, subject to the contiguity
and time-ordering guards migration 0010 already enforces. Migration 0017 adds the
one thing that was missing — the connector-to-run edge — which makes those
per-job checkpoints readable as one connector's continuous history:

```sql
ORDER BY sync_runs.sync_sequence DESC, job_checkpoints.checkpoint_sequence DESC
```

is a total order over every cursor a connector has ever committed, so the resume
point after any crash is a single deterministic row.

**Content, checksums and ACLs are evidence.** Bytes go through the same
content-addressed blob store and `omnivia_staged_sources` verification as local
source capture; the artifact keeps the source-native id, the provider-neutral
checksum and the locator; permission labels land in
`omnivia_evidence_permission_labels` as policy *input*, exactly as V06-3 defined
them. No new evidence shape was introduced.

Migration 0017 therefore adds exactly three tables, all append-only:
`omnivia_connector_sync_runs`, `omnivia_connector_dead_letters` and
`omnivia_connector_health_events`.


## The four ordering guarantees

Each is a consequence of an ordering choice, not of a check that could be
forgotten. All four have adversarial coverage in
`packages/omnivia-core-runtime/tests/phase3/runtime/test_v06_8_ingestion_coordinator.py`.

1. **The cursor never runs ahead of the work.** One batch is one fenced
   transaction and the checkpoint is its last statement, so a crash leaves the
   batch applied *and* the cursor advanced, or neither.
2. **Replay is a no-op.** Every effect the coordinator has is decided by
   comparing the reported change against durable *current state* — does an
   artifact for these exact bytes exist, is the item present or tombstoned,
   where is it now, which labels does it carry — rather than by appending
   whatever arrived. Re-processing a batch finds its own work already done and
   writes nothing.
3. **Bytes exist before the database names them.** Content is fetched, verified
   against the declared checksum *and* the declared length, and published to the
   blob store before any transaction opens.
4. **Cancellation lands on a boundary.** It is observed between batches, between
   items, and once more after the last item's bytes are in hand and before the
   batch transaction opens. That third observation is not redundant: a batch's
   final `content()` call has no item after it, so a run that looked only
   between items would fetch the bytes, see nothing, and commit a batch the
   caller had already cancelled. A cancelled run abandons the batch it was
   preparing entirely rather than committing part of it.

Guarantee 2 is stated in terms of current state rather than of derived
identifiers on purpose, and the difference is not cosmetic. An identifier
derived only from the *facts of one event* makes a legitimate recurrence — an
item deleted, restored and deleted again; an item moved back to a locator it
once had — collide with its own history and be silently dropped as a duplicate.
Identifiers here therefore include the event's position in its own stream, and
idempotency comes from the current-state comparison that decides whether to
append at all.

## What ordering is, and is not, guaranteed

An opaque cursor invites a guarantee it cannot support, so the boundary is worth
stating exactly.

**Not guaranteed:** that out-of-order delivery is detected. A cursor token is
opaque and carries no order; `source_version` is a source-native token and is
likewise not comparable. Nothing here can tell that a source handed back a page
from before the last one. Any design that claimed otherwise would be pretending
opaque tokens are lexically ordered.

**Guaranteed, and tested:**

- **The durable resume point never regresses.** It is the single row the total
  order `(sync_sequence, checkpoint_sequence)` selects over every cursor the
  connector has ever committed, and that order only grows. "Latest" is a
  property of the read, never of the token's value.
- **The same batch applied twice changes nothing the second time**, for every
  change kind, because each decision is against current state.
- **An inapplicable move is a no-op.** A rename whose `previous_locator` is not
  the item's current locator has nothing to apply and is ignored; otherwise it
  is applied. That is the whole rule, and it is about applicability rather than
  age. It is *not* stale-change detection: an A→B replayed after the item
  legitimately cycled B→A is indistinguishable from a fresh A→B recurrence, and
  both are applied. Outside that one narrow case, the rule is simply **last
  delivered writer wins**.
- **Nothing is lost either way.** Evidence, provenance and permission labels are
  append-only, so every observation the source ever made is still readable even
  when only the latest one is current.

## Rename, deletion, reappearance and ACL change

None of these is a mutation, because the evidence tables have none to offer.
Each is an append whose *decision* is read off current state.

A **rename** appends a `source.renamed` provenance event carrying an
`omnivia_evidence_event_references` row with the new locator. The artifact keeps
the locator it was captured at, which stays true; the current locator is the
latest rename event's reference, falling back to the artifact when there has
been no move. The move is recorded when the item is currently at
`previous_locator` — or when the capture that just ran created the artifact,
which is the case where the item was born at its destination and the move would
otherwise be unrecoverable. A rename is captured *and then* recorded as a move,
in that order and unconditionally, because an item that moved and changed
content in the same pass has two facts and every rule for choosing between them
loses one.

A **deletion** appends a `source.deleted` provenance event with
`tombstoned_observation = 1`. The artifact and its bytes stay exactly as
captured. Whether the observation is needed is read from the *head* of the
presence stream — the most recent event that said anything about presence at all
— not from whether a tombstone has ever been written. Repeating a deletion is
recognised and does nothing.

A **reappearance** is the same rule read the other way. An item whose bytes are
already held but whose stream head says tombstoned is observed present again by
appending a `source.ingested` event with `tombstoned_observation = 0`. Delete,
reappear and delete again is therefore four observations on one artifact rather
than one tombstone the item can never escape, and the artifact is never
duplicated.

An **ACL change** on an item whose bytes are unchanged is a real durable change,
so it is not "unchanged". The permission-label stream stays the only authority:
the current label set is folded out of it in `label_sequence` order, compared
with what the source now reports, and the difference is appended as `withdrawn`
then `attached`, each in ascending label order. Identical input appends nothing,
which is what makes an ACL replay idempotent without a second record of
"current". Labels attach to an evidence identity, so a content change produces a
new artifact carrying the labels reported with it.

`SyncOutcome` has one counter per verdict, and they are disjoint: `ingested`,
`unchanged`, `relabelled`, `restored`, `renamed`, `deleted`, `dead_lettered`.
`unchanged` means nothing durable was written, which is why the two verdicts
that do write — a relabel and a restoration — have their own terms rather than
hiding inside it. The outcome validates its nested values as models and not
merely as present or absent: a `cursor` that is not a `ConnectorCursor`, or a
`failure` that is not a `ConnectorFailure`, is refused where the outcome is
built rather than surviving to fail at `to_wire`, where no caller is left to fix
it.

## The connector is untrusted input

A connector is code this repository did not write, running in this process,
handing values to columns with domains. Three things follow, and all three are
checks rather than orderings, because nothing about the shape of the work could
have made them free.

**Boundary values are validated before any durable write.** `connector_id`,
`source_kind` and `state_version` are checked against the same domains the
schema enforces, and a refusal names the field without quoting the value — a
hostile identity copied into an exception is exactly how it reaches a log.
Return types are checked too: `fetch` must return a `SourceBatch`, `content`
must return bytes, `health` must return a `SourceHealth`. A `health` that does
not is an `unavailable` observation; the other two are classified failures.

**Two batch-level rules are enforced that a connector could otherwise break
durably.** A batch whose cursor carries a state version that is not the
connector's is refused, because committing it would poison the connector rather
than merely fail a run: that cursor is read back by every later run and refused
on resume forever. And a batch with `has_more` set whose cursor this run has
already been handed is refused, because it describes a loop and the loop would
be inside `synchronise`.

**Connector-authored text is redacted before it becomes durable.** Failure
messages and health detail pass through a filter that strips control characters,
bounds length, and removes URL credentials, keyed secret values
(`password=`, `api_key:`, `Authorization:` and their relatives, quoted or not),
`Bearer`/`Basic` credentials and any token-shaped run of 32 characters or more.
An exception quoting the request that failed is the ordinary way a connector
fails, not an exotic one, and these columns are append-only: a credential that
lands in one is there for the life of the workspace.

## Limits

A run is bounded in three dimensions — batches, changes and content bytes —
each with a per-run ceiling on `IngestionCoordinator`. All four ceilings
(`max_item_attempts` too) are validated at construction: a positive, non-bool
`int` or `IngestionRefused`. A zero or negative ceiling admits nothing and would
truncate or fail every run forever, and `True` is an `int` that reads as a flag
and quietly means one. Refusing at construction is necessarily before any
durable write.

`max_item_attempts` is bounded above as well, at `MAX_DEAD_LETTER_ATTEMPTS`
(256), and the bound is not the coordinator's to choose: it is the range the
durable `attempts` column and the `DeadLetter` contract accept. Configured
higher, the first item to exhaust its retries would build a dead letter the
contract refuses — mid-run, after bytes had already moved, as an exception
rather than a run outcome. The three per-run ceilings have no such upper bound,
because nothing durable records them.

A batch is the indivisible unit — applied whole in one fenced transaction or not
at all — so each ceiling is tested **at the batch boundary, against what the
fetched batch declares it will cost, before any `content()` call**:

- **It fits the remaining budget** → the batch is materialised and committed.
- **It does not fit what is left of this run** → the run stops successfully with
  `SyncOutcome.truncated`. No bytes are fetched, no rows are written, and the
  cursor still names the last batch that did fit, so the next run meets the same
  batch with a whole budget. Nothing is dropped and nothing needs reconciling.
- **It could not fit the whole-run ceiling even at zero bytes spent** → the run
  **fails** with a `connector_contract_violation`. Truncating here would defer
  the batch to a run with more budget, and there is no such run: the source would
  be parked behind it permanently while every run reported `succeeded`. It is the
  connector's ceiling to respect, so it is the connector's violation, and it is
  deterministic — the same batch fails the same way every time.

`max_batches_per_run` needs none of this: one batch is one unit, so counting them
before the fetch is already exact.

What the run counts against the byte ceiling is **declared** cost, not fetched
cost. An item that dead-letters or is retried therefore spends exactly what the
decision that admitted its batch reserved for it, so the running total cannot
drift away from the preflight. Counting fetched bytes instead would let a batch
of items that all fail be admitted against its declaration and charged nothing,
which is a source of unlimited admissible batches.

A batch is bounded independently of all this: `MAX_BATCH_CHANGES` changes at the
contract boundary, and `MAX_SOURCE_BYTES` per item, reusing the capture limit
local source capture already enforces rather than inventing a second one. The
per-item limit still applies inside an admitted batch: an item declaring more
than `MAX_SOURCE_BYTES` is dead-lettered without being fetched.

**Not guaranteed:** that a run never transfers more bytes than
`max_content_bytes_per_run`. The ceiling bounds *declared* content admitted per
run; an item retried after a failed fetch re-reads bytes that were already
charged once, and a source whose bytes do not match its declaration has already
sent them by the time the mismatch is found. The bound this ceiling does give is
the one that matters for durable state: a run cannot commit more declared
content than its ceiling, and it cannot exceed it by an unbounded whole batch.

## Retry classification and dead letters

Connectors classify their own failures in the frozen vocabulary already used by
the application contract (`non_retryable`, `retryable`, `retryable_after_delay`,
`retryable_after_precondition_refresh`). An exception that is *not* a
`ConnectorError` is treated as non-retryable: nobody has said it is safe to
repeat, and guessing that it is turns one fault into a loop.

A retryable item failure is retried in-run up to `max_item_attempts`; a
non-retryable one is dead-lettered immediately. A dead letter is evidence of a
decision already taken, not a queue — nothing re-reads it to try again. A run
with dead letters still commits its cursor, so one poisoned item cannot stall
the source behind it.

**One row per run and item, however many times the run sees it fail.** A single
batch may not report an item twice, but a *run* legitimately can: a paginated
listing that shifts under the reader hands the same native id back on a later
page. If the item fails there too, that is a repeated observation of a decision
already recorded, not a second decision, so `record_dead_letter` writes nothing
and returns `None`. The schema states the same invariant and keeps stating it —
it is preserved here rather than relaxed there, because relaxing it would let a
run's dead-letter history count one item more than once. `SyncOutcome`'s
`dead_lettered` is therefore the number of durable rows the run created, not the
number of failures it saw, and the later batch commits its cursor like any
other.

The four ways a source can contradict itself — an item over the capture size
limit, bytes that are not the declared length, bytes that are not the declared
checksum, and a `content()` that did not return bytes at all — are all
non-retryable, for one reason: repeating the fetch cannot resolve a
disagreement the source is having with itself. Only the source changing its mind
can, and that arrives as a later change.

## Connector state versioning

A connector declares a `state_version` and every run records it. The rules are
fail-closed in every direction:

- a cursor from an **older** version is discarded and the next run starts from
  the beginning, because its token meant something the current version no longer
  promises;
- a cursor from a **newer** version, or a connector whose declared version is
  behind its own durable history, raises `IngestionRefused` — a downgrade would
  read a newer token under older rules, which is the silent corruption a version
  exists to prevent. The schema enforces the same rule independently;
- a *batch* whose cursor is not at the connector's own version is refused before
  it is committed, which is the case the first two rules cannot help with,
  because by the time such a cursor is durable the damage is permanent.

## Remaining A6 scope

These are accepted V06-8 A6 contract obligations that this foundation does
**not** discharge. They are listed as outstanding work, not as non-goals — the
section below is for the things that are deliberately never coming.

- **The four-operation connector SPI.** The accepted contract is
  `describe` / `migrate_cursor` / `probe` / `poll`, with a `PollContext` carrying
  what a poll is entitled to know. What ships here is the three-method
  `SourceConnector` (`health`, `fetch`, `content`) the coordinator needs; the
  other operations and the context type do not exist yet.
- **Cursor witness and full parent lineage validation.** The cursor is opaque and
  versioned, and the durable resume point is a single deterministic row, but
  nothing validates a cursor *witness* or checks a full parent lineage, and the
  migration obligations that lineage validation implies are not written.
- **A deterministic fake connector.** The connector fakes in this slice live
  inside the test modules that use them. No shared, deterministic fake ships as
  part of the contract for other suites — or other repositories — to build on.
- **The corpus-driven conformance kit.** The accepted 65-case conformance corpus
  and the kit that runs a candidate connector against it are absent. Coverage
  today is the acceptance suites listed above, which test *this* coordinator
  rather than an arbitrary connector's conformance.
- **The packaging assertion.** Nothing asserts that the connector SDK is
  packaged and consumable as the accepted artefact; the package boundary is
  checked, its distribution shape is not.

## Non-goals, stated so they are not read as gaps

- **Bidirectional synchronisation is out of scope by decision.** Nothing writes
  to a source, and no part of this slice is a partial step toward it.
- **No connector implementation ships here.** Not a filesystem one, not a cloud
  one. The `SourceConnector` protocol is structural: an implementation inherits
  nothing and registers nowhere.
- **No new application operation.** The frozen 20-operation catalogue is
  unchanged. `system.connector_sync` is a durable audit and metadata label for a
  service-initiated maintenance run — the same family as the existing
  `system.recovery` — and is not dispatchable over any transport.
- **No scheduler.** Something has to decide when to call
  `IngestionCoordinator.synchronise`. That decision is not made here.
- **No credential store.** A connector arrives already able to reach its source.

## Residuals

- **Redaction is defence in depth, not a guarantee.** It is a pattern filter over
  text a connector chose to write. It catches URL credentials, keyed secrets,
  auth schemes and long token-shaped runs; it will not catch a short secret
  introduced by a phrase no rule anticipates. A connector must still not put
  credentials in messages. What the filter does guarantee is that the failure
  path most likely to leak one — an exception quoting a request — no longer does.
- **One attempt per run.** A failed run is resumed by the *next* run, which
  starts from the last durable checkpoint, so continuity is carried by the cursor
  rather than by multi-attempt retries within one job. The job family's
  multi-attempt machinery is available and unused; the metadata reserves
  `max_attempts = 8`. A crashed run also leaves its job `claimed` and its attempt
  `running` until the existing stranded-job recovery reaches it.
- **A rename always fetches content.** Because capture runs first
  unconditionally, a pure move re-reads bytes it already holds. The publish is a
  no-op (the blob is content-addressed) but the connector's `content()` call is
  real. A connector that can serve unchanged bytes cheaply pays little; one that
  cannot may want this revisited.
- **A checksum or length mismatch is dead-lettered but not staged.** The
  staged-source relation has a `digest_mismatch` outcome that would record the
  disagreement durably. Nothing reads it today, so the dead letter carries the
  fact instead.
- **Health is recorded, never acted on.** An `unavailable` source fails the run;
  `degraded` is recorded and the run proceeds. No backoff policy reads the
  stream.
- **A truncated run is a flag, not a signal.** `SyncOutcome.truncated` says work
  remains, but nothing schedules the follow-up run, because nothing schedules any
  run.
- **A batch no run can afford stalls that connector until something changes.**
  Failing it is the honest outcome — see Limits — but the failure repeats every
  run until the connector pages more finely or the ceiling is raised. Nothing
  raises it automatically, and nothing alerts on the repetition.
