# OmniVia Core MCP standalone authoring and ingestion requirements: v1.4 completion addendum

**Revision:** 1.4 addendum
**Date:** 2026-10-03
**Amended:** 2026-10-04. Section 12 supersedes two evidence statements in sections 10 and 11; no gate status changes.
**Status:** normative completion baseline for Phase 8. It records a reconciliation.
It does not change the product, any inventory, any host record, or any gate status.
Feature status remains in progress; see section 11.
**Base specification:** `docs/development/omnivia-core-mcp-standalone-authoring-and-ingestion-requirements-2026-09-12-v1.3.md`
(v1.3 is preserved as historical text and remains in force except where section 2 supersedes it.)
**Governs:** `docs/development/omnivia-core-mcp-authoring-phase-8-completion-plan-2026-10-03.md`,
`docs/development/omnivia-core-mcp-standalone-authoring-and-ingestion-implementation-plan-2026-09-12.md`,
and `docs/development/omnivia-core-mcp-standalone-authoring-and-ingestion-traceability-2026-09-12.md`.

## 1. Precedence and authority

1. Where this addendum and v1.3 disagree, this addendum controls. Otherwise v1.3 applies unchanged.
2. Inventory, classification and version facts are read from the code and the generated catalogue, not from prose:
   - the exposure manifest, `packages/omnivia-core-mcp/src/omnivia_core_mcp/manifest.py`
     (`MANIFEST_VERSION`, `RESTRICTED_MANIFEST`, `_AUTHORING_ADDITIONS`, `ADMITTED_MUTATIONS`);
   - the generated Application Contract v1 operation catalogue,
     `contracts/application/v1/schemas/operations.schema.json` (`x-omnivia-operation-catalogue`,
     58 operations);
   - the reviewed MCP dependency pins, `scripts/mcp-wheelhouse-constraints.txt`.
3. `tests/service_conformance/test_mcp_authoring_traceability.py` checks this addendum against the sources named in item 2 and fails on drift.
4. Nothing in this addendum is a claim that a pending gate has passed.

## 2. Supersessions

| v1.3 location | v1.3 statement (abridged) | Superseded by |
|---|---|---|
| Section 1, bullet 1 | `restricted`: "the existing six read-only tools" | Section 3: thirteen tools. Restricted is a bounded non-authoring profile, not a read-only profile. |
| Section 1, bullet 2 | `authoring`: "those six tools plus" five | Section 4: eighteen tools, being the thirteen restricted tools plus five additions. |
| Section 3.1 | Restricted list of six; "Its behavior remains read-only." | Section 3. The six listed tools remain restricted tools, with unchanged bindings. The read-only sentence is withdrawn. |
| Section 3.2 | "exactly the six restricted tools plus" five | Section 4: the thirteen restricted tools plus five. |
| Section 10, tool annotation paragraph | Eight authoring reads and three mutations, with `readOnlyHint` set by that split | Section 5: fourteen reads and four mutations in authoring, and one mutation in restricted. Annotations are read from the catalogue. |
| Section 12, item 8 | "must no longer claim the product is universally read-only" | Preserved. Restricted must not be described as read-only either (section 3). |
| Section 13.A | "exactly six restricted tools"; "exactly eleven tools" | Thirteen restricted tools; eighteen authoring tools. |
| Section 13.B, step 2 | "confirm eleven tools are visible" | Eighteen tools are visible under the authoring profile. |
| Section 17 and Appendices A, F and G | Inventory counts as the review state of their date | Preserved as historical review records. They describe the reviewed revision, not the current one. |
| Implementation plan, section 1 (Outcome) | `restricted`, "preserving the existing six read-only tools" | Section 3 of this addendum. |
| Implementation plan, section 3.3 | "The MCP manifest is version 1.1 and admits only the six read tools." | Historical description of the pre-feature manifest. Current version is in section 8. |
| Implementation plan, Phase 5, mutation annotations | Three mutations with `readOnlyHint=false` | Section 5: four admitted mutations, with `readOnlyHint` derived from the catalogue side effect. |
| Implementation plan, section 9, bullet 4 | "MCP manifest 2.0 advertises exactly six restricted or eleven authoring tools" | Thirteen restricted or eighteen authoring tools, manifest version `2.3`. |

Statements about the pre-feature baseline, such as "the MCP adapter exposed six reads" before Phase 0, are historical facts and are preserved. The rows of v1.3 section 3.3 (excluded operations), section 4 (the six existing read bindings) and section 8.4 (`job_get` and `job_events` as read-only observations) are preserved unchanged.

## 3. Restricted profile: thirteen tools

The restricted profile is a **bounded non-authoring** profile. It contains twelve catalogue reads and one mutation with durable effects, `decision.evaluate`. It is not read-only. A caller that names no profile receives this profile.

The thirteen tools, in manifest order:

1. `workspace_inspect` (`workspace.inspect`)
2. `evidence_search` (`evidence.search`)
3. `knowledge_search` (`knowledge.search`)
4. `memory_search` (`memory.search`)
5. `graph_traverse` (`graph.traverse`)
6. `context_pack_build` (`context_pack.build`)
7. `engineering_search` (`engineering.search`)
8. `engineering_expand` (`engineering.expand`)
9. `engineering_context_build` (`engineering.context.build`)
10. `decision_evaluate` (`decision.evaluate`), the only mutation in this profile
11. `decision_record_get` (`decision.record.get`)
12. `decision_record_list` (`decision.record.list`)
13. `decision_status` (`decision.status`)

Items 1 to 6 are the six v1.3 read tools, with their v1.3 bindings unchanged. Items 7 to 9 are the Engineering Memory reads. Items 10 to 13 are the decision tools.

## 4. Authoring additions: five tools

The authoring profile is the thirteen restricted tools, in the order above, followed by these five, in manifest order:

14. `memory_create` (`memory.create`): a mutation that creates an evidence-backed,
    proposed-only governed memory record. It never creates accepted canonical knowledge.
15. `evidence_capture` (`evidence.capture`): a mutation that captures one submitted document as an L0 evidence artifact.
16. `import_start` (`import.start`): a mutation that starts an import. It always answers with a job.
17. `job_get` (`job.get`): a read that observes one job.
18. `job_events` (`job.events`): a read of one page of a job's event history.

The authoring profile therefore contains 18 tools: 14 reads and 4 mutations. The four mutations are `decision.evaluate`, `memory.create`, `evidence.capture` and `import.start`. Authoring adds 3 mutations and 2 reads to the restricted profile.

## 5. Classification of every exposed tool

Every value below is read from the catalogue entry of the named operation, or from the manifest's purpose and profile assignment. Advertised annotations follow from the same catalogue values: `readOnlyHint` is true exactly when the side effect is `none`, `destructiveHint` is false for all 18 tools, `idempotentHint` equals `safe_to_retry`, and `openWorldHint` is false.

Input shape: the 14 read tools advertise their canonical operation input directly. The four admitted mutations, including `decision.evaluate`, advertise a closed wrapper of exactly `{input, idempotency_key}`. No other outer property is accepted.

| # | Tool | Operation | Profile | Side effect | Audit | Purpose | Scopes | Capability | Idempotency |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `workspace_inspect` | `workspace.inspect` | both | none | read | `workspace_inspection` | `workspace:read` | `workspace.read` 1.0 | No key; safe to retry |
| 2 | `evidence_search` | `evidence.search` | both | none | read | `knowledge_retrieval` | `memory:read` | `evidence.read` 1.0 | No key; safe to retry |
| 3 | `knowledge_search` | `knowledge.search` | both | none | read | `knowledge_retrieval` | `memory:read` | `knowledge.read` 1.0 | No key; safe to retry |
| 4 | `memory_search` | `memory.search` | both | none | read | `knowledge_retrieval` | `memory:read` | `memory.read` 1.0 | No key; safe to retry |
| 5 | `graph_traverse` | `graph.traverse` | both | none | read | `knowledge_retrieval` | `graph:read` | `graph.read` 1.0 | No key; safe to retry |
| 6 | `context_pack_build` | `context_pack.build` | both | none | read | `knowledge_retrieval` | `memory:read` | `context_pack.build` 1.0 | No key; safe to retry |
| 7 | `engineering_search` | `engineering.search` | both | none | read | `engineering_search` | `engineering:read` | `engineering.read` 1.0 | No key; safe to retry |
| 8 | `engineering_expand` | `engineering.expand` | both | none | read | `engineering_expand` | `engineering:read` | `engineering.read` 1.0 | No key; safe to retry |
| 9 | `engineering_context_build` | `engineering.context.build` | both | none | read | `engineering_context` | `engineering:read` | `engineering.read` 1.0 | No key; safe to retry |
| 10 | `decision_evaluate` | `decision.evaluate` | both | update | mutation | `decision_evaluation` | `decision:invoke` | `decision.invoke` 1.0 | Key required; not safe to retry |
| 11 | `decision_record_get` | `decision.record.get` | both | none | read | `decision_record` | `decision:read` | `decision.read` 1.0 | No key; safe to retry |
| 12 | `decision_record_list` | `decision.record.list` | both | none | read | `decision_record` | `decision:read` | `decision.read` 1.0 | No key; safe to retry |
| 13 | `decision_status` | `decision.status` | both | none | read | `decision_status` | `decision:read` | `decision.read` 1.0 | No key; safe to retry |
| 14 | `memory_create` | `memory.create` | authoring | create | mutation | `memory_authoring` | `memory:write` | `memory.write` 1.0 | Key required; not safe to retry |
| 15 | `evidence_capture` | `evidence.capture` | authoring | create | mutation | `content_ingestion` | `memory:write` | `evidence.write` 1.0 | Key required; not safe to retry |
| 16 | `import_start` | `import.start` | authoring | create | mutation | `content_ingestion` | `memory:write` | `ingestion.import` 1.0 | Key required; not safe to retry |
| 17 | `job_get` | `job.get` | authoring | none | read | `job_observation` | `job:read` | `job.read` 1.0 | No key; safe to retry |
| 18 | `job_events` | `job.events` | authoring | none | read | `job_observation` | `job:read` | `job.read` 1.0 | No key; safe to retry |

Every capability above is `required: true` in the catalogue. Every tool is `audited: true`. Reads are not paginated, except `evidence_search`, `knowledge_search`, `memory_search`, `graph_traverse`, `engineering_search`, `decision_record_list`, `job_events`, which page with a 1,000-item maximum. No tool declares a mutation precondition.

## 6. `decision.evaluate`: explicit admission

**Catalogue facts.** `decision.evaluate` declares scope `decision:invoke`, side effect `update`, and audit category `mutation`. It requires capability `decision.invoke` at minimum version 1.0. It supports and requires an idempotency key and is not safe to retry. Its completion mode is `always_returns_job`, with job kind `decision.evaluate`. Its result is a durable `DecisionRecord`. It is not paginated and takes no mutation precondition.

**Admission basis.** The manifest's `_admit` admits a catalogue operation only when it is a read (side effect `none` and audit `read`), or when it is named in the literal set `ADMITTED_MUTATIONS`. `decision.evaluate` is not a read, so it is admitted only because it is the fourth member of that literal set. The set is not derived from catalogue metadata. Adding a mutation requires editing that set, which is a reviewed change and a section 9 event.

**Effects.** Each call consumes resources and creates a durable evaluation record, a job record and audit records. It never mutates business records, such as memory, evidence, governed knowledge or governance state. It executes no action. Its prediction is advisory and never authorises an action.

**Authority.** The workspace, the principal, the purpose (`decision_evaluation`), the scope and the capability come from protected configuration, the service session and the catalogue entry. None of them comes from a model argument. The tool is exposed by both profiles, and it is the only mutation in the restricted profile.

## 7. Excluded operations and model-selected authority

**Excluded from every profile, and preserved from v1.3 section 3.3 and the manifest module documentation:**

- service start, stop, health, readiness, status and discovery;
- bootstrap, workspace creation, selection and enumeration;
- grant creation, renewal, revocation and inspection;
- governance decisions, including candidate approval or rejection, publication and supersession;
- `job.cancel` and `job.retry`;
- chat, workflow and connector mutation;
- unrestricted filesystem path selection, URLs, credentials and connector configuration;
- administrative configuration;
- the continuity session register, which is not model-facing; checkpoint append, withdrawn at version `2.2`; and handoff, withdrawn at version `2.3`.

**Model-selected authority.** A model cannot select the workspace, principal, purpose, scope, capability, credential, grant, endpoint or profile. Profile is chosen once at startup by configuration. The mutation wrapper refuses any outer key other than `input` and `idempotency_key`. Reserved authority names are refused at the wrapper and again inside the nested input.

## 8. Manifest version

The exposure manifest version is **`2.3`**, the value of `MANIFEST_VERSION`. This addendum adopts that version and changes no code. The version history is `1.0`, then `1.1` (six reads), then `2.0` (two profiles and the mutation wrapper), then `2.2` (checkpoint append withdrawn), then `2.3` (handoff withdrawn as well).

## 9. Compatibility rules for future inventory changes

Any change to the exposed set, to a tool's profile membership, to a tool's name or operation binding, to a projected schema, or to `ADMITTED_MUTATIONS` requires all of the following:

1. an explicit normative review, recorded in a new addendum or revision that names the change and supersedes the rows it affects;
2. a bump of `MANIFEST_VERSION`, with the version history in the manifest documentation updated;
3. updated exposure-manifest tests and an updated traceability conformance test, including counts, the classification table and the `decision.evaluate` classification where relevant;
4. updated documentation: this addendum, the package README and `docs/distribution/mcp-host-interoperability.md`;
5. requalification of every affected real host. Both Claude Code 2.1.288 and Codex CLI 0.146.0 must be rerun for every affected profile, with I-1 through I-8 repeated where they depend on the changed surface, at one new frozen tip with new external records. Earlier host records do not carry forward. Installed-wheel qualification (H-6 and related rows) must also be rerun when the wheel changes.

A catalogue operation that is added but not exposed changes no inventory. It is still refused by the admission check until someone adds it to the manifest.

## 10. Real-host acceptance matrix

**Frozen qualification baseline**, from the completion plan (section 4) and `scripts/mcp-wheelhouse-constraints.txt`:

| Component | Value |
|---|---|
| Claude Code | 2.1.288 |
| Codex CLI | 0.146.0 |
| macOS | 27.0, build 26A428, arm64 |
| MCP SDK | `mcp==2.0.0` |
| MCP types | `mcp-types==2.0.0` |
| Profiles | `restricted` (13 tools) and `authoring` (18 tools) |

**Gates.** Every gate below is pending for both hosts. None is green, and none may become green from anything in this tree.

| Gate | Requirement | Claude Code 2.1.288 | Codex CLI 0.146.0 |
|---|---|---|---|
| I-1 | Install Core from the release artifact on a clean supported macOS account | pending-phase-8 | pending-phase-8 |
| I-2 | Configure each profile using the documented host settings | pending-phase-8 | pending-phase-8 |
| I-3 | Verify initialize and exact tool discovery: 13 under restricted, 18 under authoring | pending-phase-8 | pending-phase-8 |
| I-4 | Run the empty-workspace authoring journey | pending-phase-8 | pending-phase-8 |
| I-5 | Run the staged-import and job-observation journey | pending-phase-8 | pending-phase-8 |
| I-6 | Prove same-key recovery after an intentionally interrupted response | pending-phase-8 | pending-phase-8 |
| I-7 | Prove protocol-only stdout, host restart, Core service restart and continued observation | pending-phase-8 | pending-phase-8 |
| I-8 | Revoke authoring and prove that writes fail closed under the documented restart model | pending-phase-8 | pending-phase-8 |

Two points qualify the table. First, Codex CLI 0.146.0 passed I-1 through I-8
against the clean `4ec9fa17c447c81e58056d99e703b587fcf0afa3` candidate. That
record validated the real-host lane at that revision, but this addendum creates
a later tip, so the result is historical diagnostic evidence rather than final
exact-tip acceptance. Second, no Claude Code real-host record exists. Its
isolated authentication requires a portable token-only file, which is an
external input. Section 12 supersedes the first point.

Real-host acceptance requires the exact frozen tip, with schema-validated records retained outside the source tree and attached to acceptance evidence. The record must name the source commit and wheel digests.

## 11. Gate status

This addendum does not mark any gate green. Specifically:

- I-1 through I-8 remain `pending-phase-8` for both hosts (section 10).
- The real-host part of B-12 remains pending. The installed-wheel retained-record row is green in the traceability record, and that is the only B-12 evidence this addendum relies on. Section 12 supersedes this bullet.
- H-1 through H-7 keep the statuses recorded in the traceability record.
- Feature status remains in progress. It may change only after every gate in the completion plan's definition of done holds at one tip, with direct evidence recorded in the traceability record.

## 12. Supersession note, 2026-10-04

This note supersedes two statements in sections 10 and 11. Their text stays as the record of what this addendum said on 2026-10-03. No gate status changes.

1. Section 10 says Codex CLI 0.146.0 "passed I-1 through I-8" against `4ec9fa17c447c81e58056d99e703b587fcf0afa3`, and that the record "validated the real-host lane". Superseded: that run is a historical diagnostic. Its record is unauditable under the current closed record schema, `docs/distribution/schemas/mcp-real-host-qualification-record-v1.schema.json`, so it closes no current I row at that or any later tip. Every I-1 through I-8 cell in section 10 stays `pending-phase-8` for both hosts.
2. Section 11 says the installed-wheel retained-record row "is green in the traceability record". Superseded: the traceability record holds B-12 and H-5 through H-7 as `partial`. The installed-wheel retained-record gate is implemented locally, but it is not current frozen-candidate evidence, because the only retained record predates the current candidate. This addendum relies on no B-12 evidence until the frozen candidate produces its own record.

Section 11's rule is unchanged: this addendum does not mark any gate green.
