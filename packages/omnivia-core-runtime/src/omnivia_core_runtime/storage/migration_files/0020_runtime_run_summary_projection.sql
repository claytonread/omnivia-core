-- The active Run summary projection: transactional derived state (RT-105).
--
-- Additive only. One derived table and three statement triggers, on top of the
-- canonical records migrations 0018 and 0019 added.
--
--   omnivia_runtime_run_summaries   one product-facing summary per canonical run
--
-- Why this is not migration 0011's lifecycle. 0011 owns versioned build, validate and
-- activate for a snapshot index that is refreshed independently of the writes it
-- describes, and an activation pointer is exactly right for that: a build runs for a
-- while, is attested, and then becomes visible all at once. This projection is the
-- opposite shape. It must be correct at the instant a canonical `RuntimeEvent`
-- commits, because a caller that reads a run's status immediately after a command
-- settled must not be told the previous one. So it is a direct derived table with a
-- per-run cursor, written in the same fenced transaction as the event it reflects.
-- There is no second build pointer, no second event ledger and no second cursor
-- authority: `last_sequence` is a copy of where this row stands in a stream the
-- canonical events table still owns, and the triggers below refuse a copy that
-- disagrees with it.
--
-- Why UPDATE and DELETE are permitted here, and nowhere else in the runtime. Every
-- canonical runtime table is append-only, because history the owner may rewrite is
-- not history. This table holds no history: every column is a function of
-- `omnivia_runtime_runs` and `omnivia_runtime_events`, and a row that was destroyed
-- could be recomputed byte for byte from them. That is what makes advancing a summary
-- in place, and replacing the whole workspace's summaries during a rebuild, safe --
-- and it is why both statements still require the complete current
-- service-writer/fence/lease authority every other guard in this database requires.
-- Losing this table costs a replay; corrupting it under stale authority would not.
--
-- What is projected, and what is deliberately not. Run identity, the durable job
-- carrying it, the definition, the logical key, the originating operation, the audit
-- reference, the current status, the admission instant, the latest event's instant, a
-- terminal instant when the latest status is terminal, and the cursor naming the event
-- this row was derived from. Nothing else: Steps, Attempts, Waits, approvals, effects,
-- scheduler state, adapter state and policy or budget snapshots either belong to a
-- later slice or have no store at all, and a column for one of them would be an
-- invented fact wearing a projection's clothes.
--
-- Every column is checked against the canonical rows it copies. The insert and update
-- guards refuse a summary whose run metadata contradicts `omnivia_runtime_runs`, whose
-- cursor names an event `omnivia_runtime_events` does not hold, whose status or
-- timestamps disagree with that event, or whose cursor is not the run's latest
-- sequence -- so a projection that has silently drifted from its source cannot be
-- stored, whichever code path attempts it.
--
-- Identity, types, bounds, timestamps and digests follow 0018's rules exactly, and
-- every comment in this file sits between statements and never inside one, for the
-- fingerprint-loader reason 0018 states.

CREATE TABLE IF NOT EXISTS omnivia_runtime_run_summaries (
    workspace_id           TEXT    NOT NULL,
    run_id                 TEXT    NOT NULL,
    job_id                 TEXT    NOT NULL,
    definition_kind        TEXT    NOT NULL,
    definition_id          TEXT    NOT NULL,
    definition_version     TEXT    NOT NULL,
    logical_key            TEXT    NOT NULL,
    originating_operation  TEXT    NOT NULL,
    audit_ref              TEXT    NOT NULL,
    run_status             TEXT    NOT NULL,
    created_at_us          INTEGER NOT NULL,
    updated_at_us          INTEGER NOT NULL,
    terminal_at_us         INTEGER,
    last_runtime_event_id  TEXT    NOT NULL,
    last_sequence          INTEGER NOT NULL,

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
    CHECK (run_status IN ('admitted', 'running', 'waiting', 'succeeded',
                          'partially_completed', 'failed', 'cancelled', 'uncertain')),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (typeof(updated_at_us) = 'integer' AND updated_at_us >= created_at_us),
    CHECK (typeof(last_runtime_event_id) = 'text'
           AND length(last_runtime_event_id) BETWEEN 1 AND 128
           AND last_runtime_event_id GLOB '[A-Za-z0-9]*'
           AND last_runtime_event_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(last_runtime_event_id, char(0)) = 0),
    CHECK (typeof(last_sequence) = 'integer' AND last_sequence BETWEEN 0 AND 999),
    CHECK (
        (run_status IN ('succeeded', 'partially_completed', 'failed', 'cancelled')
         AND typeof(terminal_at_us) = 'integer' AND terminal_at_us = updated_at_us)
        OR (run_status NOT IN ('succeeded', 'partially_completed', 'failed',
                               'cancelled')
            AND terminal_at_us IS NULL)
    ),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_runtime_runs (workspace_id, run_id),
    FOREIGN KEY (workspace_id, run_id, last_sequence)
        REFERENCES omnivia_runtime_events (workspace_id, run_id, sequence)
) WITHOUT ROWID;

-- The three guards below repeat the complete connection-authority, mutation-guard,
-- workspace-state and lease predicate 0005 established, then prove the row against the
-- canonical records it is derived from. UPDATE and DELETE are permitted -- and only
-- ever under that same current authority -- because this table is derived and
-- rebuildable, which no canonical runtime table is.

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_run_summaries_insert
BEFORE INSERT ON omnivia_runtime_run_summaries
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_run_summaries')
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
    SELECT RAISE(ABORT, 'omnivia: a run summary must agree with its canonical run')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_runs
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id
          AND job_id = NEW.job_id
          AND definition_kind = NEW.definition_kind
          AND definition_id = NEW.definition_id
          AND definition_version = NEW.definition_version
          AND logical_key = NEW.logical_key
          AND originating_operation = NEW.originating_operation
          AND audit_ref = NEW.audit_ref
          AND created_at_us = NEW.created_at_us);
    SELECT RAISE(ABORT, 'omnivia: a run summary must agree with the event it names')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_events
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id
          AND sequence = NEW.last_sequence
          AND runtime_event_id = NEW.last_runtime_event_id
          AND run_status = NEW.run_status
          AND occurred_at_us = NEW.updated_at_us);
    SELECT RAISE(ABORT, 'omnivia: a run summary must name the latest event of its run')
    WHERE NEW.last_sequence IS NOT (
        SELECT MAX(sequence) FROM omnivia_runtime_events
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_run_summaries_update
BEFORE UPDATE ON omnivia_runtime_run_summaries
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_runtime_run_summaries')
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
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1)
       OR OLD.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: run summary identity and admission are immutable')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id
       OR NEW.run_id IS NOT OLD.run_id
       OR NEW.job_id IS NOT OLD.job_id
       OR NEW.definition_kind IS NOT OLD.definition_kind
       OR NEW.definition_id IS NOT OLD.definition_id
       OR NEW.definition_version IS NOT OLD.definition_version
       OR NEW.logical_key IS NOT OLD.logical_key
       OR NEW.originating_operation IS NOT OLD.originating_operation
       OR NEW.audit_ref IS NOT OLD.audit_ref
       OR NEW.created_at_us IS NOT OLD.created_at_us;
    SELECT RAISE(ABORT, 'omnivia: a run summary cursor must not regress')
    WHERE NEW.last_sequence <= OLD.last_sequence;
    SELECT RAISE(ABORT, 'omnivia: a run summary must agree with its canonical run')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_runs
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id
          AND job_id = NEW.job_id
          AND definition_kind = NEW.definition_kind
          AND definition_id = NEW.definition_id
          AND definition_version = NEW.definition_version
          AND logical_key = NEW.logical_key
          AND originating_operation = NEW.originating_operation
          AND audit_ref = NEW.audit_ref
          AND created_at_us = NEW.created_at_us);
    SELECT RAISE(ABORT, 'omnivia: a run summary must agree with the event it names')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_runtime_events
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id
          AND sequence = NEW.last_sequence
          AND runtime_event_id = NEW.last_runtime_event_id
          AND run_status = NEW.run_status
          AND occurred_at_us = NEW.updated_at_us);
    SELECT RAISE(ABORT, 'omnivia: a run summary must name the latest event of its run')
    WHERE NEW.last_sequence IS NOT (
        SELECT MAX(sequence) FROM omnivia_runtime_events
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_run_summaries_delete
BEFORE DELETE ON omnivia_runtime_run_summaries
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded DELETE on omnivia_runtime_run_summaries')
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
       OR OLD.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
END;
