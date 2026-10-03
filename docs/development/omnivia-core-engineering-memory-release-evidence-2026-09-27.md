# OmniVia Core — Engineering Memory release evidence manifest (§22.5)

**Date:** 2026-09-27 · **Contract version:** `x-omnivia-contract-version: 1.3` · **Migration head at PR #137 base:** 0053 · **Status:** draft on `codex/core-engineering-consumer-proof`; update against the final release commit before publication.

One row per shipped capability. "Producer" is the code path that writes authoritative state; "consumer" is the code path that reads it. A row with no production writer and consumer would not be listed. Test evidence names real test files; OS evidence is the CI platform matrix each PR ran (ubuntu/macos/windows lanes where listed).

| Capability | Producer | Consumer | Persistence | Test evidence | OS evidence | Exclusions / notes |
|---|---|---|---|---|---|---|
| Engineering observation ingestion | `handlers/memory.py` `create_memory_record` + engineering content-profile validator (`memory.create`, `engineering.observation` profile) | `memory.get` / `memory.list` / `memory.search`, `engineering.search` previews | `omnivia_governed_records` + evidence links (migration 0049 families) | `test_engineering_source_coverage.py`, `test_engineering_applicability.py`, `test_engineering_retrieval.py` | Phase 2 qualification lanes ×3 platforms | Proposed-only entry; no accepted state in input (EM-09) |
| Repository / checkout / snapshot identity | `handlers/engineering.py::engineering_source_record` + `storage/repository_identity.py` | `engineering.search` (repository targets), `engineering.context.build` (targets), pack applicability | migration 0047 (`omnivia_engineering_repositories`, `_snapshots`) | `test_engineering_repository_identity.py` | Core acceptance matrix | Registration is a trusted-producer operation; no model-facing registration tool (deliberate, §16.3) |
| Source streams and dependency manifests | `engineering.source.record` writer + `storage/engineering_source.py` | `current_safe` search/pack applicability resolution | migrations 0050–0052 (streams, events, dependencies, dependency sets) | `test_engineering_source_coverage.py`, `test_engineering_dependency_carry.py`, `test_engineering_dependency_lookup.py` | Core acceptance matrix | No external `RepositoryChangeSet` producer yet; coverage advances only on recorded source events |
| Captured working-tree coverage | trusted `service/source_capture.py` seal + `engineering.source.capture.commit` + bounded installed `EngineeringSourceCaptureExecutor` | captured per-path applicability lookup and the shared contiguous source barrier | migrations 0056 and 0058 (`_snapshot_files`, `_snapshot_captures`, `_source_stream_origins`, `captured_v1`, producer queue and cursors) | `test_working_tree_snapshot.py`, `test_engineering_captured_source_coverage.py` | Focused Core runtime suites; release matrix still required | Core's service tick polls independently of request traffic, renews during capture, recovers durable seals and fills linked gaps in order. Pending selection uses a next-eligible queue index, bounded retry timing and durable lane/queue/checkout cursors. A finite indexed legacy seeder has a frozen watermark. Platform change notifications and Dev parser/indexer/symbol/span adapters remain external work. The mutation is explicitly omitted from model-facing MCP. |
| Continuity sessions and checkpoints | `handlers/continuity.py` (register / append / close) | `continuity.handoff.read`, `engineering.search` `working_context` view, resume packs | migration 0048 (`omnivia_engineering_sessions`, `_checkpoints`) | `test_engineering_continuity.py`, `test_engineering_cli.py` | Core acceptance matrix + CLI vertical (macOS local; CLI surface covered by CI matrix) | Lease enforcement recorded-not-enforced (§7.3); single-principal (§19.4) |
| Progressive retrieval (preview / expand) | `handlers/engineering.py` search + `storage/engineering_preview.py` projection (migration 0053) | MCP `engineering_search` / `engineering_expand`, CLI `engineering search` / `engineering expand` | Preview projection (rebuildable) | `test_engineering_retrieval.py`, `test_engineering_preview_projection.py` | Evidence search platform qualification ×3 | No vector lane; lexical + structural only (§13.1) |
| Engineering context pack | `handlers/engineering.py::engineering_context_build` | MCP `engineering_context_build`, CLI `engineering context` | None (non-persisting read, §12.7) | `test_engineering_context_build.py`, `test_engineering_pack_render.py` | Core acceptance matrix | Historical diagnosis mode is a separate negotiated capability, not enabled |
| Context priority | `context.priority.set` audited upsert | search/pack selection boost (capped 10%, §13.3) | principal-scoped preference rows | `test_engineering_applicability.py` (priority sections) | Core acceptance matrix | Never changes governed state (EM-19) |
| Review and applicability attestation | `engineering.review.record` | applicability projection served to search/packs | assessments + attestations (migration 0049) | `test_engineering_applicability.py` | Core acceptance matrix | Review acknowledgement cannot clear stale/unknown applicability (§15.5) |
| MCP exposure | `packages/omnivia-core-mcp` manifest (restricted + authoring profiles) | installed MCP clients | n/a (exposure only) | `test_mcp_stdio_end_to_end.py`, `test_mcp_architecture_gates.py` | Core acceptance matrix | Curate profile not granted to agents by default (§16.4) |
| CLI surface | `packages/omnivia-core-cli` `surface.py` application commands | installed `omnivia` entry point | n/a (client) | `test_engineering_cli.py`, `test_v06_6_surface.py` bijection | CLI suite in Core acceptance | Mutations require explicit idempotency keys; preconditions via `--record-version` |
| Backup / restore of engineering state | `storage/backup.py` verified backup + restore | workspace restore flow | byte-for-byte restore | `test_engineering_restore.py` | Core acceptance matrix | Backup requires a quiesced writer (online-backup API blocks under a held lease) |

## Performance qualification

SPEC-CORE-ENGMEM-001 §20.2 qualification is produced by `packages/omnivia-core-runtime/tests/phase3/runtime/test_engineering_qualification.py` (`run_lane`; contract `validate_report`). No latency is asserted anywhere; every correctness gate (authorization partition, `current_safe` applicability, conflict visibility, budgets) is asserted on every sample, in every lane.

### Lane status

| Lane | Status |
|---|---|
| Harness, report contract and tiny-corpus smoke (ordinary suite, no wall-clock assertions) | Implemented; runs in the suite |
| 10k, report format `engineering-memory-qualification/2` | **Complete** at source `2ce3707a`; all three advisory warm targets passed |
| 100k, report format `engineering-memory-qualification/2` | **Complete** at source `f3de24f7`; checkpoint target passed, search and context targets missed |
| Reference-hardware run (4 cores / 16 GiB / local SSD) | **Complete** in the 100k report |
| Cold lane | Connection-cold: fresh SQLite connection, page cache and dispatcher per sample. The OS page cache is uncontrolled and recorded as such; no lane is called system-cold |

`benchmarks/reports/engineering-memory/lane-{10000,100000}.json` are the current
format-2 evidence. At 100k the worst warm search p95 was 2,505.244 ms against the
300 ms target, context-build p95 was 3,801.514 ms against 1,000 ms, and checkpoint
p95 was 21.636 ms against 200 ms. These are measured targets, not latency guarantees.
Earlier format-1 diagnostics at 2k and 3k remain historical only.

The cache note and derived `reference.release_*` fields in both committed reports
were corrected with the §20.2 interpretation in this pull request. Timing samples,
corpus identity, environment data and recorded source revisions were not changed.

The cache note and derived `reference.release_*` fields in both committed reports
were corrected with the §20.2 interpretation in this pull request. Timing samples,
corpus identity, environment data and recorded source revisions were not changed.

### Running a lane

```
OMNIVIA_ENGINEERING_QUALIFICATION=1 OMNIVIA_QUALIFICATION_CORPUS=100000 \
OMNIVIA_QUALIFICATION_STORAGE_CLASS=local-ssd \
  python -m pytest packages/omnivia-core-runtime/tests/phase3/runtime/test_engineering_qualification.py \
  -k performance_qualification_lane -s
```

One lane per invocation; the report is `benchmarks/reports/engineering-memory/lane-<corpus>.json` (override the directory with `OMNIVIA_QUALIFICATION_REPORT_DIR`). Optional inputs, all `OMNIVIA_QUALIFICATION_*`: `WORKTREES` (3), `CONFLICT_GROUPS` (corpus/1000, 2–200), `SEARCH_SAMPLES` (100), `PACK_SAMPLES` (30), `CHECKPOINT_SAMPLES` (100 per size class), `COLD_SAMPLES` (10 per operation), `READERS` (4), `READER_REQUESTS` (25 each), `WRITER_CHECKPOINTS` (25). `STORAGE_CLASS` is the operator's declaration: a storage class cannot be detected safely from the standard library, so an undeclared run reports `undeclared` and is not release-eligible.

### Fixture (corpus generator `engineering-memory-qualification-corpus` v2)

- **Worktrees.** Real Git worktrees of one logical repository (default 3), each registered through `engineering.repository.register`, sealed by the production `capture_working_tree_snapshot_owned` and committed through `engineering.source.capture.commit`. Fixed Git dates make the sealed manifests reproducible. The corpus is placed `(index // 2) % worktrees`. A record is `matched` only at a target in its own worktree's source stream, so `current_safe` requests target a worktree holding a member of the queried bucket.
- **ACL partitions.** Every tenth non-conflict record is evidence-backed by an open artifact; the rest carry the owner-held `group.engineering` label. The restricted-reader lane asserts every returned record is in the open set. Conflict members carry no evidence and are open to every reader.
- **Long code spans.** Every fourth non-conflict record has 1 900-character code in both `summary` and `what`. (A `source_span` dependency would make the record's applicability `unknown`, so spans are carried in content, not dependencies.)
- **Conflict groups.** Pairs of observations sharing a topic key and a worktree, seeded first so their discovery runs are the oldest queued. The production `EngineeringConflictExecutor` drains exactly those runs; the report records the discovery backlog before and after, and that every group produced exactly one relation. No semantic assessment provider runs, so each group is a structural `unresolved_overlap` and never an assessed material conflict; the conflict-group context-build lane asserts the group is visible, atomic and names exactly its two records. The rest of the discovery backlog is deliberately not drained.
- **Checkpoint payloads.** Short (~120 B), medium (16 KiB) and near-limit (cap − 8 KiB = 253 952 canonical bytes of the 262 144-byte cap), sized with the production canonicalizer and interleaved on one session.

### Report contract (`engineering-memory-qualification/2`)

`validate_report` fails a report missing any of these; it never inspects a latency value.

| Block | Contents |
|---|---|
| `run` | start/finish timestamps, seed seconds |
| `environment` | `cpu` (model, logical and physical count), `memory.physical_bytes`, `storage` (class and its source), `os` (name, version, build, architecture), `runtimes` (Python, SQLite, Core runtime and contract versions) |
| `source` | exact `commit`, `branch`, `dirty`, `dirty_path_count`, `dirty_digest` (status, tracked diff and untracked bytes), or an explicit `unavailable` reason |
| `migration`, `database` | applied head number and name; journal mode, synchronous, page and cache size and other connection PRAGMAs |
| `corpus` | generator, version, observation count, `digest` over every record's canonical form, record kinds, `acl`, `code_spans`, `source_snapshots` (snapshot digest; per worktree: checkout id, snapshot, stream, sequence, manifest digest, coverage, observation count), `conflict_groups` (count, method, discovery counts), `checkpoint_payloads` |
| `policy`, `effective_context_budget` | digest and snapshot of the production limits in force (candidate cap, budgets, pack and projection versions, discovery budgets, checkpoint cap, authorization partition); the effective 4 000-token / 16 KiB budget |
| `cache`, `concurrency`, `sampling` | the cold and warm procedures with what each does and does not control; the declared concurrent workload; every sample count |
| `lanes.cold`, `lanes.warm` | per operation: n, min, p50, p95, p99, max, mean (ms); checkpoint classes add `payload_bytes`; each lane also carries timestamps and `resources` |
| `lanes.concurrent` | declared workload, total requests, wall seconds, throughput, and per operation end-to-end, service and gate-wait percentiles |
| `resources` | process peak RSS, CPU seconds, database bytes at each lane boundary; unavailable fields are `null` with a stated reason |
| `reference` | the reference profile, the warm-target comparison and `release_blockers` |

Operations in every lane: `engineering.search`, `.search.current_safe`, `.search.acl_partitioned_reader`, `engineering.context.build`, `.context.build.current_safe`, `.context.build.conflict_groups`, and `continuity.checkpoint.append.{short,medium,near_limit}`.

### What the lanes control, and what they do not

- **Cold is SQLite connection/page-cache cold, not system-cold.** Before every cold sample the workspace connection is closed and the workspace adopted again, as a service restart does: a fresh SQLite connection, page cache, prepared statements and dispatcher; the operation then runs once with no warm-up. The operating-system page cache, CPU caches and process-level Python caches are not controlled.
- **Warm** is five discarded requests per operation on one connection, then the samples on that connection.
- **Concurrent** is four reader threads cycling every read operation while one writer appends checkpoints. Every request goes through the same single SQLite gate the production socket and HTTP transports hold around dispatch, so requests queue exactly as production requests do; the lane measures end-to-end latency under bounded client load, not parallel execution inside Core.
- **Resources** are standard-library only: `ru_maxrss` is the *process* high-water mark (the whole pytest run so far), current RSS exists only on Linux, and no working-set figure is captured.
- **Reference targets** (preview search p95 ≤ 300 ms, 4k-token / 16 KiB context build p95 ≤ 1 000 ms, checkpoint commit p95 ≤ 200 ms) are compared against the warm lane's worst p95 over the listed operations. `within_target` is advisory unless `release_blockers` is empty. The blockers are: corpus below 100 000, fewer than 4 logical cores, under 16 GiB RAM, storage not declared `local-ssd`, and an unknown or dirty source.

## Migration / rollout evidence

- Compatibility check + pre-migration backup exercised by the restore vertical; projections rebuild from authoritative records (`rebuild_missing_previews` maintenance path).
- Older clients: v1 operations unchanged; engineering fields never injected into v1 results (contract freeze tests + wire corpus).
- Rollback: feature flags disable exposure; committed evidence remains (§22.3). Workspace-format compatibility is pinned by the manifest `CoreCompatibility` block.

## Known exclusions (release-note language)

1. Context packs do not yet emit known-conflict warnings. Conflict discovery and governed reconciliation remain incomplete; no semantic assessor (P2-08) is enabled.
2. Core's bounded local polling producer advances registered checkout streams, recovers sealed captures and uses a durable pending-work queue with restart-persistent fairness and history-independent next-eligible lookup. Platform filesystem notifications and Dev semantic/index adapters are outside Core.
3. Single-principal Personal mode; lease/binding-generation fencing recorded but not enforced.
4. Tokenizer contract remains incomplete: the current engineering renderer reports a deterministic pattern count, explicitly labelled as not a host-model tokenizer. Exact supported-tokenizer counting or an explicitly negotiated byte-only representation is required before claiming §12.4 conformance.
