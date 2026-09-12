# OmniVia Core Semantic Registry Phase 2 Decision Record

Date: 2026-09-12
Work package: `WP-SEM-06`
Specification authority: `SPEC-CORE-SEM-001`, version 0.2, dated 2026-09-11
Plan reference: `docs/development/omnivia-core-organisational-model-phase-1-closeout-and-phase-2-implementation-plan-2026-09-12.md` (section 4, Phase 2 authorisation gate)
Fixture corpus reference: `tests/fixtures/semantic_registry/phase-2-acceptance-v1.json` (`semantic-registry-phase-2-acceptance-v1`, schema `1.0.0`)

**Status: Resolutions R1–R3 implemented locally; formal acceptance pending.**

This record freezes the implementation-level decisions required by the Phase 2
authorisation gate for `WP-SEM-06`. It is a technical baseline for B2–B9
implementation work. It does not constitute architecture, security/privacy,
product-owner, hosted CI, or merge approval — those are separate, unresolved
gates (see [Pending acceptance gates](#pending-acceptance-gates)).

## 1. Evidence authority and storage

| Decision | Frozen value |
|---|---|
| Canonical writer | SQLite is the canonical authority; only the fenced Core semantic writer may mutate canonical semantic tables |
| Evidence kinds | `manual`, `document`, `record`, `event` |
| Locator schemes | `urn`, `file`, `https`, `opaque` — stored as metadata only, never fetched or dereferenced by Core |
| Integrity | SHA-256 digest over protected evidence bytes, scoped by `(workspace_id, source_id, digest, version)` |
| Content ownership | Canonical metadata (evidence record, locator, digest, classification) is distinct from protected content, addressed only via `content_ref` |
| Content exposure | Protected content is absent from error messages, event payloads, and unfiltered projections; it is retrievable only through a separately authorised content read |

## 2. Classification and retention

| Decision | Frozen value |
|---|---|
| Classification vocabulary | `public`, `internal`, `confidential`, `restricted` |
| Inheritance rule | Most-restrictive-wins across the workspace floor, source, evidence and every derived observation, assertion and candidate projection |
| Downgrade rule | No unauthorised downgrade; a classification may only be lowered by an explicitly authorised action, never implicitly by aggregation or derivation |
| Retention default | Workspace-level default retention period |
| Retention override | Override may only be stricter (shorter) than the workspace default, never looser |
| Legal hold | Legal hold suspends retention-driven deletion regardless of override |
| Deletion receipts | Deletion produces a content-free receipt (record IDs, integrity digests, timestamp and reason code); the receipt never retains deleted sensitive content |

## 3. Capability matrix

| Capability | Grants | Independently enforced from |
|---|---|---|
| `evidence.register` | Create evidence metadata + protected content | — |
| `evidence.metadata.read` | Read evidence metadata (source, locator, digest, classification) | `evidence.content.read` |
| `evidence.content.read` | Read protected content via `content_ref` | `evidence.metadata.read` |
| `observation.create.manual` | Create a human-authored observation | `observation.create.rule` |
| `observation.create.rule` | Create a deterministic rule-generated observation | `observation.create.manual` |
| `candidate.aggregate` | Run deterministic aggregation producing candidates | `candidate.read` |
| `candidate.read` | Inspect candidate + contributions | `candidate.reject` / `candidate.convert` |
| `candidate.reject` | Reject and suppress a candidate | `candidate.reconsider` |
| `candidate.reconsider` | Trigger reconsideration of a suppressed candidate | `candidate.reject` |
| `candidate.convert` | Convert a candidate into a Phase 1 change set | `candidate.aggregate` |
| `assertion.history.read` | Read governed assertion history (including superseded records) | `evidence.content.read` |
| `temporal.query` | Run temporal queries with resolved axes echoed in the response | — |

Rules:

- Metadata and content capabilities are checked independently; holding one never implies the other (fixture: `case-permission-metadata-filtered`, `case-permission-sensitive-content-denied`).
- Permission checks occur before sensitive content retrieval and are rechecked inside canonical write transactions where authority could change mid-transaction.
- Cross-workspace lookups return the same non-disclosing error whether the target ID exists in another workspace or does not exist at all.

## 4. Temporal model

| Decision | Frozen value |
|---|---|
| Encoding | UTC, canonical precision vocabulary: `year`, `month`, `day`, `hour`, `minute`, `second` |
| Timezone resolution order | (1) explicit offset in source text, (2) trusted recorded UTC, numeric offset or IANA source timezone, (3) preserve timezone-less source text and reduce sub-day structured values to day precision — never guess |
| `effective_from` | `valid_from` when stated, otherwise `attested_from`; absent both → fail closed (`TEMPORAL_START_INDETERMINATE`) |
| `effective_to` | `valid_to` when end state is `stated`; `attested_to` when end state is `unknown`; `+Infinity` when end state is `open`; `unknown` with no `attested_to` → fail closed (`TEMPORAL_END_INDETERMINATE`) |
| Interval shape | Half-open `[effective_from, effective_to)` |
| Source time authority | Authorised source time first; queryable, explicit ingestion-time fallback only when source time is unavailable |
| Cross-surface consistency | Domain code, SQL projections, APIs and export fixtures must produce identical results for the same inputs |

## 5. Candidate lifecycle

| State | Entered via |
|---|---|
| `draft` | Initial aggregation output before human or rule confirmation |
| `active` | Confirmed candidate awaiting disposition |
| `proposed` | Candidate proposed for conversion to a Phase 1 change set |
| `rejected` | Human rejection recorded with reason code |
| `suppressed` | Rejection equivalence signature matched; suppression active |
| `reconsidered` | Suppression lifted by a recorded trigger |

Reconsideration triggers (from fixture corpus, all four required): new evidence
version, rule/policy version change, suppression expiry, human override.
Suppression ends only when one of these recorded conditions is satisfied — never
implicitly.

## 6. Deduplication, suppression and contradictions

| Decision | Frozen value |
|---|---|
| Evidence dedup scope | Workspace-scoped; identical content in two workspaces is never deduplicated against each other |
| Dedup key | `(workspace_id, source_id, digest, version)` |
| Suppression signature | Rejection equivalence signature keyed per workspace and versioned |
| Contradictions | Retained, linked, and queryable; never silently consolidated or deleted |
| Rule authority | Aggregation/dedup/suppression rules are Core-owned, pure, deterministic and replayable; a rule/policy version change never silently reinterprets a historical candidate |

## 7. Conversion boundary

Candidate-to-change-set conversion produces an ordinary, unapproved Phase 1
change set that enters the existing Phase 1 human review and publication path.
No machine-generated material may approve or publish its own resulting change
set.

## 8. Threat model

| Threat | Mitigation |
|---|---|
| Prompt injection via untrusted evidence | Evidence content is data, never instructions or authority; locators are metadata only and are never fetched/executed by Core; deterministic rules operate on typed fields, not free text interpreted as directives |
| Disclosure of protected content | Content lives behind `content_ref`, gated by a capability independent of metadata read; absent from errors, events, and unfiltered projections |
| Cross-workspace leakage | All identifiers, dedup, reads and writes are workspace-scoped; cross-workspace access returns a non-disclosing error indistinguishable from not-found |
| Stale writers | Every canonical write requires the current writer lease/fence; stale writer attempts fail at the fence, not at the application layer |
| Forged actor/workspace claims | Transport-supplied workspace and actor context is verified against command claims before execution; permission is rechecked inside the write transaction |
| Digest substitution | SHA-256 integrity is scoped to `(workspace_id, source_id, digest, version)`; digests are verified on read and after restore |
| Outbox leakage | Outbox events carry immutable record references (IDs), never sensitive evidence spans or protected content |
| Retention/deletion abuse | Retention override can only tighten, never loosen, the workspace default; legal hold overrides retention-driven deletion; deletion receipts are content-free |
| Restore integrity | Backup/restore verifies canonical digests, temporal anchors and suppression state; a restore that fails verification is rejected, not silently accepted |
| Optional-worker non-authority | External ontology/extraction/validation/projection/LLM workers hold no canonical database credentials and no direct mutation methods; they may only submit candidate material subject to the same permission and fencing checks as any other input |

## 9. Fixture corpus mapping

Corpus: `tests/fixtures/semantic_registry/phase-2-acceptance-v1.json`
Structural test: `tests/semantic_registry/test_phase2_acceptance_fixtures.py` (validates fixture shape/coverage only; does not yet exercise implementation logic)

| Frozen area | Fixture requirement | Case IDs |
|---|---|---|
| Evidence dedup (workspace-scoped) | `b1-exact-duplicate-evidence-one-workspace` | `case-evidence-dedup-workspace-scoped` |
| Evidence dedup (cross-workspace) | `b1-identical-evidence-two-workspaces` | `case-evidence-cross-workspace-no-dedup` |
| Evidence traceability | `b1-multiple-observations-one-evidence` | `case-observation-multi-support-single-evidence` |
| Contradictions retained | `b1-contradictory-observations-remain-visible` | `case-observation-contradictory-retained` |
| Candidate lifecycle / reconsideration | `b1-rejected-candidates-and-reconsideration-triggers` | `case-candidate-rejected-suppressed-baseline`, `case-candidate-reconsideration-new-evidence`, `case-candidate-reconsideration-rule-version-changed`, `case-candidate-reconsideration-expired`, `case-candidate-reconsideration-human-override` |
| Capability separation (metadata/content) | `b1-permission-filtered-evidence-metadata-and-sensitive-content` | `case-permission-metadata-filtered`, `case-permission-sensitive-content-denied` |
| Temporal boundary states | `b1-temporal-boundary-states` | `case-temporal-boundary-stated`, `case-temporal-boundary-absent`, `case-temporal-boundary-attested`, `case-temporal-boundary-unknown`, `case-temporal-boundary-open` |
| Temporal precision | `b1-temporal-precision-levels` | `case-temporal-precision-year` … `case-temporal-precision-second` |
| Timezone resolution | `b1-timezone-handling` | `case-temporal-timezone-explicit-offset`, `case-temporal-timezone-trusted-source`, `case-temporal-timezone-less-text` |
| Source-time backfill | `b1-historical-backfill-source-time` | `case-temporal-historical-backfill-source-time`, `case-temporal-historical-backfill-ingestion-fallback` |
| Supersession without history rewrite | `b1-correction-supersession-no-history-rewrite` | `case-assertion-correction-supersession-no-rewrite` |

Phase 2 exit criteria 1–6 (plan section 5, B9) are each already mapped to at
least one case inside the corpus's `phase2_exit_criteria` block; no gap exists
between this decision record and the existing fixture coverage.

## 10. Resolution amendment: events, temporal metadata and adapter idempotency

The following decisions supersede the remaining implementation interpretations in
the Phase 2 resolution handoff:

1. PascalCase Section 19 event names are logical specification labels. Lowercase
   dotted, explicitly versioned values are canonical on the wire. The exhaustive
   mapping lives in `omnivia_core_runtime.storage.semantic_events`; current event
   producers import their values from that registry. The former unversioned
   publication producer now emits `semantic.version.published.v1`. No existing
   immutable outbox row is rewritten.
2. Migration `0040_semantic_temporal_source_metadata.sql` adds adjacent source-text
   and trusted-timezone columns for evidence and observation source times and for
   assertion valid/attested boundaries. Typed reads reconstruct complete
   `TemporalInstant` values. Auxiliary fields participate in record digests only
   when present, preserving historical digest bytes for records with null metadata,
   and remain outside Semantic Model content digests and outbox payloads.
3. Phase 2 mutation methods remain in-process only. The executable external-source
   inventory is empty and tests scan the CLI, client, MCP and runtime service roots
   for coupling. Any first external mutation adapter must compose through the
   existing governed caller-scoped idempotency seam; a semantic-only idempotency
   store remains prohibited.

This amendment is implementation evidence. It does not grant architecture,
security/privacy, product, hosted CI or merge approval.

## 11. Pending acceptance gates

The following are explicitly **not** resolved by this record and remain
outstanding before Phase 2 can be formally accepted:

- Architecture review sign-off on the frozen decisions in sections 1–7.
- Security/privacy review of the threat model in section 8.
- Product-owner approval of the bounded Phase 2 scope.
- A green hosted `Core acceptance` run covering Phase 2 changes.
- Merge approval into the accepted integration branch.
- Review and acceptance of the local R1–R3 resolution patch on the final commit.

## 12. Rollback posture

- The resolution adds forward migration 0040. Existing migrations and immutable
  rows are unchanged. Rollback is restoration of the verified pre-0040 database;
  application code must not write source metadata until 0040 is present.
- Any future change to the values frozen in sections 1–7 that alters storage
  identity, temporal meaning, retention, or access-control semantics requires a
  new decision record superseding this one, not an in-place edit — consistent
  with the plan's exit criterion that "no unresolved decision changes storage
  identity, temporal meaning, retention or access-control semantics."
