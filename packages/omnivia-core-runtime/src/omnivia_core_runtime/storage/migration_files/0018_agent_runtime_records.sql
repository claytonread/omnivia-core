-- Canonical Agent Runtime records: Run, RunStep, Attempt, Wait, RuntimeEvent (RT-102).
--
-- Additive only. Eight append-only tables, four indexes and twenty-four statement
-- triggers, on the existing fenced workspace database. No second database, no second
-- scheduler, no second queue and no second idempotency mechanism: a canonical `Run`
-- binds to an `omnivia_durable_jobs` row through the workspace-scoped
-- `omnivia_job_application_metadata` identity migration 0010 established, and command
-- replay keeps using `omnivia_idempotency_claims` / `omnivia_idempotency_outcomes`.
--
--   omnivia_runtime_runs             one canonical run, bound to one durable job
--   omnivia_runtime_run_steps        the immutable identity of one step of a run
--   omnivia_runtime_run_step_states  that step's status, in order, forever
--   omnivia_runtime_attempts         one execution of one step, at its start
--   omnivia_runtime_attempt_outcomes that execution's single terminalization
--   omnivia_runtime_waits            one durable suspension, at its creation
--   omnivia_runtime_wait_resolutions that suspension's single resolution
--   omnivia_runtime_events           the run's ordered event stream
--
-- Why eight and not five. The five canonical records are not all immutable: a step's
-- status changes, an attempt terminalizes, a wait resolves. Current fenced ownership
-- does not authorize rewriting history, so every UPDATE and DELETE here aborts
-- unconditionally -- for the current owner too. A record that changes is therefore
-- stored as an immutable half plus an append-only statement of what became of it,
-- which is the shape migration 0015 already chose over 0010's in-place
-- terminalization. `omnivia_runtime_attempt_outcomes` and
-- `omnivia_runtime_wait_resolutions` are keyed by their subject alone, so a second
-- closure is refused by the primary key rather than by a rule written somewhere else,
-- and `omnivia_runtime_run_step_states` is a contiguous stream for the same reason
-- connector health is one: the interesting question is when a step stopped running,
-- and a rewritable column answers it by destroying the answer.
--
-- What is deliberately absent is a status column on the run. `RuntimeEvent` states the
-- run status in force when it was recorded, as a required field of the accepted
-- contract, so the stream already *is* the run's status history; a column beside it
-- would be a second answer to "where does this run stand" with no rule about which one
-- wins. The current status is the highest-sequence event's, which is derived and
-- rebuildable by construction rather than a projection anything has to maintain.
--
-- Also absent: Artifact, EvidenceItem and CleanupReceipt (RT-103), and
-- PolicySnapshot, BudgetSnapshot, CapabilityGrant, Approval, EffectIntent,
-- EffectReceipt and EffectSettlement (RT-202+). A complete canonical `Run` aggregate
-- cannot be materialised from this migration alone, and this migration does not
-- pretend otherwise by inventing the parts it does not hold.
--
-- No backfill, and no gate. Every relation this migration adds is new, and no existing
-- fact in this database is honestly classifiable as one of these records. The durable
-- job substrate is what a canonical run *binds to*, not a losing representation to be
-- copied: `omnivia_durable_jobs` and the 0010/0015 history are canonical substrate,
-- and copying them here would create the second representation of one fact that the
-- cutover rules exist to prevent. The control-plane `RunRecord` is not in this
-- database at all -- its storage authority is undecided, which is phase 0 of the
-- cutover sequence and a precondition for it rather than a step within it. Manufacturing
-- a backfill would mean inventing a definition kind, a definition version, a logical
-- key, a policy snapshot and a budget snapshot that no existing row carries. So this
-- file contains no DML at all, and the accompanying tests prove both that every prior
-- fact survives untouched and that no `omnivia_migration_0018_*_gate` is owed.
--
-- Identity, types and bounds follow the established rules. Every table is
-- workspace-scoped with a composite primary key and composite foreign keys, WITHOUT
-- ROWID; every timestamp is signed 64-bit UTC microseconds in `*_at_us`; every digest
-- is `sha256:<64 lowercase hex>` over exact Core canonical-JSON bytes; identifiers,
-- open codes and operation names use the same GLOB shapes migrations 0007 and 0010
-- established; and every text column is bounded. Credentials, provider secrets,
-- filesystem paths, URLs and raw external logs have no column here -- the only
-- free-form columns are a 2048-character `message` and the two bounded canonical
-- documents an attempt failure and an event detail carry, each stored with its digest
-- and byte length so what was hashed is recorded beside it.
--
-- Every comment in this file sits *between* statements and never inside one. The
-- migrator applies these through `split_sql_statements`, which strips comments, while
-- `canonical_schema_fingerprint()` replays the same text with `executescript`, which
-- does not, so a comment inside a `CREATE` body would make a clean workspace read as
-- drifted.

-- One canonical run: what it was admitted to execute, under whose authority, and which
-- durable job carries it.
--
-- `job_id` is the whole of this migration's relationship with the scheduler. Admission
-- and claiming stay where they are; this row records which existing job is which run,
-- and the unique constraint makes that a one-to-one edge rather than a convention.
--
-- `logical_key` is the run's stable idempotency identity: two admissions carrying the
-- same key are the same run replayed, not two runs, so it is unique within the
-- workspace. That is a storage invariant on run identity and not a command idempotency
-- mechanism -- the scoped claim and outcome relations from 0007 remain the only place
-- a command's replay is decided.
--
-- `claim_id` is how that stops being a comment. Every run is admitted under one
-- existing `omnivia_idempotency_claims` row, bound by a composite foreign key so the
-- claim cannot belong to another workspace, unique so one claim admits at most one
-- run, and checked by the insert guard so the claim's operation, idempotency key and
-- audit reference are the run's own -- a duplicated contract field that contradicted
-- the substrate would otherwise be a second, unreconciled answer. The bound job's
-- `omnivia_job_application_metadata` row must agree with the same two facts, for the
-- same reason.
--
-- Its `CHECK` is bounded and NUL-free but carries no GLOB shape, deliberately: 0007
-- bounds `omnivia_idempotency_claims.claim_id` by length alone, and a stricter rule
-- here would refuse rows that relation accepts, leaving the foreign key unreachable
-- for them.
--
-- Deliberately *not* required at admission: a settled `omnivia_idempotency_outcomes`
-- row. A claim is made when a command is admitted and settled when it answers; a run
-- that had to be admitted under a settled claim could never be the thing that produces
-- the answer. RT-104 settles the existing claim; this migration adds no second
-- mechanism for it to settle.
CREATE TABLE IF NOT EXISTS omnivia_runtime_runs (
    workspace_id          TEXT    NOT NULL,
    run_id                TEXT    NOT NULL,
    job_id                TEXT    NOT NULL,
    claim_id              TEXT    NOT NULL,
    definition_kind       TEXT    NOT NULL,
    definition_id         TEXT    NOT NULL,
    definition_version    TEXT    NOT NULL,
    logical_key           TEXT    NOT NULL,
    originating_operation TEXT    NOT NULL,
    audit_ref             TEXT    NOT NULL,
    created_at_us         INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, run_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (typeof(job_id) = 'text' AND length(job_id) BETWEEN 1 AND 128
           AND job_id GLOB '[A-Za-z0-9]*'
           AND job_id NOT GLOB '*[^A-Za-z0-9._:/-]*'
           AND instr(job_id, char(0)) = 0),
    CHECK (typeof(claim_id) = 'text' AND length(claim_id) BETWEEN 1 AND 128
           AND instr(claim_id, char(0)) = 0),
    CHECK (definition_kind IN ('agent_component', 'workflow')),
    CHECK (typeof(definition_id) = 'text' AND length(definition_id) BETWEEN 1 AND 128
           AND definition_id GLOB '[A-Za-z0-9]*'
           AND definition_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(definition_id, char(0)) = 0),
    CHECK (typeof(definition_version) = 'text'
           AND length(definition_version) BETWEEN 1 AND 128
           AND definition_version GLOB '[0-9]*'
           AND definition_version NOT GLOB '*[^0-9A-Za-z.+-]*'
           AND instr(definition_version, char(0)) = 0),
    CHECK (typeof(logical_key) = 'text' AND length(logical_key) BETWEEN 1 AND 128
           AND logical_key GLOB '[A-Za-z0-9]*'
           AND logical_key NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(logical_key, char(0)) = 0),
    CHECK (typeof(originating_operation) = 'text'
           AND length(originating_operation) BETWEEN 1 AND 128
           AND originating_operation GLOB '[a-z]*'
           AND originating_operation NOT GLOB '*[^a-z0-9_.]*'
           AND originating_operation NOT GLOB '*.'
           AND originating_operation NOT GLOB '*.[^a-z]*'),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),

    UNIQUE (workspace_id, job_id),
    UNIQUE (workspace_id, logical_key),
    UNIQUE (workspace_id, claim_id),

    FOREIGN KEY (workspace_id, job_id)
        REFERENCES omnivia_job_application_metadata (workspace_id, job_id),
    FOREIGN KEY (claim_id, workspace_id)
        REFERENCES omnivia_idempotency_claims (claim_id, workspace_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

-- The immutable identity of one step: which run, which position, what kind of work.
-- Ordinals run 1..N contiguously within a run and are never renumbered, so the unique
-- constraint below and the contiguity rule in the insert guard together make a
-- reordered history unrepresentable rather than merely discouraged.
CREATE TABLE IF NOT EXISTS omnivia_runtime_run_steps (
    workspace_id  TEXT    NOT NULL,
    run_step_id   TEXT    NOT NULL,
    run_id        TEXT    NOT NULL,
    ordinal       INTEGER NOT NULL,
    step_kind     TEXT    NOT NULL,
    created_at_us INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, run_step_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(run_step_id) = 'text' AND length(run_step_id) BETWEEN 1 AND 128
           AND run_step_id GLOB '[A-Za-z0-9]*'
           AND run_step_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_step_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (typeof(ordinal) = 'integer' AND ordinal BETWEEN 1 AND 256),
    CHECK (typeof(step_kind) = 'text' AND length(step_kind) BETWEEN 1 AND 128
           AND step_kind GLOB '[a-z]*' AND step_kind NOT GLOB '*[^a-z0-9_.]*'
           AND step_kind NOT GLOB '*.' AND step_kind NOT GLOB '*.[^a-z]*'),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),

    UNIQUE (workspace_id, run_id, ordinal),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_runtime_runs (workspace_id, run_id)
) WITHOUT ROWID;

-- What a step's status was, in order, forever.
--
-- A step's status is the one part of a step that changes, and history that the owner
-- may rewrite is not history, so it is a contiguous stream rather than a column. The
-- step's current status is the highest-sequence row; `updated_at` is that row's
-- instant. `waiting` is refused unless a wait naming this step exists, because a
-- suspended step that cannot say what it is suspended on cannot be resolved.
CREATE TABLE IF NOT EXISTS omnivia_runtime_run_step_states (
    workspace_id   TEXT    NOT NULL,
    run_step_id    TEXT    NOT NULL,
    state_sequence INTEGER NOT NULL,
    status         TEXT    NOT NULL,
    observed_at_us INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, run_step_id, state_sequence),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(run_step_id) = 'text' AND length(run_step_id) BETWEEN 1 AND 128
           AND run_step_id GLOB '[A-Za-z0-9]*'
           AND run_step_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_step_id, char(0)) = 0),
    CHECK (typeof(state_sequence) = 'integer' AND state_sequence >= 0),
    CHECK (status IN ('pending', 'running', 'waiting', 'succeeded', 'failed',
                      'cancelled', 'skipped')),
    CHECK (typeof(observed_at_us) = 'integer' AND observed_at_us > 0),

    FOREIGN KEY (workspace_id, run_step_id)
        REFERENCES omnivia_runtime_run_steps (workspace_id, run_step_id)
) WITHOUT ROWID;

-- One execution attempt of one step, recorded when it starts.
--
-- An attempt exists because execution started, so there is no queued attempt state and
-- no nullable start: waiting to run is a state of the step. Attempts are numbered
-- 1..N contiguously within their step and never overlap, and only a failed, cancelled
-- or uncertain attempt may be followed by another -- a succeeded attempt is final, and
-- a history that continues past one is describing work that was already done.
CREATE TABLE IF NOT EXISTS omnivia_runtime_attempts (
    workspace_id   TEXT    NOT NULL,
    attempt_id     TEXT    NOT NULL,
    run_id         TEXT    NOT NULL,
    run_step_id    TEXT    NOT NULL,
    attempt_number INTEGER NOT NULL,
    started_at_us  INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, attempt_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(attempt_id) = 'text' AND length(attempt_id) BETWEEN 1 AND 128
           AND attempt_id GLOB '[A-Za-z0-9]*'
           AND attempt_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(attempt_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (typeof(run_step_id) = 'text' AND length(run_step_id) BETWEEN 1 AND 128
           AND run_step_id GLOB '[A-Za-z0-9]*'
           AND run_step_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_step_id, char(0)) = 0),
    CHECK (typeof(attempt_number) = 'integer' AND attempt_number BETWEEN 1 AND 256),
    CHECK (typeof(started_at_us) = 'integer' AND started_at_us > 0),

    UNIQUE (workspace_id, run_step_id, attempt_number),

    FOREIGN KEY (workspace_id, run_step_id)
        REFERENCES omnivia_runtime_run_steps (workspace_id, run_step_id),
    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_runtime_runs (workspace_id, run_id)
) WITHOUT ROWID;

-- The single terminalization of one attempt.
--
-- Keyed by the attempt alone, so "an attempt terminalizes exactly once" is enforced by
-- the primary key. An attempt with no row here is still running.
--
-- `uncertain` carries no failure, and the check refuses one: uncertainty is not
-- failure, and attaching an error to it would licence exactly the blind retry the
-- uncertainty forbids. A failed attempt must state its failure, as canonical JSON
-- stored beside the digest and byte length of the bytes that were hashed.
CREATE TABLE IF NOT EXISTS omnivia_runtime_attempt_outcomes (
    workspace_id        TEXT    NOT NULL,
    attempt_id          TEXT    NOT NULL,
    status              TEXT    NOT NULL,
    finished_at_us      INTEGER NOT NULL,
    failure_json        TEXT,
    failure_digest      TEXT,
    failure_byte_length INTEGER,

    PRIMARY KEY (workspace_id, attempt_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(attempt_id) = 'text' AND length(attempt_id) BETWEEN 1 AND 128
           AND attempt_id GLOB '[A-Za-z0-9]*'
           AND attempt_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(attempt_id, char(0)) = 0),
    CHECK (status IN ('succeeded', 'failed', 'cancelled', 'uncertain')),
    CHECK (typeof(finished_at_us) = 'integer' AND finished_at_us > 0),
    CHECK (failure_json IS NULL OR (typeof(failure_json) = 'text'
           AND length(CAST(failure_json AS BLOB)) BETWEEN 2 AND 65536)),
    CHECK (failure_digest IS NULL OR (typeof(failure_digest) = 'text'
           AND length(failure_digest) = 71
           AND substr(failure_digest, 1, 7) = 'sha256:'
           AND substr(failure_digest, 8) NOT GLOB '*[^0-9a-f]*')),
    CHECK (failure_byte_length IS NULL OR (
           typeof(failure_byte_length) = 'integer'
           AND failure_byte_length = length(CAST(failure_json AS BLOB)))),
    CHECK (
        (status = 'failed' AND failure_json IS NOT NULL
         AND failure_digest IS NOT NULL AND failure_byte_length IS NOT NULL)
        OR (status <> 'failed' AND failure_json IS NULL
            AND failure_digest IS NULL AND failure_byte_length IS NULL)
    ),

    FOREIGN KEY (workspace_id, attempt_id)
        REFERENCES omnivia_runtime_attempts (workspace_id, attempt_id)
) WITHOUT ROWID;

-- One durable suspension of a run, recorded when it is created.
--
-- First-class rather than a scheduler detail, because a suspended run is a state the
-- contract must be able to state. `resume_digest` binds the state the wait resumes
-- from, so a resolution can prove it is resuming the state that was suspended rather
-- than some later one, and it is the same canonical `ContentChecksum` shape the import
-- contract uses -- a value both sides recompute, never an opaque server token.
--
-- The kind fixes what the wait is waiting for, and therefore which resolution may
-- resolve it. Nothing here requeues a job: `ResolveWait` is a Runtime command, and
-- this migration adds no command handling of any kind.
CREATE TABLE IF NOT EXISTS omnivia_runtime_waits (
    workspace_id  TEXT    NOT NULL,
    wait_id       TEXT    NOT NULL,
    run_id        TEXT    NOT NULL,
    run_step_id   TEXT    NOT NULL,
    kind          TEXT    NOT NULL,
    created_at_us INTEGER NOT NULL,
    expires_at_us INTEGER,
    resume_digest TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, wait_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(wait_id) = 'text' AND length(wait_id) BETWEEN 1 AND 128
           AND wait_id GLOB '[A-Za-z0-9]*'
           AND wait_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(wait_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (typeof(run_step_id) = 'text' AND length(run_step_id) BETWEEN 1 AND 128
           AND run_step_id GLOB '[A-Za-z0-9]*'
           AND run_step_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_step_id, char(0)) = 0),
    CHECK (kind IN ('approval', 'external_signal', 'timer')),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (expires_at_us IS NULL OR (typeof(expires_at_us) = 'integer'
           AND expires_at_us >= created_at_us)),
    CHECK (typeof(resume_digest) = 'text' AND length(resume_digest) = 71
           AND substr(resume_digest, 1, 7) = 'sha256:'
           AND substr(resume_digest, 8) NOT GLOB '*[^0-9a-f]*'),

    FOREIGN KEY (workspace_id, run_step_id)
        REFERENCES omnivia_runtime_run_steps (workspace_id, run_step_id),
    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_runtime_runs (workspace_id, run_id)
) WITHOUT ROWID;

-- The single resolution of one wait: resolved by a `ResolveWait`, expired at its
-- deadline, or cancelled because the run was.
--
-- Keyed by the wait alone, so a wait stops being pending exactly once. A wait with no
-- row here is still pending and still holding its step.
--
-- `approval_id` names the recorded approval that resolved an approval wait. It carries
-- no foreign key because RT-102 does not own the approval store: the column records
-- the identifier the `Wait` contract states, and gains a reference when that store
-- lands. The guard still refuses it on any wait that is not a resolved approval wait.
CREATE TABLE IF NOT EXISTS omnivia_runtime_wait_resolutions (
    workspace_id      TEXT    NOT NULL,
    wait_id           TEXT    NOT NULL,
    status            TEXT    NOT NULL,
    resolved_at_us    INTEGER NOT NULL,
    resolution_reason TEXT    NOT NULL,
    approval_id       TEXT,

    PRIMARY KEY (workspace_id, wait_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(wait_id) = 'text' AND length(wait_id) BETWEEN 1 AND 128
           AND wait_id GLOB '[A-Za-z0-9]*'
           AND wait_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(wait_id, char(0)) = 0),
    CHECK (status IN ('resolved', 'expired', 'cancelled')),
    CHECK (typeof(resolved_at_us) = 'integer' AND resolved_at_us > 0),
    CHECK (typeof(resolution_reason) = 'text'
           AND length(resolution_reason) BETWEEN 1 AND 128
           AND resolution_reason GLOB '[a-z]*'
           AND resolution_reason NOT GLOB '*[^a-z0-9_.]*'
           AND resolution_reason NOT GLOB '*.'
           AND resolution_reason NOT GLOB '*.[^a-z]*'),
    CHECK (approval_id IS NULL OR (typeof(approval_id) = 'text'
           AND length(approval_id) BETWEEN 1 AND 128
           AND approval_id GLOB '[A-Za-z0-9]*'
           AND approval_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(approval_id, char(0)) = 0)),
    CHECK (approval_id IS NULL OR status = 'resolved'),

    FOREIGN KEY (workspace_id, wait_id)
        REFERENCES omnivia_runtime_waits (workspace_id, wait_id)
) WITHOUT ROWID;

-- The run's ordered event stream: contiguous from zero, never renumbered, never
-- regressing in time, and stating the run status in force at each entry.
--
-- This is where a canonical run's status lives, because the contract already requires
-- every event to carry it. Sequence zero opens the stream at `admitted` and no later
-- entry may claim it; each consecutive pair must be a legal transition; and no entry
-- may follow a terminal one, because every terminal status is a sink and recovering
-- work is a new run rather than the resurrection of a closed one. `uncertain` is
-- deliberately not terminal: an unreconciled effect is an open question, not an
-- outcome, so a run holding one can still move.
CREATE TABLE IF NOT EXISTS omnivia_runtime_events (
    workspace_id        TEXT    NOT NULL,
    run_id              TEXT    NOT NULL,
    sequence            INTEGER NOT NULL,
    runtime_event_id    TEXT    NOT NULL,
    occurred_at_us      INTEGER NOT NULL,
    event_kind          TEXT    NOT NULL,
    run_status          TEXT    NOT NULL,
    run_step_id         TEXT,
    message             TEXT,
    details_json        TEXT,
    details_digest      TEXT,
    details_byte_length INTEGER,

    PRIMARY KEY (workspace_id, run_id, sequence),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (typeof(sequence) = 'integer' AND sequence BETWEEN 0 AND 999),
    CHECK (typeof(runtime_event_id) = 'text'
           AND length(runtime_event_id) BETWEEN 1 AND 128
           AND runtime_event_id GLOB '[A-Za-z0-9]*'
           AND runtime_event_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(runtime_event_id, char(0)) = 0),
    CHECK (typeof(occurred_at_us) = 'integer' AND occurred_at_us > 0),
    CHECK (typeof(event_kind) = 'text' AND length(event_kind) BETWEEN 1 AND 128
           AND event_kind GLOB '[a-z]*' AND event_kind NOT GLOB '*[^a-z0-9_.]*'
           AND event_kind NOT GLOB '*.' AND event_kind NOT GLOB '*.[^a-z]*'),
    CHECK (run_status IN ('admitted', 'running', 'waiting', 'succeeded',
                          'partially_completed', 'failed', 'cancelled', 'uncertain')),
    CHECK (run_step_id IS NULL OR (typeof(run_step_id) = 'text'
           AND length(run_step_id) BETWEEN 1 AND 128
           AND run_step_id GLOB '[A-Za-z0-9]*'
           AND run_step_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_step_id, char(0)) = 0)),
    CHECK (message IS NULL OR (typeof(message) = 'text'
           AND length(message) BETWEEN 0 AND 2048
           AND instr(message, char(0)) = 0)),
    CHECK (details_json IS NULL OR (typeof(details_json) = 'text'
           AND length(CAST(details_json AS BLOB)) BETWEEN 2 AND 65536)),
    CHECK (details_digest IS NULL OR (typeof(details_digest) = 'text'
           AND length(details_digest) = 71
           AND substr(details_digest, 1, 7) = 'sha256:'
           AND substr(details_digest, 8) NOT GLOB '*[^0-9a-f]*')),
    CHECK (details_byte_length IS NULL OR (
           typeof(details_byte_length) = 'integer'
           AND details_byte_length = length(CAST(details_json AS BLOB)))),
    CHECK (
        (details_json IS NULL AND details_digest IS NULL
         AND details_byte_length IS NULL)
        OR (details_json IS NOT NULL AND details_digest IS NOT NULL
            AND details_byte_length IS NOT NULL)
    ),

    UNIQUE (workspace_id, runtime_event_id),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_runtime_runs (workspace_id, run_id),
    FOREIGN KEY (workspace_id, run_step_id)
        REFERENCES omnivia_runtime_run_steps (workspace_id, run_step_id)
) WITHOUT ROWID;

-- Four indexes, one per read the repositories actually perform. The composite primary
-- keys and unique constraints already index every other access path this slice uses.
CREATE INDEX IF NOT EXISTS omnivia_idx_runtime_run_step_states_latest
    ON omnivia_runtime_run_step_states (workspace_id, run_step_id, state_sequence DESC);
CREATE INDEX IF NOT EXISTS omnivia_idx_runtime_attempts_run
    ON omnivia_runtime_attempts (workspace_id, run_id, run_step_id, attempt_number);
CREATE INDEX IF NOT EXISTS omnivia_idx_runtime_waits_run
    ON omnivia_runtime_waits (workspace_id, run_id, created_at_us, wait_id);
CREATE INDEX IF NOT EXISTS omnivia_idx_runtime_waits_step
    ON omnivia_runtime_waits (workspace_id, run_step_id, created_at_us);

-- The eight INSERT guards below each repeat the complete connection-authority,
-- mutation-guard, workspace-state and lease predicate `0005` established, then add the
-- rules specific to the record they admit. UPDATE and DELETE abort unconditionally on
-- all eight tables -- for the current fenced owner too, because a run history the owner
-- may rewrite is not a run history, and current fenced ownership is authority to
-- append, never authority to revise.

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_runs_insert
BEFORE INSERT ON omnivia_runtime_runs
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_runs')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining'))
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: a run must be admitted onto an open durable job')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_durable_jobs
        WHERE job_id = NEW.job_id AND state IN ('queued', 'claimed'));
    SELECT RAISE(ABORT, 'omnivia: run audit workspace mismatch')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events
        WHERE audit_ref = NEW.audit_ref AND workspace_id = NEW.workspace_id);
    SELECT RAISE(ABORT, 'omnivia: a run must agree with the application metadata of its job')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_job_application_metadata
        WHERE workspace_id = NEW.workspace_id AND job_id = NEW.job_id
          AND originating_operation = NEW.originating_operation
          AND audit_ref = NEW.audit_ref);
    SELECT RAISE(ABORT, 'omnivia: a durable job may carry only one canonical run')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_runtime_runs
        WHERE workspace_id = NEW.workspace_id AND job_id = NEW.job_id);
    SELECT RAISE(ABORT, 'omnivia: a run must agree with the idempotency claim admitting it')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_idempotency_claims
        WHERE claim_id = NEW.claim_id AND workspace_id = NEW.workspace_id
          AND operation = NEW.originating_operation
          AND idempotency_key = NEW.logical_key
          AND audit_ref = NEW.audit_ref);
    SELECT RAISE(ABORT, 'omnivia: this idempotency claim already admitted a run')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_runtime_runs
        WHERE workspace_id = NEW.workspace_id AND claim_id = NEW.claim_id);
    SELECT RAISE(ABORT, 'omnivia: this logical key already names a run in this workspace')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_runtime_runs
        WHERE workspace_id = NEW.workspace_id AND logical_key = NEW.logical_key);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_runs_update
BEFORE UPDATE ON omnivia_runtime_runs
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_runs is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_runs_delete
BEFORE DELETE ON omnivia_runtime_runs
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_runs is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_run_steps_insert
BEFORE INSERT ON omnivia_runtime_run_steps
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_run_steps')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining'))
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: a step must not predate the run it belongs to')
    WHERE NEW.created_at_us < (
        SELECT created_at_us FROM omnivia_runtime_runs
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: step ordinals must be contiguous within their run')
    WHERE NEW.ordinal IS NOT (
        SELECT COALESCE(MAX(ordinal), 0) + 1 FROM omnivia_runtime_run_steps
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: a terminal run admits no further history')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_runtime_events
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id
          AND run_status IN ('succeeded', 'partially_completed', 'failed', 'cancelled'));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_run_steps_update
BEFORE UPDATE ON omnivia_runtime_run_steps
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_run_steps is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_run_steps_delete
BEFORE DELETE ON omnivia_runtime_run_steps
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_run_steps is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_run_step_states_insert
BEFORE INSERT ON omnivia_runtime_run_step_states
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_run_step_states')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining'))
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: step state sequence must be contiguous')
    WHERE NEW.state_sequence IS NOT (
        SELECT COALESCE(MAX(state_sequence), -1) + 1
        FROM omnivia_runtime_run_step_states
        WHERE workspace_id = NEW.workspace_id AND run_step_id = NEW.run_step_id);
    SELECT RAISE(ABORT, 'omnivia: a step state must not predate the step')
    WHERE NEW.observed_at_us < (
        SELECT created_at_us FROM omnivia_runtime_run_steps
        WHERE workspace_id = NEW.workspace_id AND run_step_id = NEW.run_step_id);
    SELECT RAISE(ABORT, 'omnivia: step state time must not regress')
    WHERE NEW.state_sequence > 0
      AND NEW.observed_at_us < (
        SELECT observed_at_us FROM omnivia_runtime_run_step_states
        WHERE workspace_id = NEW.workspace_id AND run_step_id = NEW.run_step_id
          AND state_sequence = NEW.state_sequence - 1);
    SELECT RAISE(ABORT, 'omnivia: a step state that restates its predecessor records nothing')
    WHERE NEW.state_sequence > 0
      AND NEW.status IS (
        SELECT status FROM omnivia_runtime_run_step_states
        WHERE workspace_id = NEW.workspace_id AND run_step_id = NEW.run_step_id
          AND state_sequence = NEW.state_sequence - 1);
    SELECT RAISE(ABORT, 'omnivia: a terminal step state is final')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_runtime_run_step_states
        WHERE workspace_id = NEW.workspace_id AND run_step_id = NEW.run_step_id
          AND status IN ('succeeded', 'failed', 'cancelled', 'skipped'));
    SELECT RAISE(ABORT, 'omnivia: a waiting step must name the wait holding it')
    WHERE NEW.status = 'waiting'
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_waits w
        LEFT JOIN omnivia_runtime_wait_resolutions r
          ON r.workspace_id = w.workspace_id AND r.wait_id = w.wait_id
        WHERE w.workspace_id = NEW.workspace_id AND w.run_step_id = NEW.run_step_id
          AND r.wait_id IS NULL);
    SELECT RAISE(ABORT, 'omnivia: a terminal run admits only terminal step closure')
    WHERE NEW.status NOT IN ('succeeded', 'failed', 'cancelled', 'skipped')
      AND EXISTS (
        SELECT 1 FROM omnivia_runtime_run_steps s
        JOIN omnivia_runtime_events e
          ON e.workspace_id = s.workspace_id AND e.run_id = s.run_id
        WHERE s.workspace_id = NEW.workspace_id AND s.run_step_id = NEW.run_step_id
          AND e.run_status IN ('succeeded', 'partially_completed', 'failed', 'cancelled'));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_run_step_states_update
BEFORE UPDATE ON omnivia_runtime_run_step_states
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_run_step_states is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_run_step_states_delete
BEFORE DELETE ON omnivia_runtime_run_step_states
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_run_step_states is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_attempts_insert
BEFORE INSERT ON omnivia_runtime_attempts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_attempts')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining'))
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: an attempt must name the run of its own step')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_run_steps
        WHERE workspace_id = NEW.workspace_id AND run_step_id = NEW.run_step_id
          AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: an attempt must not predate its step')
    WHERE NEW.started_at_us < (
        SELECT created_at_us FROM omnivia_runtime_run_steps
        WHERE workspace_id = NEW.workspace_id AND run_step_id = NEW.run_step_id);
    SELECT RAISE(ABORT, 'omnivia: attempt number must be contiguous within its step')
    WHERE NEW.attempt_number IS NOT (
        SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM omnivia_runtime_attempts
        WHERE workspace_id = NEW.workspace_id AND run_step_id = NEW.run_step_id);
    SELECT RAISE(ABORT, 'omnivia: only a failed, cancelled or uncertain attempt may be followed by another')
    WHERE NEW.attempt_number > 1
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_attempts a
        JOIN omnivia_runtime_attempt_outcomes o
          ON o.workspace_id = a.workspace_id AND o.attempt_id = a.attempt_id
        WHERE a.workspace_id = NEW.workspace_id AND a.run_step_id = NEW.run_step_id
          AND a.attempt_number = NEW.attempt_number - 1
          AND o.status IN ('failed', 'cancelled', 'uncertain'));
    SELECT RAISE(ABORT, 'omnivia: attempts of one step must not overlap')
    WHERE NEW.attempt_number > 1
      AND NEW.started_at_us < (
        SELECT o.finished_at_us FROM omnivia_runtime_attempts a
        JOIN omnivia_runtime_attempt_outcomes o
          ON o.workspace_id = a.workspace_id AND o.attempt_id = a.attempt_id
        WHERE a.workspace_id = NEW.workspace_id AND a.run_step_id = NEW.run_step_id
          AND a.attempt_number = NEW.attempt_number - 1);
    SELECT RAISE(ABORT, 'omnivia: a terminal step admits no further attempt')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_runtime_run_step_states
        WHERE workspace_id = NEW.workspace_id AND run_step_id = NEW.run_step_id
          AND status IN ('succeeded', 'failed', 'cancelled', 'skipped'));
    SELECT RAISE(ABORT, 'omnivia: a terminal run admits no further history')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_runtime_events
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id
          AND run_status IN ('succeeded', 'partially_completed', 'failed', 'cancelled'));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_attempts_update
BEFORE UPDATE ON omnivia_runtime_attempts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_attempts is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_attempts_delete
BEFORE DELETE ON omnivia_runtime_attempts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_attempts is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_attempt_outcomes_insert
BEFORE INSERT ON omnivia_runtime_attempt_outcomes
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_attempt_outcomes')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining'))
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: an attempt cannot finish before it started')
    WHERE NEW.finished_at_us < (
        SELECT started_at_us FROM omnivia_runtime_attempts
        WHERE workspace_id = NEW.workspace_id AND attempt_id = NEW.attempt_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_attempt_outcomes_update
BEFORE UPDATE ON omnivia_runtime_attempt_outcomes
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_attempt_outcomes is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_attempt_outcomes_delete
BEFORE DELETE ON omnivia_runtime_attempt_outcomes
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_attempt_outcomes is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_waits_insert
BEFORE INSERT ON omnivia_runtime_waits
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_waits')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining'))
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: a wait must name the run of its own step')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_run_steps
        WHERE workspace_id = NEW.workspace_id AND run_step_id = NEW.run_step_id
          AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: a wait must not predate its step')
    WHERE NEW.created_at_us < (
        SELECT created_at_us FROM omnivia_runtime_run_steps
        WHERE workspace_id = NEW.workspace_id AND run_step_id = NEW.run_step_id);
    SELECT RAISE(ABORT, 'omnivia: a step may hold only one unresolved wait')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_runtime_waits w
        LEFT JOIN omnivia_runtime_wait_resolutions r
          ON r.workspace_id = w.workspace_id AND r.wait_id = w.wait_id
        WHERE w.workspace_id = NEW.workspace_id AND w.run_step_id = NEW.run_step_id
          AND r.wait_id IS NULL);
    SELECT RAISE(ABORT, 'omnivia: a terminal step admits no further wait')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_runtime_run_step_states
        WHERE workspace_id = NEW.workspace_id AND run_step_id = NEW.run_step_id
          AND status IN ('succeeded', 'failed', 'cancelled', 'skipped'));
    SELECT RAISE(ABORT, 'omnivia: a run may hold at most 64 waits')
    WHERE 64 <= (
        SELECT COUNT(*) FROM omnivia_runtime_waits
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: a terminal run admits no further history')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_runtime_events
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id
          AND run_status IN ('succeeded', 'partially_completed', 'failed', 'cancelled'));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_waits_update
BEFORE UPDATE ON omnivia_runtime_waits
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_waits is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_waits_delete
BEFORE DELETE ON omnivia_runtime_waits
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_waits is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_wait_resolutions_insert
BEFORE INSERT ON omnivia_runtime_wait_resolutions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_wait_resolutions')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining'))
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: a wait cannot be resolved before it was created')
    WHERE NEW.resolved_at_us < (
        SELECT created_at_us FROM omnivia_runtime_waits
        WHERE workspace_id = NEW.workspace_id AND wait_id = NEW.wait_id);
    SELECT RAISE(ABORT, 'omnivia: only a resolved approval wait names an approval')
    WHERE NEW.approval_id IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_waits
        WHERE workspace_id = NEW.workspace_id AND wait_id = NEW.wait_id
          AND kind = 'approval');
    SELECT RAISE(ABORT, 'omnivia: a wait resolved past its deadline has expired, not resolved')
    WHERE NEW.status = 'resolved'
      AND NEW.resolved_at_us > COALESCE((
        SELECT expires_at_us FROM omnivia_runtime_waits
        WHERE workspace_id = NEW.workspace_id AND wait_id = NEW.wait_id),
        NEW.resolved_at_us);
    SELECT RAISE(ABORT, 'omnivia: only a time-bounded wait expires, and never before its deadline')
    WHERE NEW.status = 'expired'
      AND NEW.resolved_at_us < COALESCE((
        SELECT expires_at_us FROM omnivia_runtime_waits
        WHERE workspace_id = NEW.workspace_id AND wait_id = NEW.wait_id),
        NEW.resolved_at_us + 1);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_wait_resolutions_update
BEFORE UPDATE ON omnivia_runtime_wait_resolutions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_wait_resolutions is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_wait_resolutions_delete
BEFORE DELETE ON omnivia_runtime_wait_resolutions
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_wait_resolutions is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_events_insert
BEFORE INSERT ON omnivia_runtime_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_events')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining'))
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: run event sequence must be contiguous from zero')
    WHERE NEW.sequence IS NOT (
        SELECT COALESCE(MAX(sequence), -1) + 1 FROM omnivia_runtime_events
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: a run stream opens at sequence zero with the admitted status')
    WHERE (NEW.sequence = 0 AND NEW.run_status <> 'admitted')
       OR (NEW.sequence > 0 AND NEW.run_status = 'admitted');
    SELECT RAISE(ABORT, 'omnivia: a run event must not predate its run')
    WHERE NEW.occurred_at_us < (
        SELECT created_at_us FROM omnivia_runtime_runs
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: run event time must not regress')
    WHERE NEW.sequence > 0
      AND NEW.occurred_at_us < (
        SELECT occurred_at_us FROM omnivia_runtime_events
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id
          AND sequence = NEW.sequence - 1);
    SELECT RAISE(ABORT, 'omnivia: a terminal run event is final')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_runtime_events
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id
          AND run_status IN ('succeeded', 'partially_completed', 'failed', 'cancelled'));
    SELECT RAISE(ABORT, 'omnivia: run status transition is not permitted')
    WHERE NEW.sequence > 0
      AND EXISTS (
        SELECT 1 FROM omnivia_runtime_events p
        WHERE p.workspace_id = NEW.workspace_id AND p.run_id = NEW.run_id
          AND p.sequence = NEW.sequence - 1
          AND p.run_status <> NEW.run_status
          AND NOT (
            (p.run_status = 'admitted'
             AND NEW.run_status IN ('running', 'cancelled'))
            OR (p.run_status = 'running'
                AND NEW.run_status IN ('waiting', 'succeeded', 'partially_completed',
                                       'failed', 'cancelled', 'uncertain'))
            OR (p.run_status = 'waiting'
                AND NEW.run_status IN ('running', 'cancelled', 'failed'))
            OR (p.run_status = 'uncertain'
                AND NEW.run_status IN ('running', 'succeeded', 'partially_completed',
                                       'failed', 'cancelled'))));
    SELECT RAISE(ABORT, 'omnivia: a run event may only name a step of its own run')
    WHERE NEW.run_step_id IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_run_steps
        WHERE workspace_id = NEW.workspace_id AND run_step_id = NEW.run_step_id
          AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: a runtime event identifier is used at most once per workspace')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_runtime_events
        WHERE workspace_id = NEW.workspace_id
          AND runtime_event_id = NEW.runtime_event_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_events_update
BEFORE UPDATE ON omnivia_runtime_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_events is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_events_delete
BEFORE DELETE ON omnivia_runtime_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_events is append-only; DELETE is never permitted');
END;
