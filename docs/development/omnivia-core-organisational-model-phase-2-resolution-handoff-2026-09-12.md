# OmniVia Core Organisational Model Phase 2 Resolution Handoff

Date: 2026-09-12
Status: Resolution required before unconditional Phase 2 acceptance
Specification authority: `SPEC-CORE-SEM-001`, version 0.2, dated 2026-09-11
Branch: `codex/core-organisational-model-v02`
Implementation tip: `5a7833ac3849700988bfa3a116e4c45890142e97`
Acceptance-handoff commit: `55da9b3`

## 1. Purpose

Resolve the remaining technical interpretations and external acceptance gates for
the Phase 2 organisational-model implementation. This document is a decision and
execution handoff; it does not itself grant acceptance, publish a release, open a
pull request or authorise optional workers.

The local implementation and full preflight are complete. The implementation
under review ends at:

```text
5a7833a feat(runtime): add phase 2 retention events and recovery
```

The acceptance handoff is:

```text
55da9b3 docs(core): add phase 2 acceptance handoff
```

Phase 2 implementation range:

```text
3c7024b..5a7833a
```

## 2. Required resolutions

### R1 — Event-name representation

Current state:

- The specification presents event names in PascalCase, for example
  `SemanticCandidateCreated.v1`.
- The existing Phase 1 outbox schema requires lowercase dotted event kinds.
- Phase 2 therefore persists names such as `semantic.candidate.created.v1`.
- Event payloads remain versioned, content-free and fail closed for unsupported
  versions.

Recommended decision:

**Accept the lowercase dotted form as the canonical wire representation and add
an explicit normative mapping to the Phase 2 decision record.** This preserves
the already-established outbox vocabulary and avoids rebuilding a populated
append-only table solely for display-name casing.

Required output:

1. Record whether the specification names are logical labels or literal wire
   values.
2. If logical, add a complete logical-to-wire mapping and a contract test.
3. If literal, add a forward migration that safely rebuilds the outbox constraint,
   preserves all rows and dispatch acknowledgements, and proves restore and
   replay compatibility.

Decision owner: Architecture, with runtime/storage concurrence.

Closure evidence:

- An approved ADR/decision-record amendment.
- Event compatibility and unsupported-version tests.
- `git diff --check`, Ruff, strict mypy and affected runtime tests pass.

### R2 — Persistence of temporal source metadata

Current state:

- `TemporalInstant` preserves `original_source_text` and `source_timezone` in the
  public contract.
- Canonical SQLite rows preserve the resolved UTC value, declared precision and
  provenance.
- The auxiliary original-text and trusted-timezone fields are not independently
  projected for every persisted temporal boundary.

Recommended decision:

**Persist the auxiliary fields for every source-derived temporal boundary before
unconditional Phase 2 acceptance.** The specification says original timezone-less
source text must be preserved when structured precision is reduced. Relying on a
nearby observation string is less explicit and is insufficient for every evidence
and assertion path.

Recommended implementation:

1. Add the next ordered forward migration; do not edit an already accepted or
   deployed migration.
2. Add nullable original-source-text and source-timezone fields for source-derived
   evidence, observation and assertion temporal boundaries.
3. Enforce valid null combinations and size limits.
4. Extend typed repository reads/writes so a store/read round trip reproduces the
   complete `TemporalInstant`.
5. Extend digest or binding verification if these fields are intended to affect
   semantic identity; otherwise explicitly document why they are preserved but
   excluded from semantic digests.
6. Add backup/restore and Python/SQLite/API conformance fixtures.

Decision owner: Architecture and runtime/storage, with security/privacy review of
source-text classification.

Closure evidence:

- Round-trip fixtures for explicit offset, trusted IANA timezone and timezone-less
  sub-day text reduced to day precision.
- Restored records preserve the same original text, timezone, UTC value,
  precision and provenance.
- No general event or error exposes the source text.

### R3 — Caller-scoped command idempotency

Current state:

- The repository already has a governed mutation/idempotency seam.
- Phase 2 canonical records use immutable IDs, content digests and deterministic
  candidate/change-set identities.
- The new Phase 2 service is currently an in-process seam and has no new HTTP,
  MCP or CLI exposure.
- A future adapter could violate the specification if it calls the service while
  bypassing the repository-wide caller-scoped idempotency mechanism.

Recommended decision:

**Require every externally exposed Phase 2 mutation to enter through the existing
governed mutation/idempotency seam. Do not build a parallel semantic-only
idempotency store.** Keep transport exposure disabled until the adapter tests
prove this composition.

Required implementation before public exposure:

1. Define canonical request and result documents for each mutating Phase 2
   operation.
2. Bind caller, workspace, operation and idempotency key to the canonical request
   digest through the existing seam.
3. Return the stored result for an exact replay.
4. Return a stable conflict when the same key is reused for different request
   bytes.
5. Prove a retry cannot append a second record or outbox event.
6. Prove authority and fencing are still rechecked on the first canonical write.

Decision owner: Runtime/transport architecture and security/privacy.

Closure evidence:

- Replay, conflicting-key, concurrent-retry and rollback tests for every mutation
  family.
- No public adapter route exists that bypasses the seam.

## 3. External acceptance gates

After R1–R3 are resolved, complete these gates in order:

1. Confirm the Phase 1 acceptance record is a formal `GO` and its hosted check is
   green. If it is not, finish that acceptance first.
2. Rebase or merge the reviewed Phase 2 range onto the accepted integration base
   without rewriting accepted Phase 1 commits.
3. Run the complete local `./scripts/preflight` on the final review tip.
4. Push the reviewed branch and open the Phase 2 pull request.
5. Obtain a green hosted `Core acceptance` result for the exact commit range.
6. Obtain architecture sign-off.
7. Obtain runtime/storage sign-off on migrations, fencing and rollback.
8. Obtain security/privacy sign-off on content separation, temporal source text,
   permissions, events and retention.
9. Obtain product-owner approval of the bounded Phase 2 behavior.
10. Record the final decision and merge only the accepted range.

None of these external approvals are claimed by the current local handoff.

## 4. Work that remains outside Core

The following capabilities are deliberately removable integrations. They should
receive separate implementation tasks and must not receive canonical database
credentials:

- Protected-content/blob storage and policy-compliant backup.
- Execution of deletion plans across blobs, projections, caches, logs and backup
  generations.
- Search, graph and vector projection rebuilders.
- Extraction, ontology, reasoner, validation and LLM workers.
- HTTP, MCP and CLI adapters for the Phase 2 service.

These integrations consume versioned Core contracts. They do not gain approval,
publication, activation or direct canonical mutation authority.

## 5. Resolution sequence

```text
Confirm Phase 1 GO
  -> decide R1 event representation
  -> implement and approve R2 temporal persistence
  -> compose and test R3 transport idempotency
  -> focused verification
  -> full local preflight
  -> hosted Core acceptance
  -> formal sign-offs
  -> merge
  -> separately schedule removable integrations
```

R1 is primarily a decision/documentation task if the recommendation is accepted.
R2 is a Core schema and repository task. R3 is a runtime/transport composition
task and may be completed with transport exposure still disabled.

## 6. Acceptance requirements for the resolution patch

The resolution work is complete only when:

- The R1 wire-name decision is explicit and executable tests match it.
- Complete source-derived `TemporalInstant` metadata survives SQLite
  store/read/backup/restore paths.
- Every exposed Phase 2 mutation is caller-scoped and idempotent through the
  existing governed seam.
- Sensitive source text remains absent from general errors and outbox payloads.
- Existing fence, workspace, append-only and human-governance guarantees remain
  unchanged.
- Focused Phase 2 suites, migration tests, Ruff, strict mypy and
  `git diff --check` pass.
- Complete local preflight and hosted `Core acceptance` pass on the same reviewed
  commit.
- The acceptance handoff is updated with the final range and signed decisions.

## 7. Decision record

| Resolution | Recommended decision | Final decision | Owner | Evidence/commit |
|---|---|---|---|---|
| R1 event names | Accept lowercase dotted wire names with normative mapping | Pending | Architecture + runtime/storage |  |
| R2 temporal metadata | Add explicit SQLite persistence and restore coverage | Pending | Architecture + runtime/storage + security/privacy |  |
| R3 idempotency | Compose all adapters through the existing mutation seam | Pending | Runtime/transport + security/privacy |  |

## 8. Sign-off record

| Gate | Decision | Reviewer | Date | Notes |
|---|---|---|---|---|
| Phase 1 prerequisite | Pending confirmation |  |  |  |
| Architecture | Pending |  |  |  |
| Runtime/storage | Pending |  |  |  |
| Security/privacy | Pending |  |  |  |
| Product owner | Pending |  |  |  |
| Local preflight after resolution | Pending |  |  |  |
| Hosted Core acceptance | Pending |  |  |  |
| Merge | Pending |  |  |  |

## 9. Source handoffs

- `omnivia-core-organisational-model-phase-2-acceptance-handoff-2026-09-12.md`
- `omnivia-core-semantic-registry-phase-2-decision-record-2026-09-12.md`
- `omnivia-core-organisational-model-phase-1-closeout-and-phase-2-implementation-plan-2026-09-12.md`
- `SPEC-CORE-SEM-001` version 0.2 source document.
