# OmniVia Core Organisational Model Phase 2 Resolved Handoff

Date: 2026-09-12
Status: Local resolution implementation complete; external acceptance pending
Specification authority: `SPEC-CORE-SEM-001`, version 0.2, dated 2026-09-11
Branch: `codex/core-organisational-model-v02`

## 1. Purpose

This handoff records the implementation evidence for the three adopted Phase 2
resolutions. It does not grant architecture, runtime/storage, security/privacy or
product approval; it does not substitute for hosted Core acceptance; and it does
not authorise a public mutation adapter.

## 2. Implemented resolutions

### R1 — Logical event labels and canonical wire values

- Section 19 PascalCase values are logical specification labels.
- `omnivia_core_runtime.storage.semantic_events` owns the explicit logical-to-wire
  and reverse mappings.
- Every canonical wire value follows
  `semantic.<lowercase dotted tokens>.v<positive integer>`.
- Current Phase 1 and Phase 2 producers import their values from the registry.
- Publication now emits `semantic.version.published.v1` for new events. Existing
  immutable outbox rows are not rewritten.
- The reviewed Phase 2 extension `SemanticCandidateReconsidered.v1` is registered
  explicitly outside the initial Section 19 set.
- Contract tests prove exhaustive Section 19 coverage, one-to-one mapping,
  producer binding, grammar compliance and fail-closed aliases/versions.

### R2 — Complete source temporal metadata

- Forward migration `0040_semantic_temporal_source_metadata.sql` adds adjacent
  source-text and trusted-timezone columns without editing semantic migrations 0037–0039.
- Evidence and observation source times and assertion valid/attested boundaries
  round-trip complete `TemporalInstant` values.
- Original source text is limited to 2,048 Unicode characters. Trusted timezone
  metadata is limited to 255 characters and validated as UTC, a numeric offset or
  an IANA identifier by the typed contract.
- Explicit `Z` and numeric offsets are retained as trusted timezone metadata.
- A timezone-less sub-day value preserves its exact text, retains no timezone and
  is reduced to day precision rather than restoring or guessing a clock offset.
- SQLite enforces storage null relationships and length limits. Existing
  append-only and writer-fence triggers remain authoritative.
- Auxiliary source metadata participates in evidence, observation and assertion
  record digests when present. Null auxiliary fields are omitted from digest
  payloads so historical digest bytes remain stable.
- The metadata remains outside Semantic Model content digests, general errors and
  content-free outbox payloads. It is returned through existing permission-checked
  detail reads and inherits the containing record classification and retention.
- Backup/restore fixtures include evidence, observation and assertion source
  metadata and compare restored rows exactly.

### R3 — Caller-scoped idempotency at the future adapter boundary

- The six mutating in-process Phase 2 operations are explicitly inventoried.
- The external mutation-adapter inventory is empty.
- An executable source-root scan proves that CLI, client, MCP and other runtime
  service modules do not couple to `SemanticPhase2Service`.
- No transport adapter or semantic-only idempotency store was added.
- The first adapter-enablement change remains required to compose each mutation
  through the existing governed caller/workspace/operation/idempotency-key seam and
  supply replay, conflict, concurrency and rollback evidence.

## 3. Verification evidence

Completed on the resolution working tree:

```text
Ruff over src, packages and tests: passed
Strict mypy over Core and Core Runtime: passed (209 source files)
Semantic Registry domain/runtime suites: 572 passed
git diff --check: passed
```

Complete local `./scripts/preflight`: passed. Evidence included:

```text
Package boundaries: 51 tests passed
Application contracts: 9,255 passed, 19 skipped
Canonical migration and compatibility: 1,686 passed, 2 skipped
Phase 0 baseline: 749 passed
Full repository suite: 21,229 passed, 32 skipped
Benchmarks: 30 passed
Ruff: passed
Strict mypy: 274 source files passed
macOS status menu companion: 48 tests passed
All five Python distributions built and passed isolated install/import checks
```

## 4. Acceptance still required

1. Confirm the Phase 1 formal `GO` and hosted prerequisite.
2. Review and accept the final Phase 2 resolution commit range.
3. Run hosted `Core acceptance` on the exact reviewed tip.
4. Obtain architecture and runtime/storage sign-off.
5. Obtain security/privacy sign-off for classified source text and access paths.
6. Obtain product-owner approval of the bounded Phase 2 behavior.
7. Merge only after the required hosted checks and sign-offs are green.

## 5. Work deliberately outside Core

Protected blob storage, deletion executors, projections, validation/extraction/
reasoner/LLM workers and HTTP/MCP/CLI adapters remain removable integrations. They
receive versioned Core contracts and no canonical database credentials, publication
authority or activation authority.

## 6. Rollback posture

- Semantic migrations 0037–0039 and prior rows are unchanged.
- Rollback of temporal persistence restores the verified pre-0040 database and the
  matching pre-resolution application version.
- Existing immutable outbox values are never rewritten; the versioned publication
  value applies to newly emitted events.
