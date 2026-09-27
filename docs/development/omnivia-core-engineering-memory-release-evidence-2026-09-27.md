# OmniVia Core — Engineering Memory release evidence manifest (§22.5)

**Date:** 2026-09-27 · **Contract version:** `x-omnivia-contract-version: 1.3` · **Migration head:** 0053 · **Branch:** `main`

One row per shipped capability. "Producer" is the code path that writes authoritative state; "consumer" is the code path that reads it. A row with no production writer and consumer would not be listed. Test evidence names real test files; OS evidence is the CI platform matrix each PR ran (ubuntu/macos/windows lanes where listed).

| Capability | Producer | Consumer | Persistence | Test evidence | OS evidence | Exclusions / notes |
|---|---|---|---|---|---|---|
| Engineering observation ingestion | `handlers/memory.py` `create_memory_record` + engineering content-profile validator (`memory.create`, `engineering.observation` profile) | `memory.get` / `memory.list` / `memory.search`, `engineering.search` previews | `omnivia_governed_records` + evidence links (migration 0049 families) | `test_engineering_source_coverage.py`, `test_engineering_applicability.py`, `test_engineering_retrieval.py` | Phase 2 qualification lanes ×3 platforms | Proposed-only entry; no accepted state in input (EM-09) |
| Repository / checkout / snapshot identity | `handlers/engineering.py::engineering_source_record` + `storage/repository_identity.py` | `engineering.search` (repository targets), `engineering.context.build` (targets), pack applicability | migration 0047 (`omnivia_engineering_repositories`, `_snapshots`) | `test_engineering_repository_identity.py` | Core acceptance matrix | Registration is a trusted-producer operation; no model-facing registration tool (deliberate, §16.3) |
| Source streams and dependency manifests | `engineering.source.record` writer + `storage/engineering_source.py` | `current_safe` search/pack applicability resolution | migrations 0050–0052 (streams, events, dependencies, dependency sets) | `test_engineering_source_coverage.py`, `test_engineering_dependency_carry.py`, `test_engineering_dependency_lookup.py` | Core acceptance matrix | No external `RepositoryChangeSet` producer yet; coverage advances only on recorded source events |
| Continuity sessions and checkpoints | `handlers/continuity.py` (register / append / close) | `continuity.handoff.read`, `engineering.search` `working_context` view, resume packs | migration 0048 (`omnivia_engineering_sessions`, `_checkpoints`) | `test_engineering_continuity.py`, `test_engineering_cli.py` | Core acceptance matrix + CLI vertical (macOS local; CLI surface covered by CI matrix) | Lease enforcement recorded-not-enforced (§7.3); single-principal (§19.4) |
| Progressive retrieval (preview / expand) | `handlers/engineering.py` search + `storage/engineering_preview.py` projection (migration 0053) | MCP `engineering_search` / `engineering_expand`, CLI `engineering search` / `engineering expand` | Preview projection (rebuildable) | `test_engineering_retrieval.py`, `test_engineering_preview_projection.py` | Evidence search platform qualification ×3 | No vector lane; lexical + structural only (§13.1) |
| Engineering context pack | `handlers/engineering.py::engineering_context_build` | MCP `engineering_context_build`, CLI `engineering context` | None (non-persisting read, §12.7) | `test_engineering_context_build.py`, `test_engineering_pack_render.py` | Core acceptance matrix | Historical diagnosis mode is a separate negotiated capability, not enabled |
| Context priority | `context.priority.set` audited upsert | search/pack selection boost (capped 10%, §13.3) | principal-scoped preference rows | `test_engineering_applicability.py` (priority sections) | Core acceptance matrix | Never changes governed state (EM-19) |
| Review and applicability attestation | `engineering.review.record` | applicability projection served to search/packs | assessments + attestations (migration 0049) | `test_engineering_applicability.py` | Core acceptance matrix | Review acknowledgement cannot clear stale/unknown applicability (§15.5) |
| MCP exposure | `packages/omnivia-core-mcp` manifest (restricted + authoring profiles) | installed MCP clients | n/a (exposure only) | `test_mcp_stdio_end_to_end.py`, `test_mcp_architecture_gates.py` | Core acceptance matrix | Curate profile not granted to agents by default (§16.4) |
| CLI surface | `packages/omnivia-core-cli` `surface.py` application commands | installed `omnivia` entry point | n/a (client) | `test_engineering_cli.py`, `test_v06_6_surface.py` bijection | CLI suite in Core acceptance | Mutations require explicit idempotency keys; preconditions via `--record-version` |
| Backup / restore of engineering state | `storage/backup.py` verified backup + restore | workspace restore flow | byte-for-byte restore | `test_engineering_restore.py` | Core acceptance matrix | Backup requires a quiesced writer (online-backup API blocks under a held lease) |

## Performance qualification

Reports: `benchmarks/reports/engineering-memory/lane-10000.json`, `lane-100000.json` (machine, corpus seed, sample counts and p50/p95/p99 recorded per operation inside each report). These are §20.2 qualification *measurements on the producing machine* — labelled as measurements, not guarantees. Targets (p95 preview ≤ 300 ms, p95 4k-token pack ≤ 1 s, p95 checkpoint commit ≤ 200 ms) are compared against the 100k lane in the consolidated status note.

## Migration / rollout evidence

- Compatibility check + pre-migration backup exercised by the restore vertical; projections rebuild from authoritative records (`rebuild_missing_previews` maintenance path).
- Older clients: v1 operations unchanged; engineering fields never injected into v1 results (contract freeze tests + wire corpus).
- Rollback: feature flags disable exposure; committed evidence remains (§22.3). Workspace-format compatibility is pinned by the manifest `CoreCompatibility` block.

## Known exclusions (release-note language)

1. No semantic assessor (P2-08): conflict relation candidates stay structural/lexical; governance remains human.
2. No automatic repository change-event producer: `current_safe` applicability advances only over recorded source streams.
3. Single-principal Personal mode; lease/binding-generation fencing recorded but not enforced.
4. Tokenizer contract: the pinned tokenizer is the one configured server-side; `tokenizer_unavailable` is returned rather than a heuristic count.
