---
title: "OmniVia Core governed knowledge: Stage 1 acceptance closeout"
date: "2026-09-13"
status: "Accepted and merged"
specification: "SPEC-CORE-KNOWLEDGE-IMPROVEMENT-001 v0.1"
---

# Acceptance decision

Stage 1 of the governed organisational knowledge and expert-feedback work is
accepted and merged. This record supersedes the pre-merge acceptance state in
the implementation handoff; it does not enable any later-stage capability.

- Pull request: `#109`
- Reviewed and approved head: `9168a98c3bc47d95cce421e7a3dfa2a3ffed1ad0`
- Merge commit on `main`: `7dccb9f8f10e1b26abc675152ba615eb86232c01`
- Merged at: `2026-09-13T02:21:51Z`
- Decision: `GO` for Stage 1
- Migration identifiers: none
- Newly enabled public operations: none
- Newly enabled MCP mutations: none

# Accepted gates

The product owner approved the following gates for the exact reviewed head and
authorised the merge:

- architecture;
- runtime and storage;
- security and privacy; and
- product owner.

All required hosted checks were successful at the reviewed head before merge:

- `Core acceptance`;
- `Phase 2 platform (ubuntu-latest)`;
- `Phase 2 platform (macos-latest)`; and
- `Phase 2 platform (windows-latest)`.

# Accepted scope

The accepted scope is the Stage 1 manual, model-free foundation recorded in
`omnivia-core-governed-knowledge-stage-0-binding-and-stage-1-implementation-handoff-2026-09-13.md`:

- governed organisational-position profiles and bounded applicability;
- task-context profiles and deterministic selection manifests;
- consumer-issued delivery receipts;
- immutable feedback, manual diagnosis and pending improvement proposals;
- explicit consumer dependencies; and
- the public-client-compatible reference journey.

Stage 2 assisted diagnosis/evaluation, Stage 3 dynamic impact/readiness,
Stage 4 contribution and Stage 5 broader work views remain disabled. This
acceptance does not authorise a worker, model provider, candidate overlay,
scheduled evaluation, cross-workspace transfer or production-effect tool.

# Rollback and historical evidence

Rollback is a forward revert of the Stage 1 merge. There is no schema migration
to reverse. Existing governed records carrying the Stage 1 profile versions
remain opaque, immutable content and are not deleted or reinterpreted by a code
rollback. Historical results retain their original selection and delivery
references subject to current permission and retention policy.
