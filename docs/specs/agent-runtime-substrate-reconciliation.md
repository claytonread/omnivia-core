# Agent Runtime Substrate Reconciliation (Gate 0)

## Status

Accepted reconciliation record. **Non-behavioural.** This document freezes how the
job, control-plane and run-ledger implementations that exist in Core today relate
to the canonical OmniVia Runtime model. It adds no runtime state, no schema, no
migration and no API. RT-101 (canonical Runtime schema, enums, semantic
validation) and RT-102 (additive persistence migration and repositories) are
separate lanes and are out of scope here.

Public product term is **Runtime**. Executable definitions are **Agent
Components** or **Workflows**. The private seam is an agent-worker kernel; there
is no public Harness object and this document introduces none.

## 1. Purpose and non-goals

Core already owns three unrelated things that all use the word *run*. Before any
canonical Runtime record is written, each has to be named, assigned a status, and
either extended, projected, migrated or retired. Guessing later is how two
competing Run tables get built.

Non-goals: no new store, no new scheduler, no change to public job wire
behaviour, no claim that the existing control-plane registry is already the
canonical Runtime.

## 2. Current representations

### 2.1 `JobQueue` / `omnivia_durable_jobs` and its durable history

The mutable scheduler row is a single flat table with no workspace column:

- `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/migration_files/0001_ownership_substrate.sql:89`
  — `omnivia_durable_jobs (job_id PK, job_type, state, payload_json, created_at,
  updated_at, fencing_generation, claimed_by_service_instance)`.

**Writers.** All writes are fenced through
`omnivia_core_runtime.ownership.fencing.fenced_transaction`:

| Symbol | Path | Role |
| --- | --- | --- |
| `JobState` | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/jobs.py:33` | `queued`, `claimed`, `succeeded`, `failed`, `cancelled` |
| `JobQueue` | `service/jobs.py:131` | generic fenced queue: `enqueue:149`, `claim_next:179`, `complete:261`, `cancel:266`, `fail:275`, `recover_stranded:296` |
| `claim_application_job` | `service/jobs.py:362` | claims one queued application job and appends its running Attempt and event |
| `_terminalize_application_job` | `service/jobs.py:443` | single terminalization path; writes attempt closeout, terminal event and terminal observation |
| `acknowledge_application_job_cancellation` | `service/jobs.py:562` | settles a pending cancellation, preserving its terminal observation |
| `complete_application_job` / `fail_application_job` | `service/jobs.py:655`, `:680` | success / failure terminalization |

`JobQueue.fail` (`service/jobs.py:275`) is the existing bounded-attempt rule: it
requeues until `max_attempts` (`DEFAULT_MAX_ATTEMPTS = 3`, `service/jobs.py:30`)
and then stops at `failed`. `_encode` (`service/jobs.py:95`) packs payload,
attempt count and last error into the single `payload_json` column, explicitly to
avoid a schema migration for scheduler bookkeeping.

**Repository / readers.** `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/jobs.py`:
`read_application_job_snapshot:198`, `read_application_job_events:603`,
`application_job_event_count:637`, `start_import_job:337`,
`request_job_cancellation:457`, `request_job_retry:522`,
`recover_stranded_application_jobs:652`, `read_accepted_import_source:119`.

**Public operation handlers.**
`packages/omnivia-core-runtime/src/omnivia_core_runtime/service/handlers/jobs.py:68-71`
defines `job.get`, `job.cancel`, `job.retry`, `job.events`; they are registered on
the operation registry at `service/application.py:503-506`.

**Durable history (migration 0010, `0010_durable_job_history.sql`).** Six
append-only relations, each with insert/update/delete guard triggers:

| Table | Line | Meaning |
| --- | --- | --- |
| `omnivia_job_application_metadata` | `:8` | job identity, kind, originating operation, audit ref, `max_attempts`, `supports_checkpoint_resume` |
| `omnivia_job_attempts` | `:59` | one immutable Attempt per try; states `running`/`succeeded`/`failed`/`cancelled` |
| `omnivia_job_progress_events` | `:91` | monotonic progress per Attempt |
| `omnivia_job_checkpoints` | `:119` | digest-bound resume checkpoints |
| `omnivia_job_events` | `:149` | contiguous, non-regressing job event stream |
| `omnivia_job_terminal_results` | `:172` | first terminal fact (superseded — see 2.1.1) |

Attempt immutability is already enforced in the substrate:
`omnivia_guard_job_attempts_insert:289` requires contiguous attempt numbers within
the job's budget and refuses a new Attempt on a terminal job;
`omnivia_guard_job_attempts_update:348` freezes identity and start and permits
exactly one terminalization; `omnivia_guard_job_attempts_delete:389` refuses
DELETE outright.

**Job control and terminal-observation bridges (migration 0015,
`0015_application_job_bridges.sql`).**

| Table | Line | Meaning |
| --- | --- | --- |
| `omnivia_application_import_claims` | `:8` | the exact accepted application input |
| `omnivia_job_terminal_observations` | `:53` | repeatable terminal facts with `provenance_kind` and committing fence |
| `omnivia_application_job_controls` | `:114` | every settled `job.cancel` / `job.retry` control, with disposition and resulting state |
| `omnivia_migration_0015_backfill_gate` | `:236` | verify gate for the 0010 → 0015 terminal backfill |

#### 2.1.1 `omnivia_job_terminal_results` is already superseded

Migration 0015 backfills every 0010 terminal fact into
`omnivia_job_terminal_observations` exactly once
(`0015_application_job_bridges.sql:234`), records the missing fence as
`provenance_kind = 'legacy_unrecorded'`, and proves the copy through the
`omnivia_migration_0015_backfill_gate` trigger. No production code in this tree
reads or writes `omnivia_job_terminal_results` — the only remaining references are
migrations `0010`/`0015` and the migration regression tests
(`packages/omnivia-core-runtime/tests/phase3/runtime/test_durable_job_history_migration.py`,
`.../test_v06_5_s3_job_migration.py`). This is the repository's own worked example
of expand → backfill → verify, and it is the pattern RT-102 must follow.

### 2.2 Control-plane `RunRecord`, `RunStepRecord`, `Approval` and the registry

**Models** — `src/omnivia_core/control_plane/models.py`:

| Symbol | Line | Notes |
| --- | --- | --- |
| `ControlPlaneRunStatus` | `:75` | 13 states including `waiting_for_approval`, `approval_required`, `partially_completed` |
| `RunStepStatus` | `:93` | `planned`, `simulated`, `waiting_for_approval`, `completed`, `failed`, `cancelled` |
| `RunStepType` | `:104` | `agent`, `capability`, `approval_wait` |
| `Approval` | `:426` | approval coverage, assignment, escalation and timeout metadata |
| `PolicyDecisionRecord` | `:502` | immutable, display-safe policy decision |
| `RunRecord` | `:473` | **self-described** as a *correlation* record: "the run ledger remains canonical" |
| `RunStepRecord` | `:547` | display-safe step evidence with `attempt`, `cost_units`, `token_usage` |
| `LocalModelInvocationRecord` | `:568` | redacted model-invocation evidence |

`RunRecord` already carries recovery bookkeeping — `resume_token`,
`resume_after`, `resume_reason`, `retry_count` (`models.py:485-488`) — and points
outward at the run ledger through `run_ledger_ref` / `run_ledger_entry_id`
(`models.py:479-480`).

**Registry operations** —
`services/omnivia-memory/src/omnivia_memory/control_plane/registry.py`:
`execute_run:1916`, `cancel_run:2387`, `retry_run:2431`, `resume_run:2520`,
`record_approval_decision:2574`, `assign_approval_request:2719`,
`escalate_approval_request:2826`, `process_approval_timeouts:2897`,
`list_due_runs:3372`, `claim_due_run:3452`, `process_due_runs_once:3574`,
`process_approved_runs_once:3701`, `materialize_run_ledger_entry:3786`,
`_record_run_step:4589`, `_record_approval_wait:4756`, `_update_run_status:4898`,
`_claim_queued_run:4977`, `_record_policy_decision:5091`, `_resume_token:5780`.

**Where it lives, and why that matters.** The registry persists a
`ControlPlaneManifest` (`src/omnivia_core/control_plane/models.py:768`) whose
`run_records` and `approvals` are ordinary mutable lists. Two different write
shapes act on that state, and both destroy the prior value:

- `_update_run_status` (`registry.py:4898`) selects the single
  `control_plane_resources` row for `(workspace_id, 'run_record', run_id)`,
  edits its decoded `payload_json` in memory — status, `updated_at`, and where
  supplied `trace_id`, `retry_count`, `resume_after`, plus
  `_materialize_resume_payload` — and writes it back with
  `UPDATE control_plane_resources SET payload_json = ?, updated_at = ?` against
  that one row (`registry.py:4910-4952`). One row is mutated in place; nothing
  records what the payload was before.
- `_store_manifest` (`registry.py:4211`) upserts the manifest row
  (`ON CONFLICT(workspace_id) DO UPDATE`) and then replaces the resource set
  workspace-wide: `DELETE FROM control_plane_resources WHERE workspace_id = ?`
  followed by a reinsert of every resource in the manifest
  (`registry.py:4216-4258`).

Persistence is `omnivia_memory.persistence.database.Database` (`registry.py:62`,
`:255-256`) — a bare SQLite connection whose schema is asserted at connect time by
`_init_schema` (`database.py:76`). Its API does not establish the mutation-guard,
lease or fencing context that migrations `0002`–`0006` require; that does not by
itself establish which physical database file is used. The file it opens is
whatever `db_path` the caller supplies:
`DatabaseConfig.db_path` is required (`database.py:26`) and `get_database()`
refuses an implicit path outright, T-0629F having removed the
`~/.omnivia/memories.db` fallback precisely because an unversioned global
database can be neither migrated nor fenced (`database.py:603-610`, `:613-635`).
The control-plane models module in the memory service is a compatibility
re-export facade over the Core leaf
(`services/omnivia-memory/src/omnivia_memory/control_plane/models.py:1-17`).

Consequence: control-plane run state has no immutable history, no fence, and no
append-only guard — a run's prior status is overwritten row-wise, and its whole
resource set is deleted and rebuilt on every manifest write. It cannot be
promoted to canonical Runtime state as-is, and this document does not assert that
it is the v0.9.1 canonical Runtime.

#### 2.2.1 `control_plane_manifests` / `control_plane_resources` / `control_plane_events` exist twice

Those three table names are defined twice, with the same column shape, and the
two definitions are maintained by wholly independent mechanisms:

| Copy | Definition | Guards | Written by |
| --- | --- | --- | --- |
| Fenced workspace baseline | `.../migration_files/0000_phase0_baseline.sql:38` (`control_plane_manifests`), `:45` (`control_plane_resources`), `:29` (`control_plane_events`) | `0002_mutation_guard.sql:211,222,233` (resources); `0003_complete_mutation_guard.sql:66,77,88,99,110,121` (events, manifests); rebound to the lease by `0004`/`0005` | nothing — no Python under `packages/omnivia-core-runtime/src/` references any of the three |
| Memory-service store | `services/omnivia-memory/src/omnivia_memory/persistence/database.py:375` (`control_plane_manifests`), `:384` (`control_plane_resources`), `:408` (`control_plane_events`) — `CREATE TABLE IF NOT EXISTS` re-asserted on every connect by `_init_schema` (`:76`) via `_try_execute_schema` (`:454-462`) | none | the registry, through `Database` (`registry.py:62`, `:255-256`) |

The shapes match because they are one lineage, not two designs. The Phase 0
baseline is a frozen extraction of the legacy runtime schema
(`0000_phase0_baseline.sql:1-4`) — that is, of `Database._init_schema` itself.
The migration lane then froze, guarded and versioned its copy while the memory
service kept re-asserting the live one.

**The fenced copies are canonical schema substrate.**
`canonical_schema_tables()` and `canonical_schema_fingerprint()`
(`storage/migrations.py:98`, `:117`) rebuild the baseline plus every migration in
memory, so all three names sit inside the fingerprint that readiness verification
compares a workspace against. Renaming or dropping one is a fingerprint change,
not a cleanup.

**They are also, in this tree, schema and not state** — the table's fourth
column is empty for a reason. The fenced copies must not be read as evidence that
canonical Agent Runtime state already lives on the fenced substrate. It does not.

**Which file the registry opens is undecided, not merely unfenced.**
`Database` opens the path its caller supplies (§2.2), and at this commit nothing
outside `services/omnivia-memory/tests/`, `baseline/` and `benchmarks/`
constructs a `Database` or a `ControlPlaneRegistry` at all. So the registry is
either pointed at a separate file, where no guard governs its writes, or at the
workspace database, where the 0002/0003 guards would abort every one of them —
their predicate requires a `omnivia_mutation_guard.fencing_generation` matching
`omnivia_workspace_state` (`0002_mutation_guard.sql:213-217`), which the registry
never establishes. Neither arrangement has been chosen and shipped.

**Do not read the substrate as satisfying a no-second-store invariant for
control-plane state.** Two definitions of the same three relations are maintained
by two mechanisms with no agreed authority between them. That does not prove two
physical stores, but the unresolved `db_path` and incompatible write context also
do not prove one authoritative store. Gate 0 records that; it changes nothing.

**Cutover implication for RT-102, or for whichever later lane owns the
control-plane projection.** That lane must, in order:

1. Choose exactly one authority and write path for control-plane state — the
   fenced workspace substrate under its lease and mutation guard, or a separately
   configured memory-service database. Not both. If the registry is configured
   against the workspace file, reconcile the competing schema ownership and make
   every write participate in the fenced transaction context.
2. If facts exist in the representation that loses, migrate and backfill them
   under the §9 expand → backfill → verify → cut over → observe → remove
   sequence, recording provenance honestly where the losing schema could not
   carry it. If it holds no facts, say so and skip straight to retirement rather
   than leaving it standing unexplained.
3. Retire the duplicate representation, or repurpose it for a different and
   explicitly named relation. Two live representations of the same facts is not
   an acceptable end state.

No permanent dual writes (§9), and no third store: this must not be answered by
adding a new control-plane database alongside the two definitions that already
exist.

### 2.3 `RunLedgerEntry`

`src/omnivia_core/run_ledger/models.py:79`, with `RunLedgerStatus:16`,
`RUN_LEDGER_CONTRACT_VERSION = 1.0` (`:12`) and
`RUN_LEDGER_PATH_ENV = "OMNIVIA_RUN_LEDGER_PATH"` (`:13`). Spec:
`docs/specs/run-ledger-contract.md`.

Its fields are `run_id`, `task_id`, `target_repo`, `lane_id`, `status`,
`started_at`, `updated_at`, `completed_at`, `evidence_file_refs`, `provenance`,
`contract_version`. `target_repo` and `lane_id` make the domain unambiguous: this
is the **agent-lane / PM tooling ledger**, an export format for who ran what task
against which repository. It is not an execution ledger for an Agent Component.

Only writer: `registry.materialize_run_ledger_entry:3786`, which projects a
control-plane `RunRecord` into a ledger-shaped entry for append/export. Readers
are the run-ledger validators and the memory service tests.

**The collision is nominal only.** `RunLedgerEntry.run_id` and a canonical
Runtime `Run.id` are different identifiers in different domains and must never be
joined.

## 3. Status assignment

| Representation | Status | Rationale |
| --- | --- | --- |
| `omnivia_durable_jobs` + `JobQueue` (`0001_ownership_substrate.sql:89`, `service/jobs.py:131`) | **Canonical substrate** | The only fenced, lease-bound, recovery-aware scheduler in Core. Extend it; do not replace it. |
| `omnivia_job_application_metadata`, `omnivia_job_attempts`, `omnivia_job_progress_events`, `omnivia_job_checkpoints`, `omnivia_job_events` (0010) | **Canonical substrate** | Already immutable, contiguous and guard-enforced. Direct precursors of Attempt and RuntimeEvent. |
| `omnivia_application_import_claims`, `omnivia_job_terminal_observations`, `omnivia_application_job_controls` (0015) | **Canonical substrate** | Provenance-qualified, fence-recorded, repeatable terminal and control history. |
| `omnivia_job_terminal_results` (0010:172) | **Retirement target** | Superseded by `omnivia_job_terminal_observations`; backfilled and verified by 0015; no production reader or writer remains. Remove only under the rules in §9. |
| `control_plane_manifests`, `control_plane_resources`, `control_plane_events` in the fenced baseline (`0000_phase0_baseline.sql:38`, `:45`, `:29`) | **Canonical schema substrate, no writer** | Guard-enforced (0002/0003, rebound by 0004/0005) and inside `canonical_schema_fingerprint()` (`storage/migrations.py:117`), so they cannot be renamed or dropped casually. Nothing under `packages/omnivia-core-runtime/src/` reads or writes them: schema, not state, and **not** canonical Agent Runtime state. See §2.2.1. |
| The identically named `control_plane_manifests`, `control_plane_resources`, `control_plane_events` re-asserted by the memory-service schema path (`persistence/database.py:375`, `:384`, `:408`) | **Duplicate schema/access path, authority undecided** | Same lineage and the only access path the registry uses. It is unguarded in a separate database; against the workspace database its writes lack the required context and are rejected. Reconcile the two mechanisms under §2.2.1 step 1, with backfill and retirement under §9 only if two factual representations exist. Do not claim a no-second-store invariant until the configured authority proves it. |
| Control-plane `RunRecord` (`control_plane/models.py:473`) | **Compatibility projection** | Self-declared correlation record on an unfenced, mutable store. Becomes a read-only projection of canonical `Run`; never the authority. |
| Control-plane `RunStepRecord` (`:547`) | **Compatibility projection** | Same store, same mutability. Projects from canonical `RunStep` + `Attempt`. |
| Control-plane `Approval` (`:426`) | **Migration source** | Carries real approval semantics (assignment, escalation, timeout, expiry) that canonical `Approval` must absorb. |
| `RunRecord.resume_token` / `resume_after` / `resume_reason` and `registry.resume_run:2520` | **Retirement target** | Local-only resume bookkeeping. Superseded by canonical `Wait` + `ResolveWait`. Never surfaced on the job wire. |
| Control-plane `PolicyDecisionRecord` (`:502`) | **Migration source** | Precursor to `PolicySnapshot`; already immutable and display-safe. |
| `LocalModelInvocationRecord` (`:568`) | **Fixture-only** | Redacted planning-step evidence. Useful as conformance input; not a canonical record. |
| `RunLedgerEntry` (`run_ledger/models.py:79`) | **Fixture-only / out of domain** | Agent-lane PM ledger. Unchanged by the Runtime work. Name collision only. |

## 4. Mapping to the canonical Runtime records

Identity rule for the whole table: every canonical record is `(workspace_id,
<record>_id)`. `omnivia_durable_jobs.job_id` is globally unique but **not**
workspace-scoped (`0001_ownership_substrate.sql:89`); every 0010/0015 relation
adds `workspace_id` and binds it to the singleton workspace inside its insert
guard. Canonical Runtime records follow the 0010/0015 shape, not the bare
scheduler shape.

| Canonical record | Nearest existing representation | Mapping | Gap RT-101/RT-102 must close |
| --- | --- | --- | --- |
| `Run` | `omnivia_durable_jobs` + `omnivia_job_application_metadata` (0010:8) | job identity, kind, originating operation, audit ref, attempt budget | No workspace-scoped Run identity; no Agent Component / Workflow definition ref; no budget or policy binding |
| `RunStep` | *(none)* — closest is control-plane `RunStepRecord` (`:547`) | step type, ordinal, status | Entirely new. `RunStepRecord` is a projection, not a source of truth |
| `Attempt` | `omnivia_job_attempts` (0010:59) | contiguous `attempt_number`, `started_at_us`, single terminalization, immutable identity (`:348`, `:389`) | Attempts hang off a job, not a `RunStep` |
| `Wait` | `omnivia_job_checkpoints` (0010:119) for the durable-suspension half; control-plane `approval_wait` step (`RunStepType.APPROVAL_WAIT`, `models.py:109`) and `_record_approval_wait:4756` for the approval half | checkpoint digest + resume evidence | No first-class durable Wait record, no wait kind, no `ResolveWait` command |
| `Approval` | control-plane `Approval` (`:426`), `record_approval_decision:2574`, `assign_approval_request:2719`, `escalate_approval_request:2826`, `process_approval_timeouts:2897` | approver role, actor, decision, comment, timeout, escalation, expiry | Mutable and unfenced; needs immutable request/decision pair bound to a `Wait` |
| `CapabilityGrant` | control-plane `Policy.capability_ids` / `Automation` (`:366`, `:455`) | which callable a definition may invoke | Grants are static manifest wiring, not per-Run issued grants |
| `PolicySnapshot` | `PolicyDecisionRecord` (`:502`) | immutable decision + reason code + audit ref | Records one decision, not the monotonic snapshot a Run is pinned to |
| `BudgetSnapshot` | `Policy.max_cost_units` / `max_token_usage` (`:378`), `RunStepRecord.cost_units` / `token_usage` (`:562`) | limits and consumption counters | No snapshot pinned at admission |
| `EffectIntent` | *(none)* — `omnivia_application_job_controls` (0015:114) is the nearest shape | a settled control with disposition and resulting state | No pre-effect intent record. "No effect before intent" is unenforceable today |
| `EffectReceipt` | `omnivia_mutation_executions` (`0013_mutation_execution_records.sql:68`) | executed mutation evidence | Not correlated to an intent |
| `EffectSettlement` | `omnivia_job_terminal_observations` (0015:53) | provenance-qualified, fence-recorded, repeatable terminal fact | Settles a job, not an individual effect |
| `RuntimeEvent` | `omnivia_job_events` (0010:149) | contiguous sequence, non-regressing time, state-matched to the scheduler | Scoped to a job and restricted to five job states |
| `Artifact` | `omnivia_blob_objects` / `omnivia_staged_sources` (`0008_blobs_staged_sources_and_evidence.sql:159`, `:300`) | content-addressed local blobs | Not bound to a Run |
| `EvidenceItem` | `omnivia_evidence_artifacts` (`0008:465`) with `omnivia_evidence_provenance_events` (`0008:709`), projected into `omnivia_evidence_search_documents` (`0012_evidence_search_projection.sql:44`) | bounded local evidence capture | Not bound to a Run |
| `CleanupReceipt` | *(none)* | — | Entirely new; "observable cleanup" has no representation |

State-vocabulary reconciliation:

- Scheduler (`JobState`, `service/jobs.py:33`): `queued`, `claimed`, `succeeded`,
  `failed`, `cancelled`. `claimed` is the scheduler's word for `running` — the
  event guard translates it explicitly
  (`0010_durable_job_history.sql:551`: `CASE NEW.state WHEN 'running' THEN 'claimed'`).
- Attempt (`0010:76`): `running`, `succeeded`, `failed`, `cancelled`.
- Control plane (`ControlPlaneRunStatus`, `models.py:75`): 13 values, including
  three that the job substrate has no equivalent for — `waiting_for_approval`,
  `approval_required`, `partially_completed`.

Canonical `Run` status must be a **new, Core-owned, closed-at-the-schema /
open-on-the-wire enum**, not any of these three. RT-101 owns it. Neither
`JobState` nor `ControlPlaneRunStatus` may be promoted in place: the first cannot
express waiting, and the second is not durable.

## 5. Wire compatibility (binding)

1. **`job.retry` is retained unchanged.** It is the single recovery operation on
   the public application wire
   (`src/omnivia_core/contracts/v1/semantics_jobs.py:23-27`,
   `JOB_RETRY_OPERATION:419`, catalogue entry in
   `contracts/application/v1/schemas/operations.schema.json:523`; line 6 only
   opens the `x-omnivia-operation-catalogue` array).
2. **No `job.resume` is added, ever.** There is no `job.resume` operation and no
   `JobControl.resume` member, by frozen contract
   (`semantics_jobs.py:26`, `src/omnivia_core/contracts/v1/generated.py:3850-3851`).
   Whether recovery restarts a failed job or continues a cancelled one from a
   checkpoint is chosen from server state and *reported* — never selected on the
   wire. The `resume_scheduled` value in
   `omnivia_application_job_controls.disposition`, declared by the `job.retry`
   CHECK at `0015_application_job_bridges.sql:163-165`, is a **disposition of
   `job.retry`**, not a second operation. Do not read it as evidence that a
   resume operation exists.
3. **`ResolveWait` is a distinct Runtime command, not job recovery.** It resolves
   exactly one durable `Wait` on a canonical `Run`. It is not `job.retry`, does
   not requeue a job, and does not belong to the application job family. It must
   not be expressed as a `JobControl` member.
4. **The frozen 20-operation catalogue is unchanged by Gate 0.** Contract version
   stays `1.3` (`contracts/application/v1/schemas/application-v1.schema.json`).
   Runtime commands, when they arrive, are a separate namespace decision for
   RT-101 — not an in-place edit of the job family.
5. **`registry.resume_run:2520` is local-only and stays local-only.** It is not
   an operation in the catalogue and must never be promoted onto the wire under
   any name.

## 6. Contract source recommendation

Follow the existing Core pattern exactly; introduce nothing new.

1. **Checked-in JSON Schema is the single canonical source.**
   `contracts/application/v1/schemas/*.schema.json`, Draft 2020-12, with
   `x-omnivia-contract-version` and the `x-omnivia-operation-catalogue`
   annotation as the only catalogue
   (`contracts/application/v1/schemas/operations.schema.json:5-6`).
2. **Generate both bindings from that source, and commit them.**
   `scripts/generate-application-contracts.py` emits
   `src/omnivia_core/contracts/v1/generated.py` and
   `generated/typescript/application/v1/index.ts` deterministically, from the
   schemas only, and raises `UnsupportedSchemaError` on any shape outside its
   supported subset rather than guessing.
3. **Keep the tolerant/strict split.** Generated `from_wire` decoders ignore
   unknown fields and preserve unknown open string values; pattern, length and
   range enforcement is JSON Schema's job via
   `scripts/check-application-contracts.py`. Composite and cross-field
   invariants that a schema cannot express stay hand-maintained in
   `src/omnivia_core/contracts/v1/semantics_*.py` — for Runtime that means a new
   `semantics_runtime.py` sibling, in the same pure, standard-library-only
   style as `semantics_jobs.py`.
4. **Drift is a hard gate.** `--check` on the generator plus
   `npm run check:application-contracts`, both already wired into
   `scripts/preflight`.
5. **Fixtures are part of the contract.** Add Runtime conformance fixtures under
   `contracts/application/v1/fixtures/` with manifest entries, matching the
   existing declared-validity + declared-semantic-expectation shape.

Recommendation: RT-101 adds Runtime definitions as **new schema documents**
alongside the existing ones rather than editing `jobs.schema.json`. The job
family is frozen and its semantics are load-bearing; a new document keeps the
frozen surface byte-stable and keeps the Runtime domain reviewable on its own.

## 7. Migration head and the additive migration

**Correction to the task packet: the next migration is `0018`, not `0012`.**
`0012_evidence_search_projection.sql` has existed since the evidence-search
projection landed.

Verified head on this branch —
`packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/migration_files/`:

```
0000_phase0_baseline.sql   (baseline artefact, not a migration)
0001 … 0016
0017_connector_sync_state.sql   <- head
```

`load_migrations()`
(`packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/migrations.py:147`)
discovers every `NNNN_*.sql` in the package except the Phase 0 baseline, orders
by integer prefix, and fails on duplicate versions. There is a separate,
independently versioned installation lane
(`storage/installation_migration_files/0001_installation_authority.sql`) — do not
number against it.

**Therefore: RT-102 adds `0018_<name>.sql` to
`storage/migration_files/`.** This document does not create it.

Rules the file must follow, all of which the existing head already demonstrates:

- Additive only. `CREATE TABLE` for new relations; `ALTER TABLE … ADD COLUMN`
  with an inline `CHECK` for new columns on existing relations
  (`0011_projection_lifecycle.sql:8-55`). No table rewrite, no destructive
  redefinition of a shipped relation.
- Earlier migrations stay byte-immutable. 0015 states this explicitly for 0010
  (`0015_application_job_bridges.sql:5`) and replaces only the three specific
  INSERT guards it had to.
- Every new relation is workspace-scoped, `WITHOUT ROWID`, with typed `CHECK`
  constraints, `*_at_us` signed-64-bit microsecond timestamps, and
  `sha256:<64 lowercase hex>` digest columns
  (`0007_application_audit_and_idempotency.sql:28-37`).
- Every INSERT carries the full service-writer / mutation-guard /
  workspace-state / lease predicate established by
  `0005_require_connection_authority.sql`; every UPDATE and DELETE on history
  aborts unconditionally, including for the current fenced owner.
- Contiguous sequences and non-regressing time are enforced in the trigger, not
  in Python (`0010_durable_job_history.sql:535-546`).

## 8. Dependency direction and forbidden imports

Verified current state (`scripts/check-package-boundaries.py`,
`tests/test_package_boundaries.py`):

```
                       omnivia-core
                            ▲
      ┌──────────────┬──────┴───────┬──────────────┐
      │              │              │              │
omnivia-core-  omnivia-core-  omnivia-core-  omnivia-core-
   runtime          mcp            cli          client
                     │              │              ▲
                     └──────────────┴──────────────┘
```

Arrows point at the dependency target. All four siblings depend on
`omnivia-core`; `omnivia-core-mcp` and `omnivia-core-cli` additionally depend on
and import `omnivia-core-client`. Those two edges are permitted, in use today,
and load-bearing — omitting them would read as a prohibition the boundary
checker does not impose.

Allowed edges, exactly as enforced:

| Edge | Enforcement / current use |
| --- | --- |
| `omnivia-core-runtime`, `omnivia-core-mcp`, `omnivia-core-cli`, `omnivia-core-client` → `omnivia-core` | `check_siblings_depend_on_core`: each must declare `omnivia-core>=0.1.0,<0.2.0` |
| `omnivia-core-cli` → `omnivia-core-client` | `packages/omnivia-core-cli/pyproject.toml:14`; `packages/omnivia-core-cli/src/omnivia_core_cli/dispatch.py:51` |
| `omnivia-core-mcp` → `omnivia-core-client` | `packages/omnivia-core-mcp/pyproject.toml:24`; `packages/omnivia-core-mcp/src/omnivia_core_mcp/configuration.py:30` |

Binding rules for the Runtime work:

- `omnivia-core-runtime` → `omnivia-core`, never the reverse. `omnivia_core`
  must not depend on or import any sibling — `omnivia_core_runtime`,
  `omnivia_core_mcp`, `omnivia_core_cli`, `omnivia_core_client` — or any legacy
  package (`check_core_has_no_sibling_or_legacy_dependency`,
  `check_core_has_no_sibling_or_legacy_import`).
- `omnivia-core-client` declares exactly one dependency, the accepted
  `omnivia-core` range, and imports no runtime, MCP, CLI or legacy package
  (`check_client_depends_only_on_core`).
- `omnivia-core-mcp` and `omnivia-core-cli` must not depend on or import
  `omnivia_core_runtime` or each other
  (`check_adapters_do_not_depend_on_runtime_or_each_other`,
  `ADAPTER_FORBIDDEN_SIBLINGS`). `omnivia-core-client` is deliberately absent
  from that forbidden set: the existing CLI → client and MCP → client edges are
  permitted and stay.
- No Core distribution may depend on or import Apps or Pro
  (`check_no_apps_or_pro_dependency_or_import`).
- `src/omnivia_core/contracts/` imports **nothing** outside the standard library
  and its own package — no runtime, storage, HTTP, MCP, CLI, Platform, Dev or
  validation-framework dependency. Enforced by
  `scripts/check-application-contracts.py`. Canonical Runtime contracts live
  under this restriction.
- Core must not define, import or reference Platform, Electron, provider, ACP,
  MCP-client, sandbox or worker-host implementation types. The private Platform
  worker host owns adapters, workspace/sandbox/secret bindings and privileged
  dispatch. It **never** owns canonical `Run` state.
- Platform's App-facing `OmniViaRuntime` remains an anti-corruption facade over
  Core's transport-neutral contracts.
- Core's runtime substrate must not import `omnivia_core.control_plane` or
  `omnivia_core.run_ledger`. Neither is imported by
  `packages/omnivia-core-runtime/src/` today; keep it that way, so the
  compatibility projection can never become a write path.
- No new SQLite file, no new scheduler, no second job queue. Runtime persistence
  extends the existing fenced workspace database through
  `storage/migration_files/`. This is a rule for the Runtime work, not a
  description of today: control-plane state already has duplicate schema/access
  paths and an unresolved storage target (§2.2.1). Closing that ambiguity is the
  later lane's job; adding another store is not.

## 9. Cutover rules

Every retirement follows six phases in order, and each phase is a separately
mergeable change:

1. **Expand** — add the new relation or column, additively, in `0018+`. Nothing
   reads it yet.
2. **Backfill** — copy existing facts exactly once, recording provenance
   honestly where the old schema could not carry it (0015 writes
   `provenance_kind = 'legacy_unrecorded'` and a NULL fence rather than
   inventing one: `0015_application_job_bridges.sql:211-234`).
3. **Verify** — prove the copy in the migration itself, with a gate that aborts
   on any count or payload divergence
   (`omnivia_migration_0015_backfill_gate`, `0015:236`).
4. **Cut over** — move readers to the new relation in one change. Writers move
   with them.
5. **Observe** — ship at least one release with no reader and no writer on the
   old relation before touching it.
6. **Remove** — drop the relation only after (5), in its own migration.

**Permanent dual writes are prohibited.** A dual-write window exists only between
phases 1 and 4 and must be closed in the same lane that opened it. If a
representation still has two writers at the end of a lane, the lane is not done.

Applied to what exists now:

- `omnivia_job_terminal_results` is at phase 5. It has no production reader or
  writer; only migrations `0010`/`0015` and migration regression tests reference
  it. Its removal is a standalone migration and is **not** part of RT-101 or
  RT-102.
- Control-plane `RunRecord` / `RunStepRecord` enter phase 1 when canonical `Run`
  and `RunStep` exist. They become read-only projections at phase 4 and are
  never dual-written.
- `RunRecord.resume_token` / `resume_after` / `resume_reason` and
  `registry.resume_run` are retired once canonical `Wait` + `ResolveWait` land.
- The duplicated `control_plane_manifests` / `control_plane_resources` /
  `control_plane_events` definitions (§2.2.1) are at phase 0: no authority has
  been chosen, so no phase has started. Choosing one is step 1 of §2.2.1 and is a
  precondition for running this sequence, not a step within it.

## 10. Exit criteria for the later lanes

### RT-101 — canonical Runtime schema, enums and semantic validation

1. New JSON Schema document(s) under `contracts/application/v1/schemas/`
   defining `Run`, `RunStep`, `Attempt`, `Wait`, `Approval`, `CapabilityGrant`,
   `PolicySnapshot`, `BudgetSnapshot`, `EffectIntent`, `EffectReceipt`,
   `EffectSettlement`, `RuntimeEvent`, `Artifact`, `EvidenceItem`,
   `CleanupReceipt`.
2. A Core-owned canonical Run status enum, distinct from both `JobState`
   (`service/jobs.py:33`) and `ControlPlaneRunStatus`
   (`control_plane/models.py:75`), able to express waiting and partial
   completion, open on the wire and fail-safe on unrecognized values.
3. `scripts/generate-application-contracts.py` emits the Python and TypeScript
   bindings from those schemas with no generator special-casing; `--check` and
   `npm run check:application-contracts` are clean.
4. `src/omnivia_core/contracts/v1/semantics_runtime.py` — pure, standard-library
   only, no runtime/storage/transport import — encoding the invariants a schema
   cannot: immutable history, exact and source-qualified correlation, immutable
   Attempts, no effect before intent, uncertainty is not failure, stable logical
   idempotency, monotonic policy, cancellation preserves evidence, observable
   cleanup, subordinate external logs, discovery is not authority.
5. Conformance fixtures under `contracts/application/v1/fixtures/` with manifest
   entries, covering at minimum one replay case and one cancellation-preserves-
   evidence case.
6. `job.retry` unchanged; no `job.resume`; `ResolveWait` defined outside the job
   family. Existing job fixtures byte-stable.
7. Zero schema or persistence change. RT-101 writes no SQL.
8. `./scripts/preflight` green.

### RT-102 — additive persistence migration and repositories

1. `storage/migration_files/0018_<name>.sql` (confirm the head again at the time
   — do not assume 0018 if another lane has landed since), additive only,
   workspace-scoped, `WITHOUT ROWID`, typed `CHECK`s, `*_at_us` timestamps,
   full guard predicate on INSERT, unconditional abort on UPDATE/DELETE of
   history.
2. Migrations `0000`–`0017` byte-unchanged.
3. Where any existing fact is carried forward, a backfill plus a
   `omnivia_migration_0018_*_gate`-style verify trigger in the same file.
4. Repositories in
   `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/`, using
   `fenced_transaction` for every write, in the shape of `storage/jobs.py`.
5. `Run` reuses the existing `omnivia_durable_jobs` scheduler for admission and
   claiming. No second queue, no second SQLite file, no new scheduler loop.
6. Replay/idempotency proven against the existing scoped-claim relations
   (`omnivia_idempotency_claims`, `omnivia_idempotency_outcomes`,
   `0007_application_audit_and_idempotency.sql:161`, `:196`) rather than a new
   mechanism.
7. Migration tests in `packages/omnivia-core-runtime/tests/phase3/runtime/`
   proving forward application from the 0017 head, guard refusal on every
   history UPDATE/DELETE, and backfill equivalence.
8. No public wire change. Contract version still `1.3` unless RT-101 moved it.
9. `./scripts/preflight` green.

## 11. Evidence index

All paths are repository-relative. Line numbers are as of
`origin/main` `9107bf1168d1f965f626f5371380d79410c28701`.

| Claim | Evidence |
| --- | --- |
| Scheduler row shape | `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/migration_files/0001_ownership_substrate.sql:89` |
| Fenced queue operations | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/jobs.py:131-323` |
| Application-job terminalization | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/jobs.py:443-553` |
| Application-job repository | `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/jobs.py:198,337,457,522,603,652` |
| Public job operations | `packages/omnivia-core-runtime/src/omnivia_core_runtime/service/handlers/jobs.py:68-71`; `service/application.py:503-506` |
| Durable job history | `.../migration_files/0010_durable_job_history.sql:8,59,91,119,149,172` |
| Attempt immutability guards | `.../0010_durable_job_history.sql:289,348,389` |
| Job control + terminal observations | `.../0015_application_job_bridges.sql:8,53,114` |
| Backfill and verify gate | `.../0015_application_job_bridges.sql:211-234,236` |
| Projection lifecycle additive `ALTER` pattern | `.../0011_projection_lifecycle.sql:8-55` |
| Audit / idempotency substrate | `.../0007_application_audit_and_idempotency.sql:102,161,196` |
| Migration discovery and ordering | `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/migrations.py:42,147` |
| Canonical schema table set and fingerprint | `packages/omnivia-core-runtime/src/omnivia_core_runtime/storage/migrations.py:98,117` |
| Fenced `control_plane_*` tables (schema, no writer) | `.../migration_files/0000_phase0_baseline.sql:1-4,29,38,45`; guards `0002_mutation_guard.sql:211,213-217,222,233`, `0003_complete_mutation_guard.sql:66,77,88,99,110,121`, rebound by `0004`/`0005` |
| Unfenced `control_plane_*` tables the registry writes against | `services/omnivia-memory/src/omnivia_memory/persistence/database.py:76,375,384,408,454-462` |
| No implicit database path; no production `Database` / `ControlPlaneRegistry` wiring | `services/omnivia-memory/src/omnivia_memory/persistence/database.py:26,603-610,613-635`; construction sites are limited to `services/omnivia-memory/tests/`, `baseline/`, `benchmarks/` |
| Control-plane models | `src/omnivia_core/control_plane/models.py:75,93,104,109,426,473,479-480,485-488,502,547,568,768` |
| Control-plane registry operations | `services/omnivia-memory/src/omnivia_memory/control_plane/registry.py:1916,2387,2431,2520,2574,3786,4589,4756,4898,5780` |
| Registry persistence mechanics | `services/omnivia-memory/src/omnivia_memory/control_plane/registry.py:62,255-256` (memory-service `Database`), `:4211,4216-4258` (`_store_manifest` workspace-wide delete/reinsert), `:4898,4910-4952` (`_update_run_status` single-row in-place JSON update) |
| Memory-service model facade | `services/omnivia-memory/src/omnivia_memory/control_plane/models.py:1-17` |
| Run-ledger contract | `src/omnivia_core/run_ledger/models.py:12,16,79`; `docs/specs/run-ledger-contract.md` |
| Single recovery operation, no `job.resume` | `src/omnivia_core/contracts/v1/semantics_jobs.py:23-27,417-419`; `src/omnivia_core/contracts/v1/generated.py:3850-3851`; `job.retry` catalogue entry at `contracts/application/v1/schemas/operations.schema.json:523` |
| `resume_scheduled` is a `job.retry` disposition | `.../0015_application_job_bridges.sql:163-165` |
| Frozen 20-operation catalogue annotation | `contracts/application/v1/schemas/operations.schema.json:5-6` (line 6 opens the array; entries follow) |
| Contract generation and drift gate | `scripts/generate-application-contracts.py`; `scripts/check-application-contracts.py`; `scripts/preflight` |
| Package boundary rules | `scripts/check-package-boundaries.py`; `tests/test_package_boundaries.py` |
| Allowed CLI/MCP → client edges | `packages/omnivia-core-cli/pyproject.toml:14`, `.../src/omnivia_core_cli/dispatch.py:51`; `packages/omnivia-core-mcp/pyproject.toml:24`, `.../src/omnivia_core_mcp/configuration.py:30` |

No test was executed to produce any claim in this document. Every statement above
is read from the tree at the commit named in this section. The verification
commands actually run for this lane are reported separately to Codex.

## 12. Known stale artefact outside this lane's allowed writes

`packages/omnivia-core-runtime/src/omnivia_core_runtime/__init__.py:1-7` still
describes the distribution as a "skeleton runtime distribution" that "has no
operational behavior yet" and instructs the reader not to add storage,
service-launch, lease or lifecycle behaviour. All four now exist in the package.
That docstring is Python source and is outside this lane's allowed writes. It
needs a separate, Codex-authorised correction.
