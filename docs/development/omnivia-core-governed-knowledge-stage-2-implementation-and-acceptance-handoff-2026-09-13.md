---
title: "OmniVia Core governed knowledge: Stage 2 implementation and acceptance handoff"
date: "2026-09-13"
status: "Implemented; local preflight passed; acceptance and activation pending"
specification: "SPEC-CORE-KNOWLEDGE-IMPROVEMENT-001 v0.1"
baseline: "7dccb9f8f10e1b26abc675152ba615eb86232c01"
implementation: "2ba08040f286bd14a6fc81a263d6a79b6f4af230"
---

# Scope and decision

The bounded, portable Stage 2 assisted-diagnosis and candidate-evaluation
foundation is implemented. The implementation is ready for review against the
architecture, runtime/storage, security/privacy, product-owner and consumer-owner
gates. This handoff is implementation evidence, not approval, release, or host
activation.

- Repository: `claytonread/omnivia-core`
- Branch: `codex/core-governed-knowledge-stage2`
- Exact accepted Stage 1 base: `7dccb9f8f10e1b26abc675152ba615eb86232c01`
- Exact locally verified implementation: `2ba08040f286bd14a6fc81a263d6a79b6f4af230`
- Migration identifiers: none
- New application operations: none
- New CLI commands: none
- New MCP tools or mutations: none
- Overall decision: `CONDITIONAL GO` for review; `NO RELEASE/ACTIVATION`
  until the named gates and host qualification conditions are satisfied

# Delivered implementation

## Bounded assisted diagnosis

`governed_knowledge.assisted` provides an injected worker boundary with exact
executor, build, content, runtime-profile, policy, isolation, lineage and
provider/model observations. The binding is evidence, not a permission grant.

The worker can return only bounded issue records, diagnoses, a pending proposal,
and non-negative token/cost observations. Unknown fields, action/tool/permission/
approval claims, malformed output, oversized combined content, workspace or
feedback mismatch, and non-pending proposals are refused. Cancellation, timeout,
declared worker failure, malformed output and unexpected provider/runtime errors
produce safe inert terminal attempts. They never delete the original feedback or
create a proposal on failure.

## Evaluation-only candidate overlays

`governed_knowledge.evaluation` provides immutable overlays pinned to workspace,
purpose, baseline/candidate digests, candidate/context refs, creation/expiry,
classification and retention. An overlay cannot enter live selection or carry a
permission grant, and it is refused when expired, not yet usable, cross-workspace,
or digest-mismatched.

Evaluation cases keep consumer-visible inputs separate from expected outcomes and
rubrics. Deterministic checks execute before the injected evaluator and cannot be
overridden by a behavioural pass. Behavioural pass/fail results require retained
output evidence; infrastructure outcomes require safe error codes. Unexpected
evaluator errors are sanitized.

## Complete pilot and report aggregation

The stable renewal pilot contains exactly `PC-01` through `PC-12`. The critical
set is `PC-02`, `PC-03`, `PC-09`, `PC-10`, `PC-11`, and `PC-12`; those cases and
the triggering scenario require three predeclared attempts. Aggregation retains
every ordered attempt, rejects missing or non-evaluation worker bindings, records
mutable model aliases as limitations, totals observed tokens/cost, and emits an
OV-CJ-1/SHA-256-bound report.

Reports can be only `eligible_for_review`, `blocked`, or `inconclusive`. They do
not approve, admit, activate, mutate, or select candidate knowledge. A later pass
cannot hide an earlier failure, and a feedback-derived case remains inconclusive
until owner reviewed.

## Versioned portable content

`governed_knowledge.stage2_content` supplies strict exact-field and exact-version
codecs for worker bindings, diagnosis attempts, candidate overlays, evaluation
cases, suites, attempts and reports. The public governed-knowledge package exports
the Stage 2 values and coordinators. No provider SDK, credential, database,
filesystem, network handle or production-effect tool is introduced into Core.

# Conformance evidence

| Specification cases | Implemented evidence |
|---|---|
| KI-T27–KI-T33 | Multiple bounded issues, exact feedback spans, distinct diagnosis owners/categories, timeline/access/disagreement evidence and lineage tests in `test_stage2_assisted.py` |
| KI-T39 | Failure, timeout, cancellation, malformed output and unexpected exceptions preserve independent feedback and create no proposal |
| KI-T43 | Evaluation-only, expiring, digest/workspace-bound overlay with no live-selection or permission route |
| KI-T44 | Exact worker result shapes and rejection of effect/tool/authority fields; injected protocols receive no production handles |
| KI-T45 | Expected outcome and rubric refs cannot appear in consumer-visible inputs |
| KI-T46 | Deterministic failure blocks before worker invocation and cannot be replaced by a pass |
| KI-T47 | Required non-results block the aggregate report |
| KI-T48 | Every retry is retained; later passes do not hide an earlier failure |
| KI-T49 | Mutable provider/model aliases are recorded as report limitations |
| KI-T50 | Feedback-derived proposed cases remain inconclusive pending owner review |

# Verification record

Focused verification on exact implementation commit
`2ba08040f286bd14a6fc81a263d6a79b6f4af230`:

```text
.venv/bin/python -m pytest tests/governed_knowledge packages/omnivia-core-client/tests/test_package_isolation.py -q
164 passed

.venv/bin/ruff check src/omnivia_core/governed_knowledge tests/governed_knowledge
All checks passed

MYPYPATH=src .venv/bin/mypy --strict src/omnivia_core/governed_knowledge
Success: no issues found in 16 source files
```

Complete local acceptance:

```text
./scripts/preflight
Package boundaries: passed; 53 boundary tests passed
Five wheel builds, isolated installs and import checks: passed
Application Contract tests: 9,832 passed, 19 skipped
TypeScript strict parity: passed
Canonical migration and compatibility tests: 1,686 passed, 2 skipped
Compatibility root resolver and installed-root smoke: passed
Phase 0 baseline: six drift checks passed; 749 tests passed
Full repository tests: 24,546 passed, 35 skipped, 7 deprecation warnings
Benchmarks: 23 passed
Ruff 0.16.1: passed
mypy 2.3.0 --strict: no issues in 326 source files
macOS status-menu package: build passed; 48 tests passed
Final result: Core preflight passed
```

Hosted acceptance remains pending for the exact PR head:

- `Core acceptance`;
- `Phase 2 platform (ubuntu-latest)`;
- `Phase 2 platform (macos-latest)`; and
- `Phase 2 platform (windows-latest)`.

# Gate record

| Gate | Current evidence | Decision required |
|---|---|---|
| Architecture | Provider-neutral injected protocols; portable profiles only; no new authority or application surface | Approve the Core/host ownership boundary and exact profile set |
| Runtime/storage | No migration or new store; existing governed-record content route remains the persistence option | Approve no-migration/no-new-operation posture and require host integration separately |
| Security/privacy | Fail-closed exact decoding, bounded output, sanitized errors, no effects/tools/credentials, overlay isolation | Approve portable controls; require host credential, deletion and isolation proofs before activation |
| Product owner | Exact twelve-case pilot, critical repeats and non-approval report semantics | Approve the case/rubric policy and the review-only outcome |
| Consumer owner | Consumer inputs/configuration are exact refs; no claim about an uninstrumented consumer | Approve a named host/consumer pilot before any production use |

# Explicitly not completed or enabled

This Core slice does not implement or authorize:

- a real provider/model adapter, credential route or worker registration;
- host-side runtime/job wiring, retries, scheduling or activation;
- persistence orchestration, new schemas, migrations, endpoints, CLI or MCP;
- live retrieval from a candidate overlay;
- knowledge admission, approval or production mutation;
- measured provider latency, memory, token or cost ceilings;
- deletion/restore evidence for prompts, raw output, reports and worker scratch; or
- owner-reviewed results from a real model/consumer pilot.

Those are activation work owned by the selected host and consumer. Core can be
merged while they remain disabled, but Stage 2 must not be advertised as an
active product capability until they pass.

# Tooling note

The required Claude Code write-lane launch was attempted before implementation.
The local Claude CLI reported an expired login and did not start a task or create
a nested worktree/diff. The bounded Codex fallback was therefore used, followed
by direct diff review and the complete repository preflight. This affects process
evidence only; it does not weaken the acceptance gates above.

# Rollback

Before acceptance, close the PR. After merge, revert the Stage 2 commits as a
forward change and leave host registration disabled. No database downgrade is
required. Existing Stage 2 profile-bearing governed records remain opaque,
versioned evidence; they are not admitted to live selection and are not deleted
or reinterpreted by code rollback. Candidate overlays expire by construction.

# Requested acceptance

Review the exact PR head and record one of `GO`, `CONDITIONAL GO`, or `NO-GO` for
architecture, runtime/storage, security/privacy, product owner and consumer owner.
Merge authorization must be explicit and must name the reviewed head after all
four hosted checks pass. Host activation requires a separate authorization after
the outstanding host/consumer qualification evidence is complete.
