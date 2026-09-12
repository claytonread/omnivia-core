---
title: "OmniVia Core governed knowledge: Stage 0 binding and Stage 1 implementation handoff"
date: 2026-09-13
status: "Implemented; local preflight passed; acceptance pending"
specification: "SPEC-CORE-KNOWLEDGE-IMPROVEMENT-001 v0.1"
---

# Scope and status

This record binds the governed-knowledge and expert-feedback specification to the
accepted `omnivia-core` repository baseline and records the Stage 1 implementation.
It is implementation evidence, not an acceptance or release declaration.

- Repository: `omnivia-ai/omnivia-core`
- Worktree: `worktree-omnivia-core-governed-knowledge-v01`
- Branch: `codex/core-governed-knowledge-v01`
- Exact base: `6f11be4ba7f935ac506f16b0f82ba84c45b0648a`
- Migration: none
- New application operations: none
- New MCP tools: none
- Enabled stage: Stage 1 manual, model-free foundation and reference integration
- Disabled stages: Stage 2 assisted diagnosis/evaluation, Stage 3 dynamic impact,
  Stage 4 contribution, and Stage 5 broader work views

The implementation uses profiles encoded in the existing governed-record `content`
object. Durable writes continue through `memory.create`, `knowledge.propose`,
`candidate.approve`, and `record.supersede`. `context_pack.build` remains a synchronous,
non-persisting read. The Stage 1 reference client composes its verified result with the
task profile and already-authorised exact position views and returns the selection manifest
in the same `Stage1ReferenceResult` bundle.

# Delivered capabilities

## KI-01: organisational positions and applicability

- Immutable `OrganisationalPosition` profile with exact profile version, semantic-model
  binding, evidence references, classification, retention, temporal source metadata,
  review policy, explicit exceptions, dependency/contradiction relations, and trusted
  outer lifecycle state.
- Position content cannot self-declare admission. The trusted governed-record boundary
  supplies lifecycle separately.
- Pure, deterministic, bounded applicability grammar: `all`, `any`, `not`, equality,
  finite membership, numeric/date comparisons, three-valued logic, and contested-position
  detection.
- Limits: 64 nodes, depth 8, set size 100, and 16 KiB canonical profile content.
- Numeric values use `Decimal` and matching units. Date precision is evaluated as a
  half-open interval and overlapping precision produces `needs_information`.
- Fact results preserve authorised fact references and provenance classes without naming
  hidden facts.

## KI-02 and KI-03: task context, selection, and delivery evidence

- Versioned `TaskContextProfile` with consumer, scope, fact, domain, stage, freshness,
  item, byte, priority, and response-shape constraints. It carries no credential,
  provider, model, or permission grant.
- Deterministic `assemble_stage1_context` checks workspace, purpose, caller scopes,
  consumer lifecycle/kind, semantic binding, recorded-as-of, valid-at, review freshness,
  byte/item bounds, applicability, and explicit contradictions.
- `ContextSelectionManifest` records selected versions, authority classes, source refs,
  applicability results, checkpoints, mechanisms, safe omissions, exact pack identity,
  byte accounting, policy/freshness state, recheck requirements, and an OV-CJ-1/SHA-256
  integrity digest.
- `ContextDeliveryReceipt` distinguishes Core selection from consumer-supplied context,
  records ordered transport states, segment provenance, consumer transformations,
  instructions/skills/tools, provider/model/configuration references, and instrumentation
  completeness. It never claims internal model reliance.

## KI-04 and KI-06: correction, diagnosis, proposal, and dependencies

- Immutable, model-free `ExpertFeedback`; incomplete or absent original context remains
  explicit and the complete profile is subject to the 16 KiB content limit.
- Manual `FeedbackDiagnosis` categories keep knowledge, retrieval, applicability,
  freshness, access, consumer transformation, procedure, citation, disagreement, and
  preference findings distinct.
- `KnowledgeImprovementProposal` remains pending content. It cannot carry a self-asserted
  admission state/reference and must use the existing governance route.
- `KnowledgeConsumerDependency` extends the existing consumer registry types for exact,
  logical, bounded-dynamic, observed-runtime, evidence, and procedure/profile relations.
- Every persistent Stage 1 profile has a version-checking content codec.

## Usable reference journey

`Stage1ReferenceClient` structurally accepts the public
`omnivia_core_client.ServiceClient.call` interface without introducing a Core-to-client
package dependency. It:

1. forwards only the accepted governed mutations named above;
2. calls only `context_pack.build` for context reads;
3. preserves the caller's request envelope, deadline, cancellation, workspace, purpose,
   and authority boundary;
4. verifies the returned Context Pack artifact digest;
5. applies the task profile and returns the original response plus the inline manifest.

The included client-renewal fixture proves annual/prepaid selection, monthly exclusion,
pending exclusion, missing-fact refusal, contested handling, byte-bound refusal, and
deterministic repeat output.

# Stage 0 binding register

| Binding | Current symbol/path | Status | Stage 1 decision / remaining gate |
|---|---|---|---|
| BR-01 accepted baseline | Git base `6f11be4ba7f935ac506f16b0f82ba84c45b0648a`; `.github/workflows/core-acceptance.yml` | Accepted baseline; local preflight passed at implementation commit `8665063` | Run the four required hosted checks on the PR head. |
| BR-02 pending/admitted seam | `contracts/application/v1/schemas/{memory,knowledge}.schema.json`; runtime `service/handlers/{memory,governance}.py` | Accepted | Profiles use `content`; create/propose/approve/supersede remain the only write journey. |
| BR-03 canonicalisation/integrity | `contracts/v1/canonical_json.py`; `semantics_knowledge.compute_context_pack_artifact_digest` | Accepted | Profile content uses the same canonical JSON; manifest digest removes only its two self-referential identity fields. |
| BR-04 temporal evaluator | `semantic_registry/temporal.py`; runtime governed/context-pack resolvers | Accepted | Positions reuse `TemporalInstant` and `EffectiveValidInterval`; no second time vocabulary. |
| BR-05 Context Pack | `ContextPackBuildInput/Result`; runtime `handlers/context_pack.py`; storage `context_pack.py` | Accepted | Existing wire contract stays unchanged. The Core reference result adds the Stage 1 profile/manifest composition; expansion/refresh remains disabled. |
| BR-06 receipt/result authority | `governed_knowledge.delivery`; consumer-owned instrumentation | Implemented, unaccepted | Receipt profiles may be persisted as governed content. No claim is made for uninstrumented hosts. |
| BR-07 review/approval | `knowledge.propose`, `candidate.approve`, `candidate.reject`, `record.supersede`; shared mutation boundary | Accepted route | No new reviewer permission. Product-owner/security approval is still required for release. |
| BR-08 worker execution | Existing runtime job/execution contracts | Out of Stage 1 | Stage 2 disabled; no model worker was added. |
| BR-09 evaluation overlay | No Stage 1 exposure | Out of Stage 1 | Stage 2 disabled; candidates never enter live selection. |
| BR-10 impact/readiness | `semantic_registry/consumers.py`; `governed_knowledge.dependency` | Explicit dependencies implemented | Dynamic impact traversal and readiness guards remain Stage 3. |
| BR-11 retention/deletion | Existing governed-record classification, retention, deletion, backup/restore paths | Reused; profile-specific operational proof pending | No new table/cache/store. Derived receipts/manifests persisted by callers inherit governed-record policy. |
| BR-12 contribution transport | None enabled | Out of Stage 1 | Stage 4 disabled. |
| BR-13 product surfaces | `governed_knowledge.reference.Stage1ReferenceClient` | Programmatic reference implemented | No App Shell UI was added; keyboard/screen-reader proof is not claimed. |
| BR-14 runtime limits | Existing Context Pack budget/frontier limits plus profile bounds | Deterministic bounds implemented | Production workload measurements remain a release qualification item. |

# Contract/profile mapping

| Logical profile | Canonical home | Codec / implementation | Public operation |
|---|---|---|---|
| OrganisationalPosition | governed-record content plus trusted outer lifecycle/provenance | `position.py` | create → propose → approve; revision via supersede |
| TaskContextProfile | governed-record content | `context.py`, `profile_content.py` | stored through existing governed-record operations |
| ContextSelectionManifest | inline deterministic reference result; optional governed-record snapshot | `context.py`, `assembly.py` | no new operation; composed over `context_pack.build` |
| ContextDeliveryReceipt | consumer-issued governed-record content | `delivery.py`, `profile_content.py` | stored through `memory.create` when persistence is required |
| ExpertFeedback | immutable governed-record content/evidence | `feedback.py`, `profile_content.py` | `memory.create`; revised by new immutable record/supersession policy |
| FeedbackDiagnosis | versioned governed-record report | `feedback.py`, `profile_content.py` | existing governed-record operations |
| KnowledgeImprovementProposal | pending governed-record content | `feedback.py`, `profile_content.py` | existing proposal/review/admission operations |
| KnowledgeConsumerDependency | existing consumer types plus governed profile | `dependency.py`, `profile_content.py` | existing governed-record operations |

No schema migration or public application-schema regeneration was required. This avoids
creating a parallel entity universe and preserves backward compatibility of Application
Contract v1 and every adapter.

# Security, privacy, and recovery

- Free text and frontmatter are inert data; they cannot grant credentials, scopes, tools,
  roles, admission, or policy changes.
- Hidden fact names and values do not appear in applicability reasons.
- Cross-workspace position/profile composition is refused.
- Pending, superseded, retracted, future, expired, late-recorded, wrong-model,
  wrong-domain, overdue-under-strict-policy, and contested positions do not enter the
  selected position set.
- Context Pack citations still require fresh authorization. A manifest or digest is never
  a bearer capability.
- No model, network, filesystem, SQL, regex, or arbitrary-code capability is reachable
  from the evaluator.
- No new durable store exists. Disable/rollback removes the reference usage while existing
  generic governed records remain readable under their profile versions. Corrections use
  forward supersession rather than history deletion.
- Response-loss, concurrent replay, stale base, reviewer revocation, and duty-separation
  behaviour remain owned by the accepted shared mutation/governance implementation and
  its existing acceptance tests.

# Conformance evidence map

| Specification cases | Evidence |
|---|---|
| KI-T01–T11 | `tests/governed_knowledge/test_position.py`, `test_applicability.py`, and `test_stage1_assembly.py` |
| KI-T12–T15 | deterministic manifest repeat, byte-bound refusal, hidden-fact test, and pending exclusion |
| KI-T17–T18 | existing Context Pack authorization/freshness/stale-projection suites |
| KI-T19–T22 | `test_delivery.py` transport, provenance, added-source, attestation, and unknown-outcome cases |
| KI-T24 | existing MCP exposure manifest tests keep the read-only Context Pack tool and exclude governed mutations |
| KI-T25–T26 | `test_feedback.py` proves durable-profile validity with no model and no original receipt |
| KI-T34–T35 | inert malicious-content and evidence-span validation tests |
| KI-T36–T42 | existing shared mutation replay/concurrency/precondition/governance suites |
| KI-T71–T72 | existing approval binding and adapter/import-route guard suites |

KI-T16 progressive expansion/refresh and KI-T70 GUI accessibility are not claimed by the
programmatic Stage 1 slice. They are release conditions for any product surface that
advertises those behaviours.

# Verification record

Focused checks completed on the final implementation source:

```text
.venv/bin/pytest -q tests/governed_knowledge packages/omnivia-core-client/tests/test_package_isolation.py
129 passed

.venv/bin/ruff check src/omnivia_core/governed_knowledge tests/governed_knowledge
All checks passed

.venv/bin/mypy --strict src/omnivia_core/governed_knowledge
Success: no issues found in 13 source files
```

The complete repository preflight then passed at exact implementation commit `8665063`:

```text
./scripts/preflight
Package boundaries: passed; 53 boundary tests passed
Five wheel builds, isolated installs, and import checks: passed
Application Contract tests: 9,832 passed, 19 skipped
Canonical migration and compatibility tests: 1,686 passed, 2 skipped
Phase 0 baseline tests: 749 passed
Full repository tests: 24,511 passed, 35 skipped, 7 deprecation warnings
Benchmarks: 23 passed
Ruff 0.16.1: passed
mypy 2.3.0 --strict: no issues in 323 source files
macOS status-menu package: build passed; 48 tests passed
Final result: Core preflight passed
```

The four required hosted PR checks remain pending until the ready PR runs.

# Acceptance record

```text
Specification: SPEC-CORE-KNOWLEDGE-IMPROVEMENT-001 v0.1
Delivered stage: Stage 0 binding plus Stage 1 manual/programmatic foundation
Enabled capabilities: KI-01, KI-02 reference profile assembly, KI-03 manifests and
  consumer receipts, KI-04 model-free feedback/manual diagnosis/proposal, KI-06 explicit
  dependencies, one public-client-compatible renewal reference journey
Disabled capabilities: Stage 2 assisted diagnosis/evaluation; Stage 3 dynamic impact and
  readiness; Stage 4 contribution; Stage 5 broader work views; context expansion/refresh;
  any new MCP mutation; any uninstrumented-host delivery claim
Accepted baseline and exact implementation range: base
  6f11be4ba7f935ac506f16b0f82ba84c45b0648a; implementation commit 8665063
Contract/profile versions: governed-knowledge-position-v1,
  governed-knowledge-applicability-v1, governed-knowledge-task-context-v1,
  governed-knowledge-selection-manifest-v1, governed-knowledge-delivery-receipt-v1,
  governed-knowledge-feedback-v1, governed-knowledge-diagnosis-v1,
  governed-knowledge-proposal-v1, governed-knowledge-consumer-dependency-v1
Migration identifiers: none
Local verification: focused green; complete preflight passed at 8665063
Hosted acceptance: pending
Pilot/evaluation evidence: deterministic synthetic renewal fixture; no model evaluation
Unresolved bindings and affected features: UI/accessibility, progressive expansion,
  profile-specific deletion/restore exercise, production workload measurement
Architecture decision: pending reviewer acceptance
Runtime/storage decision: pending reviewer acceptance; no new runtime/store
Security/privacy decision: pending reviewer acceptance
Domain/product decision: pending reviewer acceptance
Consumer-owner readiness: reference consumer only; broader consumers disabled
Overall decision: CONDITIONAL GO for review; NO RELEASE until required checks/gates pass
Conditions and expiry: hosted checks and named acceptance gates
Release/activation record: none
```

# Rollback

Before acceptance, close the PR or revert its commits. After acceptance, revert the
governed-knowledge package/reference integration as a forward commit. No data migration
needs reversal. Existing profile-bearing governed records remain opaque, versioned content
and are not deleted or reinterpreted by code rollback.
