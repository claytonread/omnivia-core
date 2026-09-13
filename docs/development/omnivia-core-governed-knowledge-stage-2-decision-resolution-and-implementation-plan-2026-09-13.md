---
title: "OmniVia Core governed knowledge: Stage 2 decision resolution and implementation plan"
date: "2026-09-13"
status: "Resolved for bounded implementation; activation remains gated"
specification: "SPEC-CORE-KNOWLEDGE-IMPROVEMENT-001 v0.1"
baseline: "7dccb9f8f10e1b26abc675152ba615eb86232c01"
---

# Outcome

Implement a bounded Stage 2 pilot foundation that can accept one host-authorised
diagnosis worker, validate its structured output, evaluate an isolated candidate
overlay against the owner-reviewed twelve-case suite, retain every attempt and
produce a version-bound review report. Core must never acquire provider
credentials, execute a production effect, admit candidate knowledge, or treat an
evaluation result as approval.

This is a cross-owner design with a deliberately narrow Core change. Core owns
portable profiles, deterministic validation, aggregation and a provider-neutral
reference coordinator. The consuming Platform or Workflow host owns real model
routing, credentials and worker execution. Host integration is a separate
activation gate and does not block the model-free Stage 1 path.

# Baseline and accepted substrate

The implementation starts from merged Core commit
`7dccb9f8f10e1b26abc675152ba615eb86232c01`.

| Binding | Accepted symbol/path | Resolution for Stage 2 |
|---|---|---|
| BR-08 worker identity and isolation | `omnivia_core_runtime.execution.profile.ExecutorDescriptor`; `RuntimeProfileDescriptor`; `ExecutionLineage` | Bind exact source, executor, version, build/content digests, approved trust, required capability and isolation. The portable coordinator receives an already-authorised worker callable and the verifiable binding; it does not resolve credentials. |
| BR-08 routing | `omnivia_core_runtime.execution.registry.RuntimeExecutionRegistry` | Runtime/host integration must resolve the exact descriptor and fail closed before invoking the portable coordinator. Replacement, degraded health or insufficient isolation invalidates the binding. |
| BR-08 invocation | Provider-neutral injected-call patterns in `service.worker_adapter` and `service.chat_generation_executor` | Use a small typed protocol. No provider SDK, endpoint, credential, discovery or network code enters `omnivia_core.governed_knowledge`. |
| BR-08 cancellation/replay | `service.jobs`, runtime execution lineage and existing shared application mutation/replay boundary | Analysis/evaluation identifiers are immutable and every attempt is retained. Cancellation and provider failure produce explicit terminal attempt states; they do not delete feedback or create a proposal. No new canonical mutation is introduced. |
| BR-09 overlay | Existing immutable governed-record references, Stage 1 profiles and content digests | Represent an overlay as a bounded evaluation-only value pinned to baseline and candidate digests, purpose, workspace, expiry and authorised refs. It exposes no live-search, admission or mutation method. |
| BR-09 suite and report | Existing governed-record content route plus OV-CJ-1 | Add versioned content profiles for case, suite and report. They may be persisted through existing governed-record operations; no table or public operation is added. |
| BR-11 retention/deletion | `storage.semantic_retention.DeletionStorageClass`, including raw completions and worker scratch | Every Stage 2 profile declares classification and retention. Host-owned prompts/raw worker output/scratch stay outside portable profiles and must be covered by the existing deletion inventory before activation. |
| BR-14 limits | Existing profile content limit, Context Pack bounds, job attempt bounds and injected deadlines | Enforce bounded issue/case/attempt collections, three critical attempts, explicit token/cost observations and no unbounded retry. Production limits require measured host qualification. |

# Resolved decisions

## D2-01: no new knowledge authority

Assisted output is analysis evidence only. A worker may return proposed issue
records, diagnoses and the smallest proposed repair, but cannot set admission,
approval, review, permission or activation state. Proposed knowledge continues
through the accepted `knowledge.propose` and governance route.

## D2-02: feedback survives independently

The Stage 2 coordinator consumes an immutable `ExpertFeedback` reference/value.
Feedback persistence remains a separate, earlier operation. A worker failure,
invalid output, cancellation or incomplete evaluation returns a bounded result
and never rolls back or removes feedback.

## D2-03: exact, inert worker binding

The request records exact executor identity, build/content digest, runtime
profile, policy, model/provider observation where available, isolation and
lineage. The binding is evidence, not a grant. The host must reauthorise and
resolve it immediately before invocation. Worker output is inert data validated
against the registered schema and size bounds.

## D2-04: no production-effect tools

The Stage 2 worker contract has no tool-call or action field. Its declared
capabilities are limited to structured diagnosis and evaluation. Any returned
tool proposal, instruction to alter authority, unknown field or unbounded output
is rejected. The Core reference coordinator receives no filesystem, network,
database, credential or mutation handle.

## D2-05: isolated candidate overlay

The candidate overlay is an immutable, evaluation-purpose-only view. It binds
one workspace, exact baseline and candidate digests, authorised candidate refs,
creation/expiry times and retention/classification. It cannot appear in ordinary
selection and cannot grant permission. Expired, cross-workspace, live-purpose or
digest-mismatched overlays are refused before a worker runs.

## D2-06: deterministic checks dominate

Deterministic schema, authority, provenance, temporal, overlay, access and
required-case checks run before behavioural aggregation. Any required
deterministic failure or indeterminate result blocks the report. A human or
model judgement cannot override it.

## D2-07: complete attempt history

Results use `pass`, `fail`, `indeterminate`, `blocked`, `not_run` and
`cancelled`. Every attempt remains present and ordered. Infrastructure retries
are labelled; later passes never erase earlier failures. Required cases that are
missing, skipped, cancelled or indeterminate prevent an eligible report.

## D2-08: pilot policy

The owner-reviewed suite contains exactly the minimum `PC-01` through `PC-12`
cases for the initial reference pilot. `PC-02`, `PC-03`, `PC-09`, `PC-10`,
`PC-11`, `PC-12` and the triggering scenario are critical and require three
predeclared attempts. Reducing that set requires a new reviewed policy version.

## D2-09: report is not approval

The aggregate report can be `eligible_for_review`, `blocked` or `inconclusive`.
It records exact bindings, all attempts, deterministic findings, coverage,
cost/token observations and human/judge provenance. It contains no `approved`,
`admitted` or `activated` state.

## D2-10: persistence and exposure

No migration, endpoint, CLI command, MCP tool or application-contract operation
is added in this slice. Profiles use existing governed-record content codecs.
Stage 2 remains opt-in and disabled unless the host satisfies its own worker,
retention, privacy and product gates.

# Implementation packages

## Package A: structured diagnosis

- Add bounded issue extraction records with exact feedback spans.
- Add worker-binding, request and outcome profiles.
- Validate category evidence, context limitations and minimal-proposal linkage.
- Preserve model/provider failure and cancellation without losing feedback.
- Reject unknown authority/tool/admission claims in decoded worker output.

## Package B: evaluation profiles and overlay

- Add candidate-overlay, case, suite, attempt and report profiles.
- Provide OV-CJ-1 content codecs and integrity digests.
- Enforce workspace, purpose, expiry, baseline/candidate and access bindings.
- Keep expected outcomes/rubrics separate from consumer-visible inputs.

## Package C: bounded reference coordinator

- Accept an injected, host-authorised structured worker protocol.
- Perform pre-invocation deterministic validation.
- Retain every attempt and aggregate the Stage 2 gate deterministically.
- Return a complete review package without persistence or side effects.

## Package D: synthetic pilot

- Implement `PC-01` through `PC-12` using the renewal scenario.
- Exercise the three distinct diagnoses: retrieval miss, consumer
  transformation loss and genuinely incomplete knowledge.
- Run critical cases and the triggering scenario three times.
- Include malicious-instruction, hidden-access, contested and historical cases.

# Conformance scope

Implement and map the following tests:

- `KI-T27` through `KI-T33`: extraction, correct diagnosis boundaries, temporal
  reconstruction, broader-analyst access, disagreement and lineage;
- `KI-T39`: feedback remains independently retrievable after analysis failure;
- `KI-T43`: candidate overlay never enters live retrieval;
- `KI-T44`: no production-effect capability is reachable;
- `KI-T45`: expected answer/rationale leakage is detected;
- `KI-T46`: deterministic failure blocks a model/human pass;
- `KI-T47`: required skipped, timed-out or cancelled cases cannot pass;
- `KI-T48`: all retries remain visible and no pass is cherry-picked;
- `KI-T49`: mutable provider/model identity limitation is recorded;
- `KI-T50`: a feedback-derived case remains proposed until owner review.

Focused verification:

```text
.venv/bin/pytest -q tests/governed_knowledge
.venv/bin/ruff check src/omnivia_core/governed_knowledge tests/governed_knowledge
.venv/bin/mypy --strict src/omnivia_core/governed_knowledge
```

Acceptance additionally requires `./scripts/preflight` and all four required
hosted PR checks at the exact PR head.

# Activation gates

Implementation completion is not product activation. Activation requires:

1. one exact host executor registered, approved, healthy and isolated at the
   policy-required level;
2. a host-side proof that credentials and provider SDKs remain outside Core;
3. a deletion/restore exercise covering prompts, raw output, reports and worker
   scratch;
4. measured deadline, token, cost and memory bounds on named hardware/provider;
5. owner review of the twelve cases, rubric and critical-case set;
6. security proof that the worker has no production-effect tools;
7. distinct reviewers where effective policy requires separation of duty; and
8. a complete exact-head acceptance handoff with explicit `GO`, `CONDITIONAL
   GO` or `NO-GO`.

# Rollback

Before activation, disable the host registration or remove the reference
coordinator call. Revert Stage 2 code as a forward commit. No database downgrade
is required. Existing Stage 2 profile-bearing governed records remain opaque,
versioned evidence. Candidate overlays expire and are never admitted or added to
live retrieval by this implementation.

