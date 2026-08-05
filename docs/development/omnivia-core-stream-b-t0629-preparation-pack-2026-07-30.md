# OmniVia Core Stream B — T-0629 Preparation Pack

Date: 2026-07-30
Author: Claude (Stream B implementation agent)
Status: **read-only preparation. No T-0629 implementation code is authorized or included.**
Basis commit for all inventory line numbers: `55f248945ecee5bb4c36cf7ffd4c155b550cc09e`
Authority: PM ADR-037; T-0629 packet
`/Users/claytonread/Projects/omnivia-pm/docs/tasks/2026-07-29-t0629-omnivia-core-phase-2-workspace-migrations-and-fencing.md`

This is the concurrent preparation the handoff brief authorizes while the T-0628
closeout gate is closed: the mutation call-site inventory, the executable
adversarial test plan, the migration fixture oracle design, the fake clock and
process-evidence doubles, and the multi-process harness design.

Companion: `omnivia-core-stream-b-b0-independent-review-2026-07-30.md`.

---

## 1. Mutation call-site inventory

The T-0629 packet states Phase 2 is complete only when the **real** legacy write
seam is guarded, not a new happy path. This is that seam, enumerated from the
bytes at the basis commit.

### 1.1 Writable connection sites

| # | Site | Nature |
|---|---|---|
| 1 | `services/omnivia-memory/src/omnivia_memory/persistence/database.py:51-54` | `sqlite3.connect(str(self.config.db_path), check_same_thread=False)` — the single production factory, `Database.connect()`. Every repository routes through it. |
| 2 | `.../persistence/database.py:587-611` | `get_database()` — same factory, but supplies the path itself. **The implicit fallback.** |
| 3 | `baseline/legacy_db.py:212` | `sqlite3.connect(str(destination_resolved))` — backup destination. Defensively guarded by `assert_fixture_database()` (`:199-200`), which rejects paths inside `~/.omnivia` or named `memories.db` under home. |
| 4 | `baseline/legacy_db.py:154` | `sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)` + `PRAGMA query_only = ON` — read-only, correct. |

No `aiosqlite`, no SQLAlchemy `create_engine` anywhere. `services/omnivia-memory/pyproject.toml:12-14`
declares `sqlalchemy>=2.0.0` but it is unused by the write path.

### 1.2 The implicit writable fallback — exact target for removal

`services/omnivia-memory/src/omnivia_memory/persistence/database.py:587-611`

```python
def get_database(db_path: Path | str | None = None) -> Database:
    global _global_db
    if _global_db is None:
        if db_path is None:
            home = Path.home()
            db_dir = home / ".omnivia"
            db_dir.mkdir(parents=True, exist_ok=True)
            db_path = db_dir / "memories.db"
        config = DatabaseConfig(db_path=Path(db_path))
        _global_db = Database(config)
        _global_db.connect()
    return _global_db
```

Two properties that make this worse than a default argument:

1. It **creates** the directory (`mkdir(parents=True, exist_ok=True)`) and opens a
   writable database as a side effect of a getter.
2. It is a module-level singleton (`_global_db`, `:584`) guarded only by
   `if _global_db is None`. The first bare call anywhere in the process fixes the
   path for **every** later caller, including callers that pass an explicit path.
   `reset_database()` (`:614-623`) is the only escape.

Confirmed live callers with no argument: `services/omnivia-memory/tests/test_persistence.py:334`
and `:345-346`. Outside an isolated `$HOME`, these touch the developer's real
`~/.omnivia/memories.db` and run the full `_init_schema()` DDL against it.

Two places in the tree already route around it deliberately, which corroborates
that this is understood as dangerous: `baseline/scenarios.py:180-182` ("`get_database()`
would default to `~/.omnivia/memories.db`, which the baseline must never open")
and `baseline/storage.py:10`.

### 1.3 Second, independent `~/.omnivia` default

`services/omnivia-memory/src/omnivia_memory/workspace/models.py:102-107` —
`WorkspaceCreate.to_workspace()` defaults `storage_path` to
`Path.home() / ".omnivia" / "workspaces" / workspace_id`, which flows straight into
`INSERT INTO workspaces` at `workspace/repository.py:23-25`.

**Fencing the database file alone does not address this.** It is row content, not a
connection path. The identical pattern exists in the public contracts package at
`src/omnivia_core/workspace/models.py:113-129` — inert today (no persistence layer
there) but exported publicly. See B0 finding F-06.

### 1.4 DDL

All production DDL is in one method, and it runs on **every connection open**:
`_init_schema()` (`database.py:75-427`), called unconditionally from `connect()`
(`:56`). Roughly 34 `CREATE TABLE` / `CREATE INDEX` / `CREATE UNIQUE INDEX`
statements covering the 14 legacy tables.

Two helpers matter for T-0629B's "ordinary open must not create or patch a schema":

- `_ensure_column()` (`:429-451`) issues `ALTER TABLE {table} ADD COLUMN` at
  `:444-446`, invoked at `:112` (`memories.workspace_id`) and `:279`
  (`sources.workspace_id`).
- `_try_execute_schema()` (`:453-461`) executes optional DDL.

Both **swallow** `sqlite3.OperationalError` when the message contains
`"readonly database"` and return rather than raising (`:457-461`). So the current
code is written to silently no-op against a read-only connection instead of
failing closed — the opposite of the T-0629B requirement.

### 1.5 DML, grouped by the subsystem T-0629F must guard

| Subsystem | Sites | Locations |
|---|---:|---|
| Repository (memory CRUD) | 3 | `persistence/repositories.py:49-52` INSERT, `:145-148` UPDATE, `:181-183` DELETE |
| Ingestion | 7 | `ingestion/repositories.py:23-25`, `:47-49` INSERT sources; `:161-163`, `:189-191` UPDATE; `:218-219` DELETE; `:261-263` INSERT chunks; `:316-317` DELETE chunks |
| Workspace | 3 | `workspace/repository.py:23-25` INSERT, `:70-72` UPDATE, `:99` DELETE |
| Graph | 8 | `graph/repository.py:53-55`, `:298-300`, `:655-657` INSERT; `:111-113`, `:357-359` UPDATE; `:145-146`, `:150-151`, `:389-390` DELETE |
| Control plane / scheduler / durable jobs | 9 | `control_plane/registry.py:1274-1276`, `:4280-4282`, `:4921-4923`, `:5026-5028` UPDATE; `:4200-4202` INSERT + `:4205` UPSERT; `:4218-4219` DELETE; `:4225-4227` INSERT; `:4377-4379`, `:5104-5106` INSERT events |

Total production DML: **24**. `executemany` has **zero** production call sites
(`Database.executemany()` at `:483-501` is defined but uncalled).

No DML at all in `memory_graph/`, `search/`, `memory/`, `knowledge/`, `lifecycle/`,
`provenance/`, `component_contract/`, `app_manifest/`, `app_shell_bridge/`,
`module_manifest/`, `_shared/` — these consume the repositories above.

### 1.6 Transaction handling — the atomicity gap

This is the most important behavioural finding for T-0629F.

- `DatabaseConfig.auto_commit` defaults to **`True`** (`database.py:26`).
- `execute()` (`:463-481`) and `executemany()` (`:483-501`) each call
  `self.connection.commit()` when `auto_commit` is true.
- `transaction()` (`:511-525`) yields, then commits, and rolls back on exception —
  but it **does not suspend `auto_commit`**. Every statement inside
  `with db.transaction():` therefore commits individually. The context manager
  provides rollback-on-exception for the *last* statement only; earlier statements
  are already durable.
- Only `immediate_transaction()` (`:527-580`) is real: it sets
  `connection.isolation_level = None` (`:564`), `auto_commit = False` (`:565`),
  and issues verbatim `BEGIN IMMEDIATE` (`:567`) / `COMMIT` (`:569`) / `ROLLBACK`
  (`:572`), restoring both in `finally` (`:579-580`).

Production callers of the **weak** `transaction()`: `control_plane/registry.py:4199`
and `:4532`. The `:4199` block is the manifest replace-all sequence — UPSERT
manifest, `DELETE FROM control_plane_resources WHERE workspace_id = ?`, then
re-INSERT each resource. With per-statement auto-commit, an interruption between
the DELETE and the re-INSERTs leaves the workspace with **no resources**, already
committed. This is the concrete mechanism behind the packet's "can split graph
deletion and control-plane resource/event pairs across commits".

The single caller of `immediate_transaction()` is the durable-run claim path
(`control_plane/registry.py:3493`, reached via `_claim_due_run_in_transaction` at
`:3505` → `_claim_queued_run` → UPDATE at `:5028`).

### 1.7 Write paths reachable outside the repository layer

Ranked by risk:

1. **`get_database()`** (`database.py:587-611`) — reachable from any importer, no
   gate. Confirmed to touch a real non-tmp path via `test_persistence.py:334`,
   `:345-346`.
2. **`WorkspaceCreate.to_workspace()`** (`workspace/models.py:102-107`) — the
   `~/.omnivia/workspaces/<id>` row-content default, independent of the DB path.
3. `services/omnivia-memory/tests/test_ingestion.py:574`, `:719`, `:859` — three
   pytest fixtures opening raw `sqlite3.connect()` and hand-running `CREATE TABLE`,
   bypassing `Database` entirely. All tmp-scoped today.
4. `baseline/tests/test_legacy_db.py:82`, `:158`, `:174` — raw connections issuing
   `UPDATE` / `DELETE` / `DROP TABLE patterns` against a `tmp_path` fixture, to
   simulate migration damage.
5. `baseline/legacy_db.py:212` — the guarded backup destination.

No CLI or MCP write path exists in this repository: `baseline/cli.py` exposes only
`capture` / `verify` / `list-gaps`, and there is no `FastMCP` / `@tool` /
`mcp.server` implementation anywhere. The string `"mcp.tool"` in
`control_plane/imports.py` is a resource-type label.

### 1.8 Summary counts

```text
Writable connection sites, production:            2  (+1 read-only)
Writable connection sites, tests bypassing wrapper: 6
Implicit home-database fallback:                  1
DDL: owning methods 1 (~34 statements) + 2 ALTER sites
     (+3 test-only CREATE, 1 test-only DROP)
Production DML sites:                            24   (executemany: 0)
Reachable outside the repository layer:           5   (1 confirmed to touch a real path)
```

### 1.9 Scope correction for T-0629B — the Phase 0 oracle is not independent

The packet requires stopping "the Phase 0 legacy oracle importing the live runtime
database". It currently does, via **deferred** imports that a top-of-file grep
misses:

- `baseline/storage.py:53` — `from omnivia_memory.persistence.database import Database, DatabaseConfig`, inside `build_storage_schema_inventory()`
- `baseline/legacy_db.py:388` — the same import inside a function

`build_storage_schema_inventory()` (`baseline/storage.py:51-73`) derives the frozen
schema by instantiating the live `Database` against a throwaway probe file and
records `"source": "omnivia_memory.persistence.database.Database._init_schema"`.

The consequence is circular authority: the "immutable oracle" is regenerated from
live code, so a change to `_init_schema` moves the oracle with it and the drift
becomes invisible. This must be cut to a checked-in SQL/DDL artifact before the
oracle can serve as migration evidence.

---

## 2. Adversarial acceptance matrix — 116 named executable cases

The packet requires the matrix be "converted into named tests rather than treated
as advisory prose". Below is the naming, one row per required case, grouped to the
mandated counts: 12 + 12 + 22 + 8 + 22 + 28 + 12 = **116**.

Proposed location: `packages/omnivia-core-runtime/tests/phase2/` (Stream B owned
per plan §7.2), one module per group.

Convention: `test_<group>_<nn>_<behaviour>`. Every case must assert an observable
outcome, not merely that a call did not raise.

### 2.1 Workspace and manifest — 12 (`test_workspace_manifest.py`)

| ID | Test name | Proves |
|---|---|---|
| WM-01 | `test_portable_workspace_contains_exactly_the_five_paths` | Only `workspace.json`, `workspace.sqlite`, `blobs/`, `indexes/`, `locks/` |
| WM-02 | `test_manifest_serialises_no_absolute_path` | No absolute path in any manifest field |
| WM-03 | `test_manifest_serialises_no_installation_or_process_identity` | No install id, PID, hostname, endpoint |
| WM-04 | `test_manifest_rejects_secret_bearing_fields` | Secret-like keys refused at construction |
| WM-05 | `test_workspace_identity_is_stable_when_moved` | Same id after directory move |
| WM-06 | `test_manifest_write_is_atomic_under_simulated_crash` | fsync/rename; no torn manifest at any interruption point |
| WM-07 | `test_manifest_canonical_serialisation_is_byte_stable` | Same values → same bytes, twice, key order fixed |
| WM-08 | `test_manifest_checksum_covers_every_meaningful_field` | Mutating any field changes the checksum |
| WM-09 | `test_read_only_inspection_performs_zero_writes` | mtime/size/journal unchanged after inspect |
| WM-10 | `test_manifest_path_traversal_is_rejected` | `../` in any path-bearing field refused |
| WM-11 | `test_unsafe_symlink_in_workspace_is_rejected` | Symlink escaping the workspace root refused |
| WM-12 | `test_incompatible_manifest_fails_before_lease_acquisition` | Compatibility evaluated before any lock/lease |

### 2.2 Bootstrap and discovery — 12 (`test_bootstrap_discovery.py`)

| ID | Test name | Proves |
|---|---|---|
| BD-01 | `test_generation_one_bootstrap_creates_substrate_exactly_once` | Generation 1 assigned once |
| BD-02 | `test_bootstrap_requires_exact_phase0_schema_fingerprint` | Wrong fingerprint refuses bootstrap |
| BD-03 | `test_bootstrap_runs_under_storage_lock_and_exclusive_connection` | Lock + sole exclusive connection held first |
| BD-04 | `test_crash_at_each_bootstrap_statement_retries_or_resumes` | Parametrised over every statement; each restart reaches a valid committed substrate or the unchanged Phase 0 state |
| BD-05 | `test_ordinary_open_never_creates_ownership_tables` | Generic open creates nothing |
| BD-06 | `test_existing_runtime_database_does_not_repeat_bootstrap` | Second open increments, never re-bootstraps |
| BD-07 | `test_simultaneous_startup_has_exactly_one_winner` | Two launchers, one service |
| BD-08 | `test_bootstrap_mutex_grants_no_write_authority` | Mutex holder cannot write |
| BD-09 | `test_discovery_record_is_published_atomically` | No partially-written descriptor observable |
| BD-10 | `test_failed_startup_does_not_delete_another_instances_discovery_record` | Compare-by-instance cleanup only |
| BD-11 | `test_discovery_rediscovery_after_mutex_finds_live_service` | discover → mutex → rediscover short-circuits the spawn |
| BD-12 | `test_baseline_adopted_is_recorded_for_the_phase0_schema` | Ledger records `baseline_adopted` |

### 2.3 Lease and owner evidence — 22 (`test_lease_owner_evidence.py`)

| ID | Test name | Proves |
|---|---|---|
| LE-01 | `test_lease_acquisition_is_atomic` | No interleaved partial acquisition |
| LE-02 | `test_every_acquisition_increments_the_fencing_generation` | Monotonic on acquire |
| LE-03 | `test_every_takeover_increments_the_fencing_generation` | Monotonic on takeover |
| LE-04 | `test_generation_is_recorded_in_authoritative_workspace_state` | Persisted, not in-memory |
| LE-05 | `test_simultaneous_acquisition_has_exactly_one_winner` | N racers, one lease |
| LE-06 | `test_expired_heartbeat_alone_does_not_permit_takeover` | Expiry is a signal only |
| LE-07 | `test_takeover_requires_storage_lock_availability` | Lock evidence mandatory |
| LE-08 | `test_takeover_requires_endpoint_evidence_where_available` | Endpoint probe consulted |
| LE-09 | `test_takeover_requires_process_start_evidence` | Process-start compared |
| LE-10 | `test_pid_reuse_does_not_prove_owner_liveness` | Same PID, different start time → not alive |
| LE-11 | `test_suspended_owner_resuming_after_takeover_is_rejected` | The core PM ADR-037 scenario |
| LE-12 | `test_stale_owner_cannot_reclaim_authority` | Old generation refused |
| LE-13 | `test_heartbeat_is_monotonic_where_available` | Monotonic clock used |
| LE-14 | `test_wall_clock_adjustment_does_not_expire_a_live_lease` | Clock jump tolerated |
| LE-15 | `test_graceful_handover_transfers_without_data_loss` | Clean handover |
| LE-16 | `test_lease_record_carries_every_required_evidence_field` | Full PM ADR-037 lease record |
| LE-17 | `test_lease_release_on_shutdown_records_shutdown_state` | Clean shutdown recorded |
| LE-18 | `test_readiness_requires_the_exact_current_lease_tuple` | Tuple identity enforced |
| LE-19 | `test_no_newer_recorded_generation_exists_before_readiness` | Final pre-readiness check |
| LE-20 | `test_lease_acquisition_after_storage_lock_and_connection_only` | Ordering invariant (PM ADR-037 #17) |
| LE-21 | `test_debugger_suspended_owner_is_not_declared_dead` | Suspension ≠ death |
| LE-22 | `test_failed_migration_during_takeover_leaves_no_partial_authority` | Takeover + migration failure is safe |

### 2.4 Filesystem locking — 8 (`test_filesystem_locking.py`)

| ID | Test name | Proves |
|---|---|---|
| FL-01 | `test_posix_two_process_exclusion` | Real second process excluded on POSIX |
| FL-02 | `test_windows_two_process_exclusion` | Same on Windows (CI matrix) |
| FL-03 | `test_lock_interface_is_identical_across_platforms` | One frozen interface, two implementations |
| FL-04 | `test_nfs_refuses_direct_writable_operation` | Fails closed |
| FL-05 | `test_smb_cifs_refuses_direct_writable_operation` | Fails closed |
| FL-06 | `test_sshfs_refuses_direct_writable_operation` | Fails closed |
| FL-07 | `test_unknown_lock_semantics_refuse_direct_writable_operation` | Default-deny for unrecognised filesystems |
| FL-08 | `test_lifetime_storage_lock_is_held_for_the_whole_ownership_lifetime` | Never dropped mid-ownership |

### 2.5 Fencing and mutation — 22 (`test_fencing_mutation.py`)

| ID | Test name | Proves |
|---|---|---|
| FM-01 | `test_every_managed_mutation_uses_begin_immediate` | `BEGIN IMMEDIATE` on all write txns |
| FM-02 | `test_token_is_validated_inside_the_write_transaction` | In-transaction validation |
| FM-03 | `test_token_is_revalidated_immediately_before_commit` | Second check |
| FM-04 | `test_stale_generation_cannot_commit_after_takeover` | Superseded writer blocked |
| FM-05 | `test_stale_generation_cannot_commit_after_sleep_resume` | Resume path blocked |
| FM-06 | `test_generation_change_between_begin_and_commit_rolls_back` | Mid-transaction change caught |
| FM-07 | `test_service_instance_mismatch_rolls_back` | Instance component enforced |
| FM-08 | `test_workspace_id_mismatch_rolls_back` | Workspace component enforced |
| FM-09 | `test_unregistered_dml_fails_closed_on_runtime_connections` | Authorizer denies |
| FM-10 | `test_unregistered_ddl_fails_closed_on_runtime_connections` | Authorizer denies DDL |
| FM-11 | `test_ordinary_external_dml_fails_closed_with_intact_triggers` | Persisted triggers fail closed |
| FM-12 | `test_second_stock_sqlite_process_cannot_dml_while_ready_posix` | Real external process, POSIX |
| FM-13 | `test_second_stock_sqlite_process_cannot_ddl_while_ready_posix` | DDL variant |
| FM-14 | `test_second_stock_sqlite_process_cannot_dml_while_ready_windows` | Windows variant |
| FM-15 | `test_schema_and_trigger_fingerprint_verified_before_readiness` | Exact fingerprint gate |
| FM-16 | `test_offline_trigger_drift_is_detected_before_writable_readiness` | Offline tamper detected |
| FM-17 | `test_offline_schema_drift_is_detected_before_writable_readiness` | Offline schema drift detected |
| FM-18 | `test_repository_mutations_are_guarded` | All 3 repository DML sites (§1.5) |
| FM-19 | `test_ingestion_mutations_are_guarded` | All 7 ingestion sites |
| FM-20 | `test_graph_and_workspace_mutations_are_guarded` | All 11 graph + workspace sites |
| FM-21 | `test_control_plane_and_scheduler_mutations_are_guarded` | All 9 control-plane sites |
| FM-22 | `test_no_normal_path_can_write_the_implicit_home_database` | `get_database()` fallback removed |

**Threat-model boundary — do not overclaim.** PM ADR-037 states plainly that
persisted triggers are "not a security boundary against arbitrary code running as
the workspace-owning OS principal", and that Core does not claim to prevent that
principal from terminating the service, changing ACLs, using another VFS, or
altering bytes offline. No test above may be named or documented as preventing
same-principal tampering. FM-16 and FM-17 assert **detection before readiness**,
which is what the ADR actually promises. A test asserting prevention would be
asserting something the architecture explicitly disclaims.

### 2.6 Migration, backup and restore — 28 (`test_migration_backup_restore.py`)

| ID | Test name | Proves |
|---|---|---|
| MB-01 | `test_migration_accepts_only_an_explicit_source_path` | No inference |
| MB-02 | `test_migration_never_infers_a_home_directory_database` | `~/.omnivia` never auto-selected |
| MB-03 | `test_source_is_opened_read_only` | `mode=ro` + `query_only` |
| MB-04 | `test_source_requires_the_exact_phase0_fingerprint` | Fingerprint gate |
| MB-05 | `test_ambiguous_multi_workspace_mapping_is_refused` | Explicit plan required |
| MB-06 | `test_backup_is_created_before_any_schema_work` | Ordering |
| MB-07 | `test_backup_is_verified_before_migration_proceeds` | Verified, not just written |
| MB-08 | `test_migration_operates_on_a_staging_copy_only` | Source untouched |
| MB-09 | `test_source_bytes_are_unchanged_after_successful_migration` | Byte-level proof |
| MB-10 | `test_source_bytes_are_unchanged_after_failed_migration` | Failure safety |
| MB-11 | `test_sole_backup_is_never_the_migration_target` | Recovery copy preserved |
| MB-12 | `test_all_fourteen_legacy_tables_are_preserved` | Table set intact |
| MB-13 | `test_every_column_is_preserved_per_table` | Column sets intact |
| MB-14 | `test_row_counts_are_preserved_per_table` | Counts intact |
| MB-15 | `test_value_checksums_are_preserved_per_table` | Values intact |
| MB-16 | `test_attempt_journal_is_written_outside_the_portable_workspace` | Journal location |
| MB-17 | `test_interrupted_migration_recovers_from_the_attempt_journal` | Resume |
| MB-18 | `test_publication_is_atomic_after_all_validation_passes` | No partial publish |
| MB-19 | `test_publication_does_not_occur_when_integrity_check_fails` | Gate honoured |
| MB-20 | `test_publication_does_not_occur_when_manifest_is_incompatible` | Gate honoured |
| MB-21 | `test_rollback_restores_the_exact_phase0_schema` | Exact schema |
| MB-22 | `test_rollback_restores_exact_row_counts_and_values` | Exact data |
| MB-23 | `test_migrations_are_checksum_pinned_and_ordered` | Ledger pinning |
| MB-24 | `test_migration_ledger_is_authoritative_over_user_version` | `PRAGMA user_version` is diagnostic only |
| MB-25 | `test_migrations_run_under_the_current_workspace_generation_instance_tuple` | Fenced migrations |
| MB-26 | `test_no_partial_migration_commit_is_observable` | Atomicity |
| MB-27 | `test_wal_foreign_keys_and_busy_timeout_are_enabled` | SQLite configuration |
| MB-28 | `test_integrity_check_runs_and_gates_readiness` | `PRAGMA integrity_check` |

### 2.7 Lifecycle, scheduler and cleanup — 12 (`test_lifecycle_cleanup.py`)

| ID | Test name | Proves |
|---|---|---|
| LC-01 | `test_all_ten_lifecycle_states_are_reachable_and_distinct` | stopped/starting/recovering/migrating/ready/running/draining/maintenance/failed/stopped |
| LC-02 | `test_writable_readiness_is_published_last` | Ordering |
| LC-03 | `test_readiness_requires_all_nine_preconditions_at_one_instance` | Full PM ADR-037 readiness set |
| LC-04 | `test_failed_transition_releases_resources_in_reverse_order` | Reverse-order cleanup |
| LC-05 | `test_failed_instance_publishes_no_readiness` | No readiness on failure |
| LC-06 | `test_scheduler_revalidates_generation_after_resume` | Resume revalidation |
| LC-07 | `test_no_partial_batch_or_import_commit` | Batch atomicity |
| LC-08 | `test_no_partial_durable_job_commit` | Job atomicity |
| LC-09 | `test_no_partial_projection_commit` | Projection atomicity |
| LC-10 | `test_durable_jobs_are_recovered_before_readiness` | Recovery gate |
| LC-11 | `test_draining_rejects_new_mutations_but_completes_in_flight` | Drain semantics |
| LC-12 | `test_mcp_cli_and_launchers_expose_no_lease_ownership_api` | Client boundary |

### 2.8 Coverage check

```text
Workspace and manifest        12
Bootstrap and discovery       12
Lease and owner evidence      22
Filesystem locking             8
Fencing and mutation          22
Migration, backup, restore    28
Lifecycle, scheduler, cleanup 12
                            ----
Total                        116
```

Cases requiring a real second OS process: BD-07, LE-05, FL-01, FL-02, FM-12,
FM-13, FM-14. Cases requiring a Windows runner: FL-02, FM-14. Cases requiring
simulated crash injection: WM-06, BD-04, MB-17, MB-26.

---

## 3. Migration fixture oracle design

### 3.1 The problem with the current frozen fixture

`baseline/inventories/legacy-db-fixture.json` records the Phase 0 fingerprint:

```text
tables:           14
content_checksum: 32babfa450b1c792e18fd2b09cf6f51fbaafd601a7df3944aab635dbb27d6b30
total rows:       4      (memories 3, workspaces 1; the other 12 tables are empty)
```

14 tables is correct and matches the packet. But **4 rows across 14 tables is a
weak oracle for MB-12 … MB-15 and MB-21 … MB-22.** A migration that preserves 3
memory rows and 1 workspace row demonstrates almost nothing about value
preservation. Twelve of the fourteen tables are empty, so no foreign-key
relationship, no `_json` column, no unicode, no NULL/empty-string distinction and
no large value is exercised at all.

### 3.2 Two-tier oracle

Keep the frozen fixture as the **fingerprint gate** (MB-04 must continue to
require exactly `32babfa4…`). Add a second, richer tier for data-preservation
proof.

**Tier 1 — frozen identity fixture (unchanged).** Purpose: prove the migration
refuses anything that is not the exact known Phase 0 schema. Never modify it; it is
Stream A / PM owned evidence.

**Tier 2 — synthetic preservation corpus (new, Stream B owned).** Built by a
generator, not committed as a binary, so it stays reviewable and deterministic.
Requirements:

1. **Every one of the 14 tables non-empty**, so no table is preserved by accident.
2. **Referential fan-out**: `sources` → `chunks`, `graph_entities` →
   `graph_relationships`, `graph_entities` ↔ `memories` via `entity_memory_links`,
   `patterns` → `pattern_occurrences` / `pattern_relationships`,
   `control_plane_manifests` → `control_plane_resources` → `control_plane_events`.
   Fan-out must be uneven (0, 1, many children) so off-by-one loss is visible.
3. **Value-class coverage per text column**: empty string, NULL, ASCII, non-BMP
   unicode (emoji), a string containing `'` and `"` and `;`, a 1 MiB value, a
   string that is valid JSON, and a string that looks like JSON but is not.
   Rationale: these are what a naive row-copy or a string-interpolated INSERT
   loses or corrupts.
4. **Numeric and temporal edges**: 0, -1, `2**63-1`, float with full precision,
   an ISO-8601 timestamp with offset, and one with `Z`.
5. **`_json` columns** (`memory_ids_json`, `source_references_json`,
   `payload_json`) must contain nested structures with key orders that differ from
   insertion order, so canonicalisation bugs surface.
6. **Deterministic**: fixed seed, no `datetime.now()`, no `uuid4()` at build time.
   Generation must be byte-reproducible, checked by a test that builds twice and
   compares digests — the same discipline the application-contract generator
   already proves.

### 3.3 Comparison oracle

Reuse `baseline/legacy_db.py`'s existing shape — it already computes per-table
`content_checksum`, `row_count` and column lists, and `compare_inventories()`
(`:295-303`) already reports missing tables. That is the right primitive.

Extend the comparison for Tier 2 to report, per table: missing/added columns,
row-count delta, and per-column value-checksum delta. An empty problem list is the
zero-data-loss signal, as the existing docstring says.

**Constraint:** the Tier 2 oracle must not import the live runtime `Database` — see
§1.9. Build the corpus from checked-in DDL plus explicit `INSERT` statements, so
the oracle is independent of `_init_schema()`.

---

## 4. Fake clock and process-evidence doubles

Needed by LE-06, LE-09 … LE-14, LE-21, FM-05, LC-06. Design only.

### 4.1 Clock

One injectable protocol with two implementations. The runtime must never call
`time.monotonic()` or `time.time()` directly in lease/heartbeat code, or LE-13 and
LE-14 cannot be written.

```text
Clock protocol:
  monotonic() -> float      # for heartbeat and expiry
  wall_time() -> datetime   # for records only, never for expiry decisions
```

- `SystemClock` — production.
- `FakeClock` — test double with `advance_monotonic(seconds)` and
  `set_wall_time(dt)` as **independent** controls.

The independence is the point. LE-14 (`wall clock adjustment does not expire a
live lease`) is only provable if wall time can jump backwards while monotonic time
advances normally. A single fake `now()` cannot express that, and the bug it
catches — using wall time for expiry — would be invisible.

### 4.2 Process evidence

PM ADR-037: "PID identity alone is insufficient because of PID reuse."

```text
ProcessEvidence:
  pid: int
  start_time: <boot-relative process start>
  boot_id: <identifier that changes on reboot>
  os_principal: str
```

- `SystemProcessEvidence` — reads real values. Note the platform split: Linux
  `/proc/<pid>/stat` field 22, macOS `kinfo_proc.p_starttime` via `sysctl`,
  Windows `GetProcessTimes`. This is exactly why LE-09/LE-10 need a double —
  otherwise the test is platform-coupled and cannot express reuse.
- `FakeProcessEvidence` — construct arbitrary tuples, including the two cases that
  matter: same `pid` with a **different** `start_time` (LE-10, reuse → not alive),
  and same `pid` with the **same** `start_time` (owner genuinely alive).

`boot_id` must participate: a PID plus start time can repeat across a reboot, and
LE-10 should assert that a differing `boot_id` alone invalidates liveness.

### 4.3 Suspension

LE-11 and LE-21 need a process that is alive but not heartbeating. Use a real child
process and `SIGSTOP`/`SIGCONT` on POSIX; on Windows use a suspended thread or a
debugger-attached stub. Do **not** simulate suspension by stopping a fake
heartbeat — that would test the double, not the system. The whole point of LE-11 is
that the OS still reports the process as alive.

---

## 5. Multi-process adversarial harness design

Needed by BD-07, LE-05, FL-01, FL-02, FM-12, FM-13, FM-14.

### 5.1 Requirements

1. **Real OS processes.** Threads share the SQLite connection cache and the GIL and
   cannot demonstrate file-level exclusion. FM-12 in particular requires a process
   that does not import OmniVia at all — a plain `sqlite3` client — because the
   claim is about the *stock* SQLite VFS, not about the runtime's own guards.
2. **Deterministic rendezvous.** Racing cases (BD-07, LE-05) must have both
   processes provably at the start line before either proceeds, or the test proves
   nothing about simultaneity. Use a filesystem barrier or a pipe handshake, not a
   sleep.
3. **Bounded and cleaned.** Every spawned process must be reaped on failure, with a
   hard timeout, so a hung child cannot wedge CI.
4. **Observable outcome per child.** Each child writes a structured result
   (exit code plus a JSON line) that the parent asserts on. "One winner" must be
   asserted from both children's reports, not inferred from the parent's view.

### 5.2 Shape

```text
harness/
  spawn.py        # launch child with explicit argv, env, cwd, timeout, reaping
  barrier.py      # filesystem/pipe rendezvous for N processes
  children/
    acquire_lease.py       # attempts acquisition, reports outcome
    stock_sqlite_writer.py # plain sqlite3, no omnivia import — FM-12/13/14
    suspendable_owner.py   # acquires then waits for SIGSTOP — LE-11/21
```

### 5.3 Platform matrix

FL-02 and FM-14 need a Windows runner. Plan for it in CI early: `fcntl` versus
`LockFileEx` semantics differ enough that a POSIX-only green suite is not evidence
for the Windows claim, and the packet's exit gate requires platform-specific lock
suites to pass in CI.

### 5.4 Filesystem qualification (FL-04 … FL-07)

NFS/SMB/SSHFS cannot be assumed present on a developer machine or a CI runner.
Split the requirement:

- **Always runnable:** inject the filesystem-qualification verdict and assert the
  fail-closed *decision path* (FL-07 covers unknown semantics with no mount at
  all).
- **Conditionally runnable:** real mounts behind an explicit marker, skipped with a
  clear reason when absent.

A skip must be reported as a skip. Per the plan's own risk table, work must not be
described as qualified when its gate did not execute — a filesystem case that
never ran is not a pass.

---

## 6. Integration requests for Stream A

Prepared as requests, not edits, per plan §8 rule 6. None of these are Stream B
files.

1. ~~Make the parity gate facade-aware.~~ **Already resolved by Stream A during
   the B0 review**, using the recommended partition
   (`FACADE_CANONICAL_TO_LEGACY` in `tests/canonical_migration/_leaves.py` plus
   `tests/compatibility/test_facade_foundation.py`). Recorded here only so the
   request list matches the B0 findings; no action outstanding.
2. **Decide the `workspace/models.py` boundary** before T-0629A. B0 finding F-06:
   the public `Workspace` model resolves real paths and defaults to
   `~/.omnivia/workspaces/<id>`, while T-0629A adds a portable, path-free
   `WorkspaceManifest` to the same public package. Stream B must not edit
   `workspace/models.py` or `workspace/__init__.py` unilaterally, so this needs a
   Stream A decision and an explicit statement in `workspace/__init__.py`.
3. **Cut the Phase 0 oracle's live-runtime dependency** (§1.9), or confirm Stream B
   should do it as the first T-0629B step. `baseline/` ownership is not assigned in
   plan §7.2.
4. **Confirm the T-0629F file list** for the legacy runtime delegate. Plan §7.2
   defers this to "an exact file list agreed at that checkpoint"; §1.5 of this pack
   is the proposed input — 24 DML sites, 1 DDL method, 2 connection factories.
5. **Settle the `ruff format` question** (B0 finding F-05) before A0 adds
   merge-blocking CI.

---

## 7. Status

```text
T-0628 closeout checkpoint:        not recorded
Operational T-0629 implementation: not started, correctly blocked
This pack:                         read-only design and inventory only
Files created:                     this document and the B0 review only
Repository code modified:          none
```
