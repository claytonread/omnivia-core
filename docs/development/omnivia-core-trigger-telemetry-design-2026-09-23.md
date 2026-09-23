# Trigger Telemetry Design (C21-A, lane 1)

Status: **design for founder review** (Decision 4A). Nothing here is implemented.
Migration `0043_runtime_trigger_telemetry.sql` is **reserved** in
`contracts/migrations/v1/allocations.json`, owner Workflow Runtime. Its SQL stays
absent until this design is accepted and a later lane advances the allocation to
`candidate`.

Path abbreviations:

- `RT/` = `packages/omnivia-core-runtime/src/omnivia_core_runtime/`
- `MIG/` = `RT/storage/migration_files/`
- `LEG/` = `services/omnivia-memory/src/omnivia_memory/` (the ADR-036 legacy facade; see `pyproject.toml:65-66`)

DEV-REQ-112 is not defined anywhere in this repo. It is paraphrased here from the
task packet as six needs:

1. subscription state
2. last observation
3. delivery status
4. processing status
5. failures
6. uncertainty

---

## 0. Headline finding

**The canonical runtime has no live trigger today.** Nothing in `RT/` wakes on a
timer, a webhook, a watch or a subscription:

- **The service loop only renews the lease and watches for signals.** The code
  says so outright: "why there is no scheduler here" (`RT/service/main.py:532-536`).
- **The runtime scheduler is a claim seam, not a timer.** `RuntimeScheduler.claim_next`
  (`RT/service/runtime_scheduler.py:250`) has no production caller. The only
  automatic use is startup recovery (`RT/service/runner.py:564-567`).
- **Connectors may not schedule themselves.** A connector that declares a
  scheduling interval is refused with "the coordinator is the only execution owner"
  (`src/omnivia_core/connector/host.py:153-156`; `RT/service/ingestion_coordinator.py:544-548`).
  `synchronise` / `synchronise_spi` (`RT/service/ingestion_coordinator.py:415`, `:534`)
  have no production caller.
- **Trigger declarations exist only as models.** `TriggerKind`, `Trigger`,
  `TriggerEventEnvelope` and `TriggerIngestionResult` are defined at
  `src/omnivia_core/control_plane/models.py:49-56`, `:309-321` and `:521-544`,
  but are only validated there. The one place they are actually ingested is the
  legacy facade (§1, T4).

So this design has two jobs:

- record telemetry for the trigger-shaped paths that do exist;
- reserve a store that a real ingestion seam can write to later.

It does not invent a scheduler. Whether Core should grow one is open question Q1.

---

## 1. Trigger taxonomy (what Core actually has)

| # | Trigger | Stimulus | Entry point | Durable today | Live in canonical runtime? |
|---|---|---|---|---|---|
| T1 | **API-started work** (`import.start`, workflow start) | Caller request | `RT/service/handlers/jobs.py:141` → `RT/storage/jobs.py:382` `start_import_job`; `RT/service/handlers/workflow.py:1410`/`:1468` → `_insert_workflow_job` `:1512` | Yes: `omnivia_durable_jobs` (`MIG/0001_ownership_substrate.sql:89-98`) plus the 0010/0015 job ledgers | Yes |
| T2 | **Connector poll** (pull, coordinator-owned) | Coordinator call | `RT/service/ingestion_coordinator.py:534` `synchronise_spi` (and `:415` legacy `synchronise`) | Yes, in 0017: `omnivia_connector_sync_runs`, `omnivia_connector_dead_letters`, `omnivia_connector_health_events` (`MIG/0017_connector_sync_state.sql:48-85`, `:101-150`, `:161-191`) | Code exists; **no production driver** |
| T3 | **Runtime wait resolution** (`external_signal`, `timer`; approval is out of scope) | External `workflow.control` `resolve_wait` | `RT/service/handlers/workflow.py:629-653` → `:1021` → `RT/service/runtime_waits.py:280` `resolve_runtime_wait` | Yes: `omnivia_runtime_waits` / `omnivia_runtime_wait_resolutions` (`MIG/0018_agent_runtime_records.sql:350-390`, `:402-`), kinds CHECK at `:378` | Resolution yes. **No production code opens waits**: `open_runtime_wait` (`RT/service/runtime_waits.py:203`) is called only from tests. **Nothing ever fires `timer_expiry`.** |
| T4 | **Declared control-plane trigger** (`manual`, `schedule`, `webhook`, `cloudevent`, `catalogue_event`) | Envelope delivery, or a one-shot schedule pass | Legacy only: `LEG/control_plane/registry.py:1668` `ingest_trigger_event`, `:1853` `materialize_due_schedule_triggers_once` | Legacy only: `control_plane_events` rows `trigger.accepted` / `trigger.duplicate` / `trigger.dead_letter` (`LEG/control_plane/registry.py:4649`, `:4513-4538`). The same table also exists in the runtime baseline (`MIG/0000_phase0_baseline.sql:29`), but nothing in `RT/` writes it | **No** |

Not triggers:

- **Lease heartbeat** (`RT/service/runner.py:608` `renew_lease_if_due`). It is internal housekeeping.
- **Chat waits** (`MIG/0033_chat_compaction_waits_agent_runs.sql:90-138`) and the **agent-run mailbox** (`:183-210`). Their functions have no production caller, and they are chat-owned.
- **`trigger_message_id`** (`MIG/0034_chat_generation_text_transport_events.sql:37`). It is a chat message id.
- **MCP notifications.** These are off (`packages/omnivia-core-mcp/src/omnivia_core_mcp/server.py:1388-1394`).

**Proposal.** Telemetry covers **T3 and T4**:

- **T1** is already fully described by the job ledgers.
- **T2** already has its own append-only telemetry in 0017. Its read-back reuses it (§6) rather than duplicating it.

T4 is the kind that has a "subscription" (a declared trigger with a lifecycle).
`trigger_kind` in the new store is the `TriggerKind` enum plus `wait_signal` and
`wait_timer` for T3.

---

## 2. Ingestion path

There is one writer discipline, the **fenced service writer**:

- `fenced_transaction` (`RT/ownership/fencing.py:281`) runs `BEGIN IMMEDIATE` and
  checks authority on entry and again before `COMMIT`.
- Guard triggers reject writes that lack the service writer or lease (for example
  `MIG/0010_durable_job_history.sql:250-261`).
- Application operations reach it through `execute_mutation` (`RT/service/mutation.py:662`).

The rule: **a telemetry row is written in the same fenced transaction as the
state change it describes.** That makes telemetry and truth unable to diverge.
The same rule already governs job events (`MIG/0010_durable_job_history.sql:547`: an event must
match the scheduler row) and the 0017 writers (`RT/storage/connectors.py:3-7`).

| Trigger | Owning writer / transaction |
|---|---|
| T3 | `resolve_runtime_wait`'s fenced transaction, next to `writer.close_wait(...)` (`RT/service/runtime_waits.py:387-393`) |
| T4 | A **new** application operation (working name `trigger.ingest`) through `execute_mutation`. It would port the legacy decision sequence (`LEG/control_plane/registry.py:1680-1851`: dedupe, lifecycle, event-type, cooldown/debounce, automation, concurrency) and, on acceptance, enqueue a `workflow.execute` job via `_insert_workflow_job` (`RT/service/handlers/workflow.py:1512`) in the same transaction. **This operation does not exist yet.** It is the next implementation lane, subject to Q1 and Q2. |

**Record shape needed for DEV-REQ-112.** One row per stimulus received, carrying:

- the trigger identity;
- its occurrence identity (`event_id` and `idempotency_key`);
- the source time if known;
- the time Core observed it;
- the delivery decision (accepted, duplicate, dead-lettered) with a reason code;
- a link to the job or run it produced.

Processing status and failures are **not copied**. They are read through that
link from the existing job and run ledgers (§6).

---

## 3. Durable telemetry store (proposed 0043 schema)

The schema follows the 0042 conventions (`MIG/0042_runtime_stop_progress.sql`):

- **Header:** a purpose line, "Additive only.", a table list and why-sections (`:1-77`). Comments sit only between statements.
- **Names:** tables `omnivia_runtime_<noun>`, indexes `omnivia_idx_...`, guard triggers `omnivia_guard_<table>_{insert|update|delete}`.
- **Types:** ids are `TEXT` with the standard id CHECK (`:91-94`), times are `*_at_us INTEGER > 0`.
- **Keys:** `PRIMARY KEY (workspace_id, <x>_id)`, `WITHOUT ROWID`, plus an `audit_ref` FK to `omnivia_application_audit_events`.
- **INSERT guard:** repeats the writer, mutation-guard and lease check (`:215-228`).
- **Append-only:** UPDATE and DELETE raise `RAISE(ABORT, 'omnivia: <table> is append-only; ...')` (`:253-263`), following the C05 stop-progress precedent (observations, not state columns, `:14-20`).

### 3.1 `omnivia_runtime_trigger_subscription_events`

Append-only history of a trigger's subscription state. The latest row is the
current state. This mirrors `omnivia_connector_health_events`
(`MIG/0017_connector_sync_state.sql:161-191`).

| Column | Type / rule |
|---|---|
| `workspace_id` | id |
| `subscription_event_id` | id, PK with `workspace_id` |
| `trigger_id` | id (control-plane `Trigger.id`, or a wait id for T3) |
| `trigger_kind` | `IN ('manual','schedule','webhook','cloudevent','catalogue_event','wait_signal','wait_timer')` |
| `subscription_sequence` | integer ≥ 1, contiguous per `(workspace_id, trigger_id)` (INSERT guard) |
| `subscription_state` | `IN ('active','paused','disabled','unavailable')`: `LifecycleState` (`src/omnivia_core/control_plane/models.py:112-124`) collapsed to what a subscriber can be |
| `reason` | dotted lowercase reason code |
| `observed_at_us` | time, never earlier than the previous row for the same trigger |
| `audit_ref` | FK |

### 3.2 `omnivia_runtime_trigger_observations`

One row per stimulus Core received, whatever the outcome.

| Column | Type / rule |
|---|---|
| `workspace_id` | id |
| `trigger_observation_id` | id, PK with `workspace_id` |
| `trigger_id`, `trigger_kind` | as in §3.1 |
| `observation_sequence` | integer ≥ 1, contiguous per `(workspace_id, trigger_id)`. Gives "last observation" in one index seek |
| `event_id` | envelope `id` (`src/omnivia_core/control_plane/models.py:524`) or `wait_id` |
| `idempotency_key` | text. Legacy default is `envelope.idempotency_key or envelope.id` (`LEG/control_plane/registry.py:1681`) |
| `occurred_at_us` | integer **nullable**: source time, often unknown (`occurred_at` is optional at `models.py:529`) |
| `observed_at_us` | Core receive time, monotonic per trigger |
| `delivery_status` | `IN ('accepted','duplicate','dead_lettered')` |
| `dead_letter_reason` | `NULL` unless `dead_lettered`. The legacy vocabulary is `unknown_workspace`, `unknown_trigger`, `inactive_trigger`, `event_type_mismatch`, `trigger_cooldown`, `trigger_debounce`, `no_automation_for_trigger`, `inactive_automation`, `automation_concurrency` (`LEG/control_plane/registry.py:1707-1801`). CHECK ties it to the status |
| `duplicate_of_observation_id` | nullable; set iff `duplicate` |
| `job_id` | nullable; the `omnivia_durable_jobs.job_id` enqueued on `accepted` (T4) |
| `run_id` | nullable; the runtime run resumed (T3) |
| `audit_ref` | FK |

Index: `omnivia_idx_runtime_trigger_observations_trigger` on
`(workspace_id, trigger_id, observation_sequence)`.

Two tables, no mutable row. **Deliberately not added:**

- a processing-status column (it would duplicate `omnivia_job_events` / `omnivia_runtime_events`);
- a failure table (dead-letter reasons live on the observation, and execution
  failures live in `omnivia_job_attempts.error_json`, `MIG/0010_durable_job_history.sql:59-89`).

---

## 4. Migration reservation

`contracts/migrations/v1/allocations.json` gains:

```json
{"number": 43, "filename": "0043_runtime_trigger_telemetry.sql",
 "owner": "Workflow Runtime", "repository": "omnivia-core", "state": "reserved",
 "predecessor": 42, "sha256": null, "introduced_commit": null, "accepted_commit": null}
```

The reserved rule (`allocations.json:5`) keeps the SQL absent.
`tests/test_migration_allocations.py` `EXPECTED_ALLOCATION` gains
`(43, "0043_runtime_trigger_telemetry.sql", "Workflow Runtime", "reserved")`.
`test_reserved_sql_is_absent_from_the_tree` then asserts the file does not exist.

When 0043 materialises, the same change must:

- set `state: candidate`, the sha256 and the introducing commit;
- add `43` to `CANDIDATE_INTRODUCED_COMMITS`;
- add a per-migration pin test, like `packages/omnivia-core-runtime/tests/phase3/runtime/test_c05_runtime_stop_progress_migration.py`.

Owner "Workflow Runtime" is a proposal (Q5).

---

## 5. Emission points (next lane; not implemented)

| Record | File / function | Transaction | Placement |
|---|---|---|---|
| T3 observation (`accepted`) | `RT/service/runtime_waits.py:280` `resolve_runtime_wait` | its existing fenced transaction | directly after `writer.close_wait(...)` (`:387-393`), before `append_run_event` (`:401`). Only for `kind IN ('external_signal','timer')`. `run_id = command.run_id` |
| T3 observation (`dead_lettered`) | same function, on the deadline / contract rejection paths (`:372`, `:380-386`, `:565-594`) | **Open (Q3):** those paths raise and roll back today, so a rejected signal leaves no trace. Recording it needs a separate fenced write on the rejection path | — |
| T4 observation + job | new `trigger.ingest` handler (sibling of `RT/service/handlers/workflow.py`) | `execute_mutation` (`RT/service/mutation.py:662`) | one transaction: observation row, then `_insert_workflow_job` (`RT/service/handlers/workflow.py:1512`) on `accepted` |
| T4 subscription event | wherever the canonical runtime changes a trigger's lifecycle | **No such writer exists in `RT/`.** Trigger declarations are persisted only by the legacy registry (`LEG/control_plane/registry.py`, `control_plane_resources`). See Q2 | — |
| T2 | none new | — | already emitted by `register_sync_run` / `record_health` / `record_dead_letter` (`RT/storage/connectors.py:243`, `:443`, `:360`) inside the coordinator's `_fenced()` (`RT/service/ingestion_coordinator.py:2101`) |

---

## 6. Dev read-back contract (for the C21 read-back lane)

This is a read-only projection. Per `trigger_id`:

| DEV-REQ-112 need | Source |
|---|---|
| Subscription state | latest `omnivia_runtime_trigger_subscription_events` row (max `subscription_sequence`): `subscription_state`, `reason`, `observed_at_us`. T2: latest `omnivia_connector_health_events.health_state` (`MIG/0017_connector_sync_state.sql:161-191`) |
| Last observation | latest `omnivia_runtime_trigger_observations` row (max `observation_sequence`): `observed_at_us`, `occurred_at_us`, `event_id`. T2: max `omnivia_connector_sync_runs.sync_sequence` / `started_at_us` (`:48-85`) |
| Delivery status | `delivery_status` and `dead_letter_reason` of that row, plus per-status counts over a window |
| Processing status | via `job_id`: latest `omnivia_job_events` state (`MIG/0010_durable_job_history.sql:149-170`) and `omnivia_job_terminal_results` (`:172`). Via `run_id` (T3): latest `omnivia_runtime_events.run_status` (`MIG/0018_agent_runtime_records.sql:449`) |
| Failures | `dead_lettered` observations, plus `omnivia_job_attempts.error_json` for accepted jobs that failed. T2: `omnivia_connector_dead_letters` (`failure_code`, `retry_class`, `attempts`) |
| Uncertainty | derived, never stored as a guess: (a) `occurred_at_us IS NULL` means source time is unknown; (b) an accepted observation whose job has a recovery-provenance terminal observation (`omnivia_job_terminal_observations.provenance_kind`, `MIG/0015_application_job_bridges.sql:53`) or a `system.recovery` control (`:137`) means processing was interrupted and re-driven; (c) `active` subscription with no observation newer than the expected cadence means silence is unexplained. Cadence is known only for `schedule` triggers (`Trigger.schedule_rrule`, `src/omnivia_core/control_plane/models.py:318`) |

Dev reads this through a Core query operation, not raw SQL. Its name and wire
schema belong to the read-back lane.

---

## 7. Open questions for the founder

1. **Q1: No live trigger source.** Core has no scheduler or receiver (§0). Should
   the implementation lane stop at T3 plus a callable `trigger.ingest` (the
   legacy "one-shot pass, not a daemon" posture, `LEG/control_plane/registry.py:1862-1864`),
   or is a Core-owned driver in scope? If yes, it would sit on the lease-renewal
   poll (`RT/service/main.py:545`) or the transport service-work hook
   (`RT/service/main.py:888`).
2. **Q2: Where do trigger declarations live in the canonical runtime?**
   `Trigger` / `Automation` are persisted only by the ADR-036 legacy registry. The
   subscription-events table needs an in-runtime writer, which needs an owned
   declaration store first. Should it be port, bridge or defer?
3. **Q3: Rejected T3 signals.** They currently roll back and leave no trace. Should
   a rejected delivery be recorded in its own fenced write (more telemetry, a
   second transaction), or stay invisible?
4. **Q4: Scope of `wait_timer`.** Nothing fires `timer_expiry` today (§1, T3). Keep
   `wait_timer` in the CHECK now (cheap, forward-compatible) or add it when a timer
   driver exists?
5. **Q5: Owner.** The allocation says Workflow Runtime (T4 enqueues
   `workflow.execute`; T3 lives in workflow control). Should it be Agent Runtime
   (it owns 0018 waits) or a new "Trigger Runtime"?
6. **Q6: Retention.** Append-only observations grow without bound under a
   high-rate webhook or cloudevent. Are 0039-style semantic retention and recovery
   in scope for 0043, or a later migration?
7. **Q7: DEV-REQ-112 text.** The requirement is not in this repo. §6 maps the
   paraphrase from the task packet. Please confirm, or supply the canonical wording
   (especially what "uncertainty" must cover).
