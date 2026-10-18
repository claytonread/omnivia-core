# Engineering-memory completion plan — from the merged state to release

| Field | Value |
|---|---|
| Date | 2026-09-26 |
| Baseline | `main` @ `ee9a4ffd` (PR #128 merged: PR-C/D/E/G/F); branch `codex/core-engineering-mcp-exposure` carries the PR-H MCP exposure amendment (uncommitted) |
| Completes | SPEC-CORE-ENGMEM-001 implementation through PR-H; P2-08 remains gated on owner decisions per plan |
| Status | Plan for owner/Codex review |

## Phase 0 — Merge the MCP exposure slice (PR-H1) — ~2h wall-clock, mostly CI

The MCP amendment is implemented; one test pin remains.

1. **Fix the refusal-skip in `test_mcp_architecture_gates.py`.** The file has two `DECISION_OUTCOMES` loops; the first now skips `_without` for refusal outcomes (structured content is None on both lanes), the second still indexes `PRINCIPAL_FACTS[tool_name]` directly and raises `KeyError: 'continuity_checkpoint_append'`. Apply the same skip-and-continue to the second loop.
2. **Full local sweep**: contracts + compatibility, service_conformance, phase2, all five engineering test files (16+3+4+7 = 30 tests), the full MCP suite, ruff, strict mypy, migration gate.
3. **Commit, push** `codex/core-engineering-mcp-exposure`, open the PR.
4. **CI**: `Core acceptance` (~55 min) + three platform jobs. Known hosted flake family: the standard-candidate journey's `initialize` stage — rerun the failed job once before investigating (documented in #127/#128).
5. **Merge on green.**

Deliverable: all nine operations exposed through the curated MCP surface; manifest 2.1; PR bodies carry the release-note limitations.

## Phase 1 — PR-H2: consumer proof and release evidence — ~2–3 sessions

1. **CLI vertical proof** (decision PR-7 pattern): drive the engineering commands through the installed `omnivia` entry point against a managed-start service — register, append, close, search, build — asserting the same behaviours the service tests prove, over the real transport.
2. **Migration/restore evidence**: backup the workspace with engineering tables populated, restore into a fresh installation, assert continuity sessions, checkpoints and observations survive with their digests.
3. **Performance qualification lanes** (§20.2): synthetic corpora at 10k and 100k observations with realistic code-source spans, ACL partitions and conflict groups; record p95 preview search, p95 4k-token pack build, p95 checkpoint commit with hardware/corpus details; targets stay labelled *targets* until measured.
4. **§22.5 release evidence manifest**: per-capability producer/consumer map, test evidence, supported OS combinations, known exclusions.
5. **Consolidated status doc**: one `docs/development/` record covering PR-A through PR-H closeout (the current plan doc stops at PR-A).

## Phase 2 — Owner-side gates (not agent-reachable)

The resolution handoff (`docs/development/omnivia-core-decision-runtime-gates-g2-g3-resolution-handoff-2026-09-26.md`) is written. Remaining:

1. **G-2**: Laya publishes a pinnable distribution (manifest + artifacts + digests; the handoff §4-G-2 lists the four requirements). Long pole; nothing else waits on it except P2-08.
2. **G-3**: owner rules G-3a–d (key holder, custody/ceremony, anchor publication venue, rotation policy) — the handoff's decision table is the agenda.
3. Record both resolutions in a status doc; allocate the anchor publication venue per preflight precedent (repository-external).

## Phase 3 — PR-P2-08: semantic assessor — after Phase 2 + provider/security approval

Only after the gates: the assessor adapter sends a bounded record pair to the local model (PR-4/5's installed model) and proposes a candidate relation verdict. Everything else is already built: conflict discovery, relation vocabulary, governed resolution, and the disabled-state refusals AC-051/052 pin.

Optional pre-build while gated: the model-manifest verification mechanism on the existing runtime-payload trust format, with a dev/test anchor behind a flag, shipped inert (see the handoff §4-G-3 mechanism half).

## Phase 4 — recorded-limitation closure candidates (later PRs, not release-blocking)

Each is a real gap, documented in PR bodies and the release note; each is one focused PR:

| Limitation | Closing PR |
|---|---|
| Continuity lease expiry / binding-generation enforcement (no refresh op) | continuity refresh operation + enforcement tests |
| Change-event producer + dependency-invalidation worker (PR-E worker half) | the applicability worker's producer side |
| Snapshot capture producer (git/working-tree capture) | the capture vertical feeding 0047 tables |
| Repository registration operation ratification (§16.3) | one catalogue amendment |
| Depth-2+ expand traversal | bounded multi-hop expansion |

## Risks

1. **Stdio e2e surface pins** — the remaining MCP-suite failures after the refusal fix are count/order pins in the same category as everything fixed so far; budget one sweep-and-fix cycle.
2. **Hosted journey flake** — the standard-candidate `initialize` stage fails intermittently on all three platforms; rerun the failed job once before investigating (documented on #127/#128).
3. **Strict mypy** — new code must import from owning modules (two implicit-re-export catches so far); run `mypy --strict` over all five packages, not just `src/omnivia_core`.
4. **Performance lanes are new infrastructure** — the synthetic-corpus builder is its own deliverable; scope it as such rather than inlining into PR-H2.

## Definition of "complete"

Per §24.4: a capability is complete when its contract, producer, authoritative persistence, consumer, negative tests, recovery tests, compatibility evidence and operating documentation all exist. On that standard:

- **Complete at Phase 1 close**: all nine operations, end to end, through service + CLI + MCP, with release evidence — the engineering-memory release ships.
- **Phase 2 + 3 complete the P2-08 assessor only**, which was always the optional, least-trusted, last-shipped component.
