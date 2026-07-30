# OmniVia Core

OmniVia Core is a public, local-first, backend-neutral portable knowledge
substrate.

It is not a note app, graph UI, scanner, sync service, hosted service, provider
router, MCP server, CLI runtime, or assistant installer. Core owns portable
contracts, validation, normalization, extension semantics, public API exports,
static fixtures/examples, and documentation that other repositories or tools
can build on safely.

## Positioning

Core is designed for:

- developers building graph-backed or knowledge-backed applications
- AI agent builders who need typed, source-grounded, reviewable context
- researchers working with claims, citations, and evidence strength
- personal knowledge builders modeling notes, links, tags, and tasks
- team knowledge builders modeling decisions, workflows, and risks
- Obsidian-like tool builders who need a portable contract surface
- Graphify-like codebase-map builders who need a portable graph fragment shape
- future OmniVia Platform, Dev, and Apps implementers

Core alone is not designed for:

- a complete note editor or publish/sync workflow
- a vault scanner, repo scanner, parser, or importer runtime
- a visual graph explorer or query runtime
- provider enrichment, model routing, or hosted storage
- direct CLI, MCP, or assistant-install surfaces

## Principles

- `local-first`: contract shapes assume local artifacts and local provenance.
- `backend-neutral`: contracts do not assume SQLite, Neo4j, vector DBs, or any
  specific storage/query engine.
- `developer-first`: exports are explicit, typed, reviewable, and easy to
  validate in tests and fixtures.
- `agent-safe`: confidence, review status, evidence strength, sensitivity, and
  missing-evidence markers stay first-class.
- `portable`: the same contract layer can represent vaults, codebases, research
  corpora, team workspaces, workflow systems, and agent memory.

## Repository Boundary

`omnivia-core` owns:

- portable knowledge contracts
- graph fragments, source refs, and schema version helpers
- validation helpers and normalization helpers
- extension manifests and namespace rules
- static examples, fixtures, adapter docs, and public-safe documentation

`omnivia-core` also still ships a small set of repo-local reference
implementations for memory, persistence, ingestion, search, and graph assembly.
Treat those as transitional code that currently lives here, not as a claim that
Core is the long-term runtime owner for those surfaces.

`omnivia-core` does not own:

- long-term ownership of ingestion, indexing, parsing, scanning, or watcher lifecycle
- long-term ownership of persistence lifecycle, caches, sync, or background jobs
- long-term ownership of query runtime, UI runtime, desktop runtime, or hosted runtime
- provider/model calls or assistant installation
- MCP serving, CLI runtime, or repo-specific tool workflows

## Comparison

Obsidian-like tools:

- Core can represent notes, wikilinks, derived backlinks, tags,
  frontmatter-derived properties, canvas/card-like objects, embedded files, and
  note-to-task links.
- Core does not try to become a note editor, plugin runtime, publish flow, or
  sync layer.

Graphify-like tools:

- Core can represent portable graph fragments, extracted/inferred/ambiguous
  confidence, source-backed code/document links, and bounded extension
  annotations such as `graphify:god_node` and `graphify:surprise_edge`.
- Graphify remains reference-only. Do not add `graphifyy` as a dependency.
- Core does not become a scanner, cache, query CLI, MCP server, or installer.

## Dependency Posture

The following are reference-only or future integration concerns, not default
Core dependencies:

- Graphify and other code-graph tools
- Obsidian and note-app/plugin runtimes
- tree-sitter language packages
- Markdown parser runtimes
- graph databases and vector databases
- model/provider SDKs
- MCP servers and CLI runtimes

## Package Topology

The repository root is the canonical `omnivia-core` distribution
(import package `omnivia_core`, under `src/`). Three sibling skeleton
distributions live under `packages/` and depend on `omnivia-core`:

```text
                    omnivia-core
                  ^      ^      ^
                  |      |      |
omnivia-core-runtime   omnivia-core-mcp   omnivia-core-cli
```

| Distribution | Import package | Location | Depends on |
|---|---|---|---|
| `omnivia-core` | `omnivia_core` | `src/omnivia_core` | — |
| `omnivia-core-runtime` | `omnivia_core_runtime` | `packages/omnivia-core-runtime` | `omnivia-core` |
| `omnivia-core-mcp` | `omnivia_core_mcp` | `packages/omnivia-core-mcp` | `omnivia-core` |
| `omnivia-core-cli` | `omnivia_core_cli` | `packages/omnivia-core-cli` | `omnivia-core` |

Rules enforced by `scripts/check-package-boundaries.py`:

- `omnivia-core` never depends on or imports any sibling distribution or the
  legacy `omnivia_memory` implementation.
- `omnivia-core-runtime`, `omnivia-core-mcp`, and `omnivia-core-cli` each
  declare a compile-time dependency on `omnivia-core`.
- `omnivia-core-mcp` and `omnivia-core-cli` never depend on or import
  `omnivia_core_runtime`.

All four packages are a **package-boundary skeleton only**: `omnivia_core`,
`omnivia_core_runtime`, `omnivia_core_mcp`, and `omnivia_core_cli` currently
expose package identity/version metadata and nothing else. There is no
runtime, MCP, or CLI implementation yet, and no compatibility facade has been
created for the legacy `omnivia-memory` implementation. The reference
implementation that other tooling should still use today continues to live,
unchanged, at `services/omnivia-memory` (import package `omnivia_memory`) — see
[Repository Split](#repository-split) below.

### Boundary and build checks

Run the boundary checks (manifest and AST-based) and their tests:

```bash
.venv/bin/python -m pytest tests -q
.venv/bin/python scripts/check-package-boundaries.py
```

Run a clean, isolated build/install check for all four distributions. This
builds a temporary wheelhouse and installs each distribution into its own
temporary virtual environment; it writes nothing under the repository tree:

```bash
PYTHON=.venv/bin/python scripts/check-package-builds.sh
```

## Application Contract v1

`omnivia_core.contracts.v1` is a provider-neutral wire contract for
application-layer request/response negotiation: version and capability
negotiation, request/response envelopes, and typed, retry-classified errors.
It is a foundation only — there is no per-operation payload catalogue, HTTP
binding, or transport implementation yet.

Canonical source and generated artifacts:

- `contracts/application/v1/schemas/*.schema.json` — sixteen JSON Schema
  Draft 2020-12 documents (`common`, `compatibility`, `errors`, `envelopes`,
  `service`, `records`, `jobs`, `operations`, `workspace`, `memory`,
  `evidence`, `knowledge`, `graph`, `context-pack`, `compatibility-matrix`,
  and the reference-only `application-v1` registry). These are the single
  source of truth; everything else is derived from them.
- `contracts/application/v1/fixtures/` — thirteen canonical example wire
  documents plus `manifest.json`, covering compatible negotiation, capability
  denial, an incompatible major version, a minimal request, a retryable
  mutation, a rich success response, a plain error response, tolerant decoding
  of an additive unknown field, an unrecognized open vocabulary value, a
  duplicate capability id, a response carrying both `result` and `error`, a
  response carrying neither, and a pattern-compatible but calendar-invalid
  RFC 3339 timestamp.
- `src/omnivia_core/contracts/v1/generated.py` — generated frozen dataclasses,
  type aliases, and frozen vocabulary constants. Standard library only.
- `src/omnivia_core/contracts/v1/codec.py` — tolerant production wire codec
  (canonical JSON, response-branch dispatch, retry semantics). Standard
  library only.
- `src/omnivia_core/contracts/v1/compatibility.py` — pure version-window and
  capability-negotiation semantics (version comparison, effective-capability
  intersection, duplicate-id detection, requirement resolution) plus the
  whole-envelope invariants `decode_response` enforces: every declared version
  parses, the versions in force (`api_version`, `workspace_format_version`)
  equal the ones negotiation selected, and each selected version falls inside
  the window the same envelope publishes as supported. The open
  `CompatibilityStatus` vocabulary is deliberately left unconstrained, so a
  newer peer's unseen status still decodes. Standard library only.
- `src/omnivia_core/contracts/v1/semantics.py` — pure semantic validation for
  the workspace and governed-memory DTOs (A2.2, ADR-038/ADR-039): domain-scope
  and identifier/open-code shape checks, temporal ordering, evidence/
  currentness/supersession/authority coherence, `memory.create` proposed-only
  result-tuple enforcement (including the reserved-field decode guard), and
  candidate evidence/provenance coherence. One shared pair of lineage rules
  validates a claim's `assertion`/`extraction` wherever it appears — on the
  `memory.create` input that supplied it and on the `RecordProvenance` that
  preserved it — so a governance transition, which can only see that both
  versions preserved lineage identically, cannot wave through lineage that is
  identically malformed on both sides. A record's *history* evidence is
  validated intrinsically but is deliberately never required to cite a source
  the current version declares: history is append-only and survives
  supersession onto wholly different evidence. Every public function here is a
  direct entry point and type-guards what it is handed, so a hand-built
  dataclass raises `ContractSemanticError`, never a raw
  `TypeError`/`AttributeError`. Standard library only.
- `src/omnivia_core/contracts/v1/semantics_evidence.py` — pure semantic
  validation for the L0 `evidence.search` DTOs (A2.3, ADR-039): evidence
  artifact integrity, tombstone rules, and sensitivity/leakage enforcement.
  Standard library only.
- `src/omnivia_core/contracts/v1/semantics_knowledge.py` — pure semantic
  validation for the governed-knowledge, graph, and Context Pack DTOs (A2.3,
  ADR-039): `knowledge.search` default-canonical-view leak prevention, the
  governance-transition closure shared by `knowledge.propose` /
  `candidate.approve` / `candidate.reject` / `record.supersede` (frozen
  state/authority/reviewer matrices, precondition binding, exactly-one audit
  event with the request rationale carried over verbatim, exact claim
  preservation or exact replacement binding), graph-traversal projection
  integrity, and Context Pack reproducibility/citation/budget invariants.
  Standard library only.

  `validate_projection_freshness` is the one shared rule every
  projection-served read composes, so no two operations can drift on what a
  staleness statement has to say: `ProjectionFreshness` carries both
  `projection_versions` (which version served this read) and
  `projection_watermarks` (how far each projection has consumed the write
  model), each non-empty and keyed by exactly the same projection names — a
  version without its watermark leaves a caller unable to tell how far behind
  the write model that projection actually is.

  `graph.traverse` is a projection over governed records, never a second
  canonical authority, so its result validator takes the *complete original
  request* plus the canonical resolution time and the caller's authorized
  views, and re-validates the request rather than trusting a view string:
  every node and relation record must belong to the selected workspace, honour
  the requested `domain_scope`/`relation_types` filters, and be exactly what
  the resolved view permits (`current_canonical` — always allowed, never gated
  — requires exact `l2`/`accepted`/`current` records carrying `canonical`
  authority, a reviewer, and a validity window containing the resolution
  instant; `candidates` and `history` must be both explicitly requested and
  present in `authorized_views`). The `history` view is not merely the same
  shape across the two reads but literally the same code: `knowledge.search`
  and `graph.traverse` compose one shared historical-canonical rule, so the two
  cannot drift on what proves a version was once canonical. History returns the
  versions that *were* canonical knowledge, so `l2`/`accepted`/`superseded`
  alone does not qualify one — it must also carry `canonical` authority and a
  reviewer, and have been superseded no later than the canonical resolution
  time, since a version replaced only *after* the instant a read resolved at
  was still the canonical answer at that instant. A superseded version's
  `valid_from`/`valid_until` window is deliberately *not* required to contain
  the resolution instant: supersession, not validity, is what places a version
  in the past. A `GraphEdge` references the relation record
  through `relation_reference` and never re-identifies it, edges order by the
  complete `(source, relation_type, target, relation_reference)` tuple rather
  than endpoints alone, and a missing endpoint is stated rather than implied:
  it carries a `GraphBoundaryReason` that must actually hold here —
  `page_boundary` only with a continuation token and the node limit reached
  exactly, `depth_boundary` only when the endpoint that is present sits at the
  applied depth. Naming *both* endpoints is the claim that the edge is fully
  materialized on this page, so both must be nodes the result actually
  returned; an end the traversal did not reach is stated by omitting that
  endpoint with a boundary reason, never by naming a record the result never
  returned. `page` is likewise a continuation *position*, not a flag: present
  on either the request or the result it must name a real continuation token,
  since a request naming nowhere to continue from and a result offering a next
  page it cannot name are both meaningless — a last page, and a request to
  start at the beginning, are stated by omitting `page` entirely.

  A traversal also answers the question it was asked, and depth is measured
  from the seeds it was asked to start from — which is what an absent request
  `page` makes checkable. Four rules state that, each checked directly: on a
  *first* page every identity in `start` comes back as a node, a returned seed
  sits at depth 0 and never deeper, a node that is *not* a requested seed never
  sits at depth 0 on any page, and a continuation page — `page` present on the
  *request* — does not return a requested seed again at any depth, since an
  earlier page already did. Together the first two make a first page return
  every seed exactly once at depth 0, so a result can never omit some or all of
  its seeds and hand back unrelated nodes instead — including the degenerate
  empty `nodes` list, which page metadata on the *result* does not excuse,
  since offering a next page says nothing about what this one already owed. The
  third closes the half no seed-presence check can: a result may return
  everything it owes *and* still smuggle a spurious record in at depth 0. Two
  consequences follow without a check of their own — every node on a
  continuation page sits at depth 1 or deeper, and the node budget always holds
  what a first page owes: a requested `node_limit` must be at least
  `len(start)`, and so must a first page's `applied_node_limit`, including when
  the request named no limit and the server tightened it on its own. Projection
  loss is never canonical-data loss.

  `context_pack.build` is a synchronous, non-persisting read of the current
  canonical view, and its result is a *content-addressed artifact*: `pack_id`
  and `reproducibility.artifact_checksum` are both the SHA-256 of the RFC 8785
  canonical UTF-8 bytes of the complete result with exactly those two members
  removed — nothing else is excluded, so the generation time, the authority
  context, the freshness statement, every policy and configuration version, the
  budget, the sections, the citations, and every selected item are all covered.
  Because identity is content, holding a `pack_id` grants nothing: it is a value
  anyone can recompute, and `fresh_authorization_required` stays literally true.

  That posture is what shapes the rest. The input carries no view selector, no
  point-in-time selector, no pagination, and no persistence, expiry, retention,
  snapshot, or job control — and because the production decoder ignores unknown
  fields by design, `decode_context_pack_build_input` refuses those names on the
  *raw* payload before the tolerant decode, rather than silently handing back a
  synchronous pack to a caller who asked for a persisted or paginated one. That
  refusal covers the request's *top-level* members and stops there, which is the
  ADR-038 rule rather than an omission: an additive unknown optional field is
  dropped whole by the tolerant decoder, so a `future_envelope.snapshot_id`
  never reaches the five scalar fields `ContextPackBuildInput` has and cannot be
  honoured in part — while rejecting it would fail a document a compatible later
  minor release is entitled to send. Only a name at the top level competes with a
  field this operation actually decodes.
  `ContextPackMode` stays wire-open but recognizes only `deterministic_view`;
  `immutable_snapshot` and `returned_artifact` are refused by name so the failure
  says why. Authority is recorded structurally rather than as an opaque
  fingerprint — principal, roles, capabilities, scopes, purpose, and policy
  versions, each compared for exact equality against what the caller vouches for
  — because two hashes that differ tell a reviewer nothing about what changed.

  Some of `reproducibility` is not like that, and the distinction is worth being
  explicit about. The normalized query and normalization version, the
  builder/retrieval/ranking/reranking/selection
  versions, the tokenizer id and version, the summarizer version, and the model
  versions are all *the producer's own statements about the producer*: nothing
  inside the artifact can contradict them, so with no expectation supplied the
  validator can only check their shape — which must not be mistaken for
  verification. Both entry points therefore take an optional `expected_*` keyword
  argument for each one; supply it and the value is bound by exact equality,
  omit it and only the shape check runs. `expected_model_versions` needs no
  sentinel for "expected absent": `model_versions` is required and may be empty,
  so `{}` expects a build that used no model and `None` expects nothing.
  Everything already bound externally — workspace, authority, scopes, purpose,
  policy versions, request, freshness, resolution time — stays mandatory.

  `authorization_context.authorized_candidate_set_checksum` used to sit in that
  optional list and no longer does, because it is the one field that must never
  be taken on trust. It is a digest of something that never appears in the
  artifact, so byte-level integrity cannot reach it and a value read back out of
  the pack agrees with itself whatever the pack actually ranked over. Both entry
  points now *require* `expected_authorized_candidate_set`: a
  `ContextPackAuthorizedCandidateSetManifest` naming the complete authorized
  frontier, supplied out of band. The frontier is the candidate set after
  retrieval, after the request's own scope, and after workspace, scope, purpose,
  capability, policy, ACL, and sensitivity authorization, frozen *before* the
  first ranking, reranking, selection, or budget decision — neither the whole
  workspace nor a restatement of what the pack selected. Its digest is
  recomputed here and compared, its workspace must be the validated one, and
  every selected item must be a member of it under its own exact partition,
  which is what makes "nothing enters a pack after the frontier is frozen"
  checkable rather than asserted.

  The manifest carries workspace domain separation plus immutable identities and
  nothing else — no content, excerpts, provenance, spans, scores, distances,
  ranks, tie-breaks, selection flags, citations, sections, query or normalization
  state, and no authority, policy, configuration, or projection version — because
  the digest must depend on which authorized material existed and on nothing a
  later ranking step could change. It is in-process, identity-only trusted input:
  never a response field, never logged, and referenced by no other definition in
  the contract. Duplicates are refused rather than collapsed (a repeated tuple,
  one evidence identifier at two content checksums, one governed
  `(record_id, version)` repeated within a partition or claimed by two of them),
  different versions of one record stay two ordinary candidates, and an unknown
  partition fails closed. Every identity component is drawn from the *exact*
  domain of the item it names — `evidence.EvidenceId` and
  `evidence.EvidenceChecksum` for an L0 artifact, `records.RecordId` and
  `records.RecordVersion` for a governed version — referenced by `$ref` and
  applied by the same validators those items pass, never restated as a local
  pattern that could drift. A frontier states which real items were authorized,
  so an identity no artifact and no record could carry names a membership that
  cannot exist, and both the schema and the checksum helper refuse it rather than
  hash it. Candidates are a set: a flat array is sorted by
  `(partition, remaining identity components in declaration order)` under the same
  unsigned UTF-16 code-unit comparison RFC 8785 uses for member names, with no
  Unicode normalization, giving the partition order `context_models`, `evidence`,
  `history`, `records`. RFC 8785 orders object members but never array elements,
  so that sort is part of the definition rather than of the canonicalization.
  That comparison is normative because the element sort and the canonicalization
  it feeds must be one ordering rather than two, not because a v1 identity can
  exercise the difference: all four identity alphabets above are ASCII, printable
  ASCII, or an ASCII-restricted checksum, and over ASCII code-unit order and
  code-point order coincide exactly, so no valid candidate distinguishes them.
  Where the two genuinely diverge is arbitrary-Unicode object member names, and
  that divergence is proved against `utf16_sort_key` and the RFC's own
  property-ordering vector in `tests/contracts/test_canonical_json.py` — not by
  admitting candidate identities the contract does not allow. What the fixture's
  mixed vectors still pin is that the comparison is over raw code units rather
  than a locale collation or a case-insensitive fold: `ev-B` sorts before `ev-a`.
  The
  preimage is
  `{"format": "omnivia.context-pack.authorized-candidate-set.v1", "workspace_id": …, "candidates": […]}`
  and the digest is `sha256:` plus the lowercase hex SHA-256 of its canonical
  UTF-8 bytes. The empty set is valid: for `ws-1` its canonical bytes are
  `{"candidates":[],"format":"omnivia.context-pack.authorized-candidate-set.v1","workspace_id":"ws-1"}`
  and its digest is
  `sha256:666dd0b418f32f6fc03a5f87e430efaf6f9a6e5d50569b5fd74eb87e8b864b41`.
  `tests/contracts/fixtures/context-pack-authorized-candidate-set-v1.json` freezes
  that and eight more vectors, plus the manifests that must never digest at all,
  so a second implementation can be held to the same bytes.

  Resolution-time closure is complete rather than representative:
  `canonical_resolution_time` is an inclusive upper bound on *every* act and
  observation a selected item carries, not only on the one or two instants a
  partition rule happens to read. For a selected evidence artifact that is
  `temporal.event_at`, `observed_at`, `ingested_at` and `recorded_at`,
  `source.retrieved_at`, every `provenance_history[].occurred_at`, and every
  `provenance_history[].evidence[].source.retrieved_at`. For a selected governed
  record in `records`, `history`, or `context_models` it is
  `provenance.temporal.event_at`, `observed_at`, `ingested_at` and `recorded_at`,
  every `provenance.sources[].retrieved_at`, every
  `provenance.history[].occurred_at`, every
  `provenance.history[].evidence[].source.retrieved_at`,
  `provenance.assertion.asserted_at`, every
  `provenance.assertion.evidence[].source.retrieved_at`, and
  `provenance.extraction.extracted_at`. A pack presenting an act that had not
  happened when it resolved is the deterministic-view guarantee read backwards,
  whichever depth it sits at. Equality passes and only a strictly later instant is
  refused; the rejection is a refusal rather than a repair — provenance is
  append-only, so an out-of-range instant is never dropped or truncated to make
  the item selectable. `assertion.proposed_valid_from` and `proposed_valid_until`
  are deliberately *not* bounded: a proposed effective date in the future is an
  ordinary claim rather than an act, and refusing it would make a forward-dated
  proposal unselectable for a reason unrelated to when the pack resolved. A record
  whose own validity contains the resolution instant may propose taking effect from
  a later one and remains selectable. No causal or ordering rule is added between
  these instants, and the bound applies to selection into a pack only — the generic
  record and evidence rules are unchanged.

  The closure sits *alongside* each partition's validity and supersession rules
  rather than replacing them, so those four rules are restated here exactly rather
  than left as "unchanged". All four are evaluated against
  `canonical_resolution_time`:

  - **`evidence`** — validity contains the resolution instant inclusively
    (`valid_from` no later than it, `valid_until` no earlier, equality accepted at
    both ends); `superseded_at` absent or *strictly after* it. Equality is
    **rejected**: an artifact replaced at the very instant a pack resolved was
    already not the live one.
  - **`records`** — validity contains the resolution instant inclusively, on the
    same two boundaries; the version must be current and unsuperseded at it
    (`currentness` exactly `current`, and `superseded_at` **absent outright,
    irrespective of timestamp**). Not merely absent at or before the resolution
    instant: a current version records no supersession at all, so a `superseded_at`
    strictly *after* the resolution instant is refused exactly as one at or before it
    is, and a version that states when it was replaced belongs to `history` whichever
    side of the instant that statement falls on. A version valid only from a later
    instant was not yet in force; one that expired earlier was no longer the answer.
  - **`history`** — `superseded_at` is required and must be at or before the
    resolution instant, with equality **accepted**: a version replaced at that
    instant was already history by it, and one superseded only afterwards was still
    canonical then. This is the deliberate mirror image of the `evidence` boundary,
    and the two differ because they ask opposite questions of the same instant.
    Validity containment is deliberately *not* required — a historical version's
    window has usually closed, which is what makes it historical, so requiring
    containment would empty the partition of exactly what it carries.
  - **`context_models`** — the same current rules as `records`, at L3 rather than
    L2, including `superseded_at` absent outright irrespective of timestamp. Only
    `layer` differs.

  One consequence is worth stating because it bounds what the closure itself can be
  tested through: on `history`, the intrinsic record chain `recorded_at <=
  superseded_at` composed with that partition's `superseded_at <= resolution` already
  forces `recorded_at <= resolution` (and `ingested_at <= recorded_at` carries it one
  step earlier). So a future `ingested_at`/`recorded_at` on a historical version is
  refused by the intrinsic rules *before* the nested closure is consulted. Those two
  instants are still bounded — just not by this rule — and the tests assert that
  refusal explicitly rather than claiming a closure diagnostic that never fires.
  Every selected item across the four partitions (L0 evidence, current canonical
  L2 `records`, historical canonical L2 `history`, current canonical L3
  `context_models`) is held to the same shared temporal and authority rules
  `knowledge.search` and `graph.traverse` apply, is cited at least once, and
  appears in `reproducibility`'s exact version sets; every citation resolves to
  something the pack actually selected and is used by at least one section.
  Every identity-bearing array is in strictly ascending order under the *same*
  unsigned UTF-16 code-unit ordering RFC 8785 imposes on object member names, so
  a pack cannot be canonically ordered by this contract's rule and out of order
  by the one its own checksum is computed under.

  Integrity verification needs the bytes, not a decoded value, so there are two
  entry points and the difference is stated rather than implied.
  `verify_context_pack_artifact_document` (and the full
  `validate_context_pack_build_result_document`) parse raw JSON text or UTF-8
  bytes with duplicate-member detection, require the raw object to round-trip
  exactly through the strict DTO, and only then hash — because an ordinary parser
  keeps the last of a duplicated member and the tolerant decoder drops unknown
  fields, so a digest computed after either would attest to a document nobody
  sent. `validate_context_pack_build_result` and
  `compute_context_pack_artifact_digest` take an already-strict trusted value and
  document that they cannot recover what an earlier parser discarded. Ordinary,
  non-integrity decoding stays tolerant exactly as ADR-038 requires.
- `src/omnivia_core/contracts/v1/canonical_json.py` — the RFC 8785 JSON
  Canonicalization Scheme the Context Pack digest is defined over, kept separate
  from the contract semantics so it can be audited on its own. Standard library
  only, and deliberately not `json.dumps(sort_keys=True)`: object members are
  ordered by unsigned UTF-16 code unit (which differs from code-point order for
  every name containing a supplementary character — the case the RFC's own
  property-ordering example turns on), and numbers are rendered by ECMAScript's
  `Number::toString`, which JCS defers to and which disagrees with Python's
  `repr` on integral values, on the 21-digit threshold where the exponential
  form takes over, and on exponent spelling. The input domain is I-JSON,
  enforced recursively: finite binary64 numbers only, Python integers only when
  the conversion is lossless, strings that are valid Unicode scalar sequences
  with no lone surrogate, string-keyed objects, and no duplicated member name.

  Two separate claims live here, and only one of them is universal.
  **Output:** every value this module *accepts* serializes byte-for-byte as
  RFC 8785 specifies, which is what makes
  `reproducibility.artifact_canonicalization` legitimately `rfc8785`.
  **Input:** OmniVia applies a stricter lossless-binary64/I-JSON *acceptance
  profile* than "any document RFC 8785 could be applied to". A valid JSON numeric
  literal whose value cannot round-trip through binary64 — `9007199254740993`,
  say — is **rejected before canonicalization**, where a conforming JCS
  implementation would follow ECMAScript and silently emit the rounded
  `9007199254740992`. Rounding is fine for serialization and fatal for a content
  address: a digest over a rounded value attests to a document the sender never
  wrote. The line falls where the JSON parser stops being lossless — integer
  literals arrive exact and are refused when out of range, fractional literals
  arrive already rounded to the nearest double and canonicalize exactly as JCS
  specifies (`0.1000000000000000055511151231257827` → `0.1`). So: OmniVia accepts
  a strict subset of RFC-8785-canonicalizable documents and canonicalizes every
  member of that subset exactly as RFC 8785 requires; it does not accept every
  possible RFC 8785 document and does not claim to. The policy is frozen —
  loosening it would change which documents verify, which is a contract change.

  Stated provider-neutrally, Context Pack format 1.0 applies RFC 8785 byte
  serialization to an admitted I-JSON data model. Before canonicalization every
  number must be a finite binary64; additionally, any number token written in
  *integer form* — and any direct host-language *integral value*, which never
  passed through a parser at all — is admitted only when converting it to
  binary64 and back to the mathematical integer is exact. Decimal and exponent
  tokens are read as finite binary64 under ordinary JCS rules, with no
  requirement of an exact decimal rational representation. This is an admission
  rule, **not** a "safe integer" range: exact larger integers such as powers of
  two stay valid. The frozen boundaries are `9007199254740991` accept,
  `9007199254740992` accept, `9007199254740993` reject, `-9007199254740993`
  reject, `1152921504606846976` accept with canonical form
  `1152921504606847000`, `1152921504606846977` reject, `0.1` accept, `1e+21`
  accept, and `1e400` reject as non-finite. They are restated as raw JSON text —
  never as host-language values, which a parser would already have rounded — in
  `tests/contracts/fixtures/context-pack-canonicalization-v1.json`, with the
  canonical form for every accepted vector and a stable error category for every
  rejected one, so a second implementation can be held to the same line.
  `reproducibility.artifact_canonicalization` stays exactly `rfc8785`: the rule
  narrows which documents are admitted and changes no output byte.
- `src/omnivia_core/contracts/v1/resources.py` — standard-library-only
  accessors for the packaged schemas and fixtures (see below).
- `generated/typescript/application/v1/index.ts` — the same contract surface
  as a declaration-only TypeScript module.

`contracts/application/v1/{schemas,fixtures}` stay the only checked-in
canonical copy. The built wheel force-includes them under
`omnivia_core/contracts/v1/resources/{schemas,fixtures}` (see
`[tool.hatch.build.targets.wheel.force-include]` in `pyproject.toml`), and
`omnivia_core.contracts.v1` exposes `list_schema_names`, `read_schema`,
`read_schema_text`, `list_fixture_files`, `read_fixture`, `read_fixture_text`,
and `read_fixture_manifest` as the only supported way to read that packaged
copy through `importlib.resources`.

Regenerate and verify:

```bash
.venv/bin/python scripts/generate-application-contracts.py         # regenerate
.venv/bin/python scripts/generate-application-contracts.py --check # verify no drift
.venv/bin/python scripts/check-application-contracts.py            # conformance gate
```

The conformance gate checks the canonical schema directory holds exactly the
sixteen frozen schema documents (an extra one would be read by no check yet
packaged by the wheel, and a missing one is reported in the same place),
validates every schema against the Draft 2020-12
metaschema and its exact `$schema`/`$id`, resolves every `$ref` offline,
checks the registry publishes exactly the source schemas' definitions and
lists their exact URIs, validates every fixture against its declared
schema-validity (RFC 3339 `format` included, via `jsonschema.FormatChecker`)
and declared `tolerant_decode` outcome by actually running the production
codec, validates the manifest itself (unique nonempty ids, unique existing
files, explicit boolean flags, known semantic keys, and an exact match
against a frozen id/file/semantic mapping so deleting, renaming, or swapping
a fixture's assertion cannot stay green), runs every fixture's semantic
assertion, and checks that `src/omnivia_core/contracts` has no import outside
the standard library or its own package — including relative imports that
resolve elsewhere, and constant-string `__import__`/`importlib.import_module`
escapes under any alias (`import importlib as il`, `from importlib import
import_module as load`), with a non-literal argument failing closed.

The schema-set check complements, rather than replaces, the exact wheel
resource-set assertion in `scripts/check-package-builds.sh`: that one proves
the built wheel packages exactly what the canonical directory holds, which is
only worth having once this one has established that the directory holds
exactly the frozen set.

Run the contract test suite:

```bash
.venv/bin/python -m pytest tests/contracts -q
```

`jsonschema[format]` (plus its `types-jsonschema` stubs) is a
development-only dependency declared under `[dependency-groups]` in
`pyproject.toml`, used only by `scripts/check-application-contracts.py` and
its tests — the `format` extra provides the RFC 3339 calendar validation
`jsonschema.FormatChecker` needs. The contract package itself (`generated.py`,
`codec.py`, `compatibility.py`, `semantics.py`, `semantics_evidence.py`,
`semantics_knowledge.py`, `canonical_json.py`, `resources.py`) has zero runtime
dependencies, including on `jsonschema` and on any third-party canonicalizer.

The generated TypeScript module is regenerated and checked for drift the same
way as the Python module, and is strict-compiled (`--strict --noEmit
--skipLibCheck`, target `ES2022`, module `ESNext`, module resolution
`Bundler`) by `scripts/check-application-typescript.sh`.

The compiler is a repository-local, version-pinned dev dependency: TypeScript
is declared at the exact version `5.9.3` in `package.json` and locked in
`package-lock.json`, so `npm ci` reproduces the same compiler every time. The
check needs no sibling repository and no global install, and adds no runtime
JavaScript dependency — `typescript` is the only entry, and it is a
`devDependencies` one:

```bash
npm ci                                 # install the pinned compiler (once)
npm run check:application-contracts    # strict, no-emit contract compile
```

The npm script runs the shell gate, so the two are never separate copies of
the compiler flags. The shell gate can also be run directly, and resolves a
`tsc` binary in order: the `TSC` environment variable (explicit override), the
repository-local `node_modules/.bin/tsc` (the reproducible default), then
`tsc` on PATH:

```bash
scripts/check-application-typescript.sh
TSC=/path/to/tsc scripts/check-application-typescript.sh   # explicit override
```

## Repository Split

| Repository | Visibility | Purpose |
|---|---|---|
| `omnivia-core` | Public | Portable contracts, validators, normalizers, fixtures, and public docs. |
| `omnivia-platform` | Private | Runtime lifecycle, desktop shell, UI/runtime boundaries, sync/distribution concerns. |
| `omnivia-dev` | Private | Query tooling, MCP/CLI surfaces, repo indexing, and developer workflows. |
| `omnivia-cloud` | Private | Future hosted/cloud implementation placeholder. |
| `omnivia-pm` | Private | Backlog, planning, ADRs, research reviews, and implementation packets. |

## Docs Map

- [Portable Knowledge ADR](docs/adr/portable-knowledge-substrate.md)
- [Portable Knowledge Contract Spec](docs/specs/portable-knowledge-contract.md)
- [Obsidian-like Compatibility](docs/compatibility/obsidian-like.md)
- [Graphify-like Compatibility](docs/compatibility/graphify-like.md)
- [Portable Knowledge Launch Packet](docs/launch/portable-knowledge-launch-packet.md)
- [Examples](docs/examples/README.md)
- [Phase 0 Baseline Freeze](docs/baseline/phase-0-baseline-freeze.md)
- [Legacy memories.db Migration Criteria](docs/baseline/legacy-memories-db-migration.md)

## Checks

Install the public Core package locally for development:

```bash
python3 -m pip install -e services/omnivia-memory[dev]
```

Run the focused contract checks:

```bash
PYTHONPATH=services/omnivia-memory/src python3 -m pytest \
  services/omnivia-memory/tests/test_public_api.py \
  services/omnivia-memory/tests/test_knowledge_contract.py
```

Run the full package suite:

```bash
PYTHONPATH=services/omnivia-memory/src python3 -m pytest services/omnivia-memory/tests
```

Verify the Phase 0 baseline freeze (public exports, storage schema, dependency
drift, and golden fixtures):

```bash
scripts/check-core-baseline.sh
```

The PDF and DOCX ingestion tests need optional extractor dependencies that the
`dev` extra does not install. See the
[Phase 0 baseline freeze](docs/baseline/phase-0-baseline-freeze.md) for the
clean environment recipe.

## Public Import Example

```python
from omnivia_memory import (
    GraphConfidence,
    GraphSourceType,
    KNOWLEDGE_CONTRACT_VERSION,
    KnowledgeObject,
    KnowledgeSource,
    KnowledgeSpace,
    SourceRef,
    validate_knowledge_space,
)

source = KnowledgeSource(
    id="source-daily-note",
    space_id="personal-vault",
    source_type=GraphSourceType.NOTE,
    title="Daily Note",
    relative_path="notes/daily-note.md",
)
note = KnowledgeObject(
    id="daily-note",
    space_id="personal-vault",
    kind="note",
    title="Daily Note",
    tags=["daily-note"],
    source_refs=[
        SourceRef(
            source_id="source-daily-note",
            source_type=GraphSourceType.NOTE,
            path="notes/daily-note.md",
            confidence=GraphConfidence.EXTRACTED,
        )
    ],
    confidence=GraphConfidence.EXTRACTED,
)
space = KnowledgeSpace(
    id="personal-vault",
    title="Personal Vault",
    space_type="personal vault",
    contract_version=KNOWLEDGE_CONTRACT_VERSION,
    sources=[source],
    objects=[note],
)

assert validate_knowledge_space(space).valid
```
