# Engineering memory: search previews without body hydration (AC-033)

Date: 2026-09-27

Implemented on `codex/engineering-memory-context-budget`. This is a bounded
implementation candidate for review, not a release. It does not claim all
AC-001 through AC-064 scenarios as complete, and it addresses AC-033 for the
governed observation views only (see "Still hydrates a body, or reads more").

Before this slice `engineering.search` hydrated every authorised version's
content, claim lineage, provenance and transition rows, then ranked over the
canonical JSON of the whole content, filtered on it, paged it and cut the
preview from the body it had just read. AC-033 requires no body read at all, so
each engineering version's bounded preview is now stored beside it and a search
reads only that.

| Spec fact (acceptance data) | How it holds |
|---|---|
| A preview is at most 480 code points | the projection's `preview` is cut by code point and CHECKed `<= 480`; the title is `<= 200` |
| A preview is at most 2 KiB UTF-8 | CHECKed `length(CAST(preview AS BLOB)) <= 2048` (480 code points are at most 1920 bytes, so the byte bound is never the binding one) |
| The preview list is bounded | the contract rejects a `limit` above 100; the service also caps its effective limit at `SEARCH_MAX_LIMIT` (100), and a page is cut by bytes |
| The response is at most 64 KiB | a page that would pass `RESPONSE_MAX_BYTES` (65 536 bytes of canonical JSON) ends early and continues; 2 KiB is held back for the page token, coverage block and response envelope, so the whole frame stays inside the cap |
| No full body is hydrated, then truncated | no statement on the governed search path names `content_json`, `claim_json` or `rationale_json`, and no hydration function is reached |
| Full hydration belongs to exact reads and expansion | `engineering.expand`, exact references and the pack builder still hydrate through `read_authorized_memory_snapshot` |

## What `engineering.search` reads now

For the `accepted`, `candidates` and `history` views, in this order:

1. **Request and coverage.** The payload decodes through the contract decoder.
   In `current_safe` mode the target's authoritative coverage is checked first,
   before any frontier read, exactly as before.
2. **One resolution instant**, taken once, or the one a continuation carries.
3. **The authorised frontier**, from `storage.memory.read_authorized_memory_frontier`:
   the sealed versions of the view's domain (`engineering.codebase`) and their
   supersession and transition endpoints, the evidence links of every version
   and of its whole transition chain, and the evidence-label fold under the
   effective caller's grant. Identities, currentness facts, links, labels and
   stored digests only. No preview and no body column is read, and a denied
   version leaves the set here.
4. **The projection rows of the admitted assemblies**, and only those, in the
   same read snapshot. A version with no row is `projection_unavailable`; one
   whose rows are of another projection version, or were derived from another
   content digest, is `stale_projection`. Both are retryable, name no version,
   and fall back to nothing.
5. **Filters on metadata:** the `assertion_basis` hypothesis exclusion under
   `accepted` (§8.2), the `repository_target` repository, and in `current_safe`
   the shared evaluator on each admitted version's identity and evidence facts,
   under the same 1000-candidate cap as before.
6. **Ranking**, by `storage.engineering_preview.rank_previews`, a pure function
   of the candidates it is handed: occurrences of the normalised query, then
   `recorded_at_us` descending, then record id, then version. That is
   `governed_order_key`'s rule, pinned by a test against it. Principal
   preferences then reorder the ranked set as before.
7. **The snapshot digest** a continuation is bound to: the ranked versions, each
   by record id, version and stored content digest, under the projection
   version. It names the content without reading it.
8. **The page:** the slice, rendered from the projection rows, with each
   version's stored assessment re-assessed conservatively as before, cut at the
   byte cap, and a continuation for what remains.

`working_context` keeps its existing matching and visibility rules; the shared
response-byte cap also applies to its pages.

## The projection (migration 0053)

`omnivia_engineering_preview_projection` holds one row per
`(workspace, assembly, projection_version)`: the title, the preview text, its
truncation flag, the observation kind, assertion basis, topic key, repository
and snapshot the version names, and the `content_digest` of the assembly it was
derived from. Every field is CHECK-bounded, and the table is append-only under
the standard three guards. It has a foreign key to its assembly.

`omnivia_engineering_preview_source` defines preview derivation. It reads
`content_json` for version writes, maintenance rebuilds, the INSERT guard, and
migration backfill. Search never reads the view.

### Rules: bounds, not repairs

| Field | Rule |
|---|---|
| `title` | the `title` string if non-empty, else the record id; cut at 200 code points |
| `preview` | the first non-empty string of `summary`, `what`, `learned`, cut at 480 code points; with none, the title |
| `truncated` | the title or the chosen text was cut |
| `observation_kind`, `assertion_basis`, `topic_key` | the string at `kind`, `assertion_basis`, `topic_ref.proposed_key` if it is 1..64, 1..32 and 1..256 code points |
| `repository_id`, `snapshot_id` | the string at `applicability.repository_id` and `.snapshot_id` if it is a contract Identifier of at most 128 characters |

A field outside its profile is left out (NULL, or the fallback above), never cut
into a different value, and a text containing NUL is treated as absent. Content
that is not a valid JSON object has no row, so a search that admits it refuses
with `projection_unavailable` instead of guessing. Observations are validated on
save, so ordinary content loses nothing. The rules are the previous
`_observation_preview` rules for valid observations; only out-of-profile
metadata, which the previous code returned unbounded and outside its contract,
is now omitted.

### Who writes it

`memory.create` and each governance transition that copies content into a new
exact version (`knowledge.propose`, `candidate.approve`, `candidate.reject`,
`record.supersede`) project the assembly they insert, in the same settlement,
through `engineering_preview.record_preview`. That is the pattern the dependency
set already uses for the same two writers. A writer that cannot project its
assembly fails the settlement instead of leaving a version no search can serve.

The INSERT guard admits a row only when it is the derivation of its own assembly
under the current projection version: it re-derives the row from
`omnivia_engineering_preview_source` and refuses any difference, so no writer
can leave a row that describes other text, other content or other rules.

### Backfill, rebuild and restore

- **Upgrade:** 0053 backfills a row for every engineering-domain assembly the
  workspace already holds, before the table's guards exist (the 0015 precedent), so an upgraded
  workspace reaches the rows a fresh one writes. A test compares them.
- **Restore:** a backup is the whole database, so it carries the table. A backup
  taken before 0053 migrates forward and is backfilled the same way.
- **Rebuild:** `engineering_preview.rebuild_missing_previews` inserts the
  current-version row of every assembly that lacks one, on the fenced writer
  connection. Nothing calls it on a read path or at start; it is the maintenance
  entry for a lost row.
- **New projection version:** a change to what a preview is ships as a migration
  that adds rows of the next version beside these and advances
  `PROJECTION_VERSION`. A workspace between the two is `stale_projection`, never
  served from the old rules.

The projection is not registered in the 0011 projection ledger (watermark and
staged activation). It is written in the same settlement as the version it
describes, so it has no lag to track. It would need registering only if it ever
became asynchronously built.

## Search semantics: preserved and changed

Preserved: the operation's input, result shape and error set; the
`accepted`/`candidates`/`history` views and the effective caller's
evidence-label grant (a denied version is never named in a projection read,
scored, counted or digested); caller-principal isolation of `working_context`
and preferences; the resolution instant a continuation carries; the
hypothesis exclusion; `current_safe`'s coverage-first refusal, candidate cap and
fail-closed evaluator; the preview fields and their optional members; the
ranking rule.

Deliberately changed, because AC-033 forbids the alternative:

- **The match surface is the bounded preview.** A query is counted in the
  normalised title, preview, observation kind and topic key, not in the
  canonical JSON of the whole content (which also matched key names and JSON
  punctuation). A term that occurs only beyond a preview is not matched. Finding
  it is an exact read, which hydrates a body; full-body lexical recall without
  hydration needs the qualified, permission-partitioned scorer of AC-035.
- **The continuation's snapshot digest** is built from the ranked versions'
  stored content digests instead of their full record wires. It binds the same
  facts, and is invalidated by the same events: a change to the admitted set, to
  the order (preferences), or to the query, view, limit or projection version.
  A later write is outside the pinned resolution instant, as before.
- **A projection refusal** (`projection_unavailable`, `stale_projection`) is a
  new outcome; both codes were already allowed for this operation.
- **The effective page limit is capped at 100** in the service as a second
  boundary. The contract already rejects a requested limit above 100.
- **A page can be shorter than `limit`** when it would pass 64 KiB (measured on
  the whole response frame); a continuation follows, and the same cut applies to
  `working_context`. Ordinary pages are far under the cap.
- **Out-of-profile metadata is omitted** rather than returned unbounded (above).
- **A `repository_target` naming no repository** matches versions whose
  projection also names none, rather than versions with an `applicability`
  object and no repository. The target's `repository_id` is optional in the
  contract and a target that names one is filtered exactly as before.

## Verification instruments

`test_engineering_preview_projection.py`, over the real production surface:

- the SQL of every governed search read, traced as SQLite executes it, names no
  body column and not the source view, in both modes, every view, and every
  page; a control shows the same trace does catch the pack builder's reads;
- every function that hydrates a body raises if it is reached;
- the stored bodies are corrupted after the projection was written (content no
  longer decodes, claim lineage is garbage) and search answers as before, while
  a hydrating read fails;
- a denied version's assembly is never named in a projection read, the label
  fold precedes it, and the scorer is handed only admitted, filtered candidates;
- bounds on long astral-plane text, the response byte cap across pages, the
  limit clamp, the cursor's scope and order binding and its content-digest
  binding;
- absent and stale rows refuse, a rebuild restores them, the guard refuses a
  forged or drifted row, and the table is append-only;
- the migration's own view over 29 edge-case contents agrees with a stated
  Python reference (code points not bytes, NUL, non-string fields, oversized
  metadata), and non-object, invalid and non-engineering content have no row;
- every writer of a version projects it; a workspace upgraded from 0052 is
  backfilled to the rows a fresh workspace writes, and reaches the canonical
  schema.

## Still hydrates a body, or reads more

- **`working_context` search** reads each of the principal's checkpoint
  payloads to match the objective. Checkpoints are continuity evidence, not
  governed observation bodies, so this is outside the AC-033 observation views
  and unchanged. A bounded-objective projection over checkpoints is the same
  technique.
- **`engineering.expand`, `context.priority.set` and `engineering.review.record`**
  resolve their exact version through `_visible_records`, which hydrates every
  version the caller can see in every view to answer a visibility question, and
  **`engineering.context.build`** hydrates the versions it renders. These are
  exact reads and expansion, where hydration belongs; `_visible_records` is
  wider than it needs to be and is a candidate for the same frontier read.
- **`current_safe` evaluation** reads the target's and each candidate's baseline
  source manifest. Those are source snapshots recorded by a trusted producer, not
  record bodies, and are unchanged.
- **The frontier's digest** (`AuthorizedMemoryFrontier.digest`) is still
  computed on every read although engineering search does not use it.

## Migration pin

| Allocation | File | Predecessor | Introducing commit | Normalized SHA-256 |
|---|---|---|---|---|
| 53 | `0053_engineering_preview_projection.sql` | 52 | `97ada9ba23e3a8ef890cfdd1ee0bd03a468bf34d` | `7028ce30eb13b91288bf91968bd2cd431592016e19b8224e6924763e91db50e5` |

A candidate owned by Engineering Memory, with `accepted_commit` null until the
normal acceptance process records a landing. Migrations 0051 and 0052 and their
pins are untouched.

## Verification

- `test_engineering_preview_projection.py`: 59 passed, including SQL read
  tracing, authority ordering, stale and missing projection refusal, bounded
  responses, migration backfill, and guarded writes.
- Adjacent source coverage, dependency carry and lookup, retrieval, and context
  build suites: 96 passed after the final migration edit.
- Migration allocation checker and registry suite: passed; 73 tests passed.
- Ruff on changed Python files and strict mypy on the runtime package: passed.

These checks qualify this implementation slice. They do not stand in for the
full v1 acceptance register or measured 10k/100k corpus performance.
