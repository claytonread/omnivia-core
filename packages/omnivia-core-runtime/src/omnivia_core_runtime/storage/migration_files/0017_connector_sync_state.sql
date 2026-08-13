-- Durable connector synchronisation state (V06-8).
--
-- Three append-only tables and nothing else. No connector implementation, no
-- scheduler, no credential store, no bidirectional write-back, and no new
-- application operation: this slice is the durable record a synchronisation run
-- leaves behind, and the code that produces it lives above the schema.
--
--   omnivia_connector_sync_runs      which durable job was which connector's run
--   omnivia_connector_dead_letters   the items a run could not ingest, classified
--   omnivia_connector_health_events  what each connector said about its source
--
-- What is deliberately absent is a fourth table. The resumable cursor is *not*
-- stored here: it is an `omnivia_job_checkpoints` row of kind `connector.cursor`
-- under the run's own attempt, which is the checkpoint authority migration 0010
-- already established. Adding a second place a cursor could live would mean two
-- answers to "where did we get to" and a rule, written nowhere, about which one
-- wins after a crash. What this slice adds instead is the missing edge --
-- connector to run -- which is exactly what makes the existing per-job
-- checkpoints readable as one connector's continuous history:
--
--   ORDER BY sync_runs.sync_sequence DESC, job_checkpoints.checkpoint_sequence DESC
--
-- is a total order over every cursor a connector has ever committed, so the
-- resume point after any crash is a single deterministic row.
--
-- Every comment in this file sits *between* statements and never inside one. The
-- migrator applies these through `split_sql_statements`, which strips comments,
-- while `canonical_schema_fingerprint()` replays the same text with
-- `executescript`, which does not. A comment inside a `CREATE` body is therefore
-- stored in `sqlite_master` by one path and not the other, and a clean workspace
-- would be reported as drifted.

-- One connector's synchronisation run, bound to the durable job that carried it.
--
-- `sync_sequence` is per connector and contiguous, so the runs of one connector
-- are totally ordered without depending on a timestamp two runs could share.
--
-- `state_version` is the connector's own state format at the time of the run,
-- recorded rather than inferred: a cursor is only interpretable by the version
-- that wrote it, and a run resuming from an earlier run's checkpoint has to be
-- able to see which version that was. It may advance between runs and may never
-- go backwards -- a downgrade would read a newer token under older rules, which
-- is precisely the silent corruption a version exists to prevent.
--
-- The run is an `ingestion.import` durable job because that is the job kind
-- `omnivia_evidence_artifacts.import_run_id` accepts, so evidence this run
-- captures names this run. The trigger requires that rather than trusting it.
CREATE TABLE IF NOT EXISTS omnivia_connector_sync_runs (
    workspace_id  TEXT    NOT NULL,
    connector_id  TEXT    NOT NULL,
    sync_sequence INTEGER NOT NULL,
    run_id        TEXT    NOT NULL,
    source_kind   TEXT    NOT NULL,
    state_version INTEGER NOT NULL,
    started_at_us INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, connector_id, sync_sequence),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(connector_id) = 'text' AND length(connector_id) BETWEEN 1 AND 128
           AND connector_id GLOB '[A-Za-z0-9]*'
           AND connector_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(connector_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:/-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (typeof(source_kind) = 'text' AND length(source_kind) BETWEEN 1 AND 128
           AND source_kind GLOB '[a-z]*'
           AND source_kind NOT GLOB '*[^a-z0-9_.]*'
           AND source_kind NOT GLOB '*.'
           AND source_kind NOT GLOB '*.[^a-z]*'
           AND instr(source_kind, char(0)) = 0),
    CHECK (typeof(sync_sequence) = 'integer' AND sync_sequence > 0),
    CHECK (typeof(state_version) = 'integer' AND state_version > 0),
    CHECK (typeof(started_at_us) = 'integer' AND started_at_us > 0),

    UNIQUE (workspace_id, run_id),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_job_application_metadata (workspace_id, job_id)
) WITHOUT ROWID;

-- The composite key the dead-letter foreign key resolves against. Declared as a
-- unique index rather than a second primary key: the run's identity is still
-- (workspace, connector, sequence), and this only makes "this run belongs to this
-- connector" an enforceable reference instead of a convention.
CREATE UNIQUE INDEX IF NOT EXISTS omnivia_idx_connector_sync_runs_connector_run
    ON omnivia_connector_sync_runs (workspace_id, connector_id, run_id);

-- One source item a run could not ingest, with the classification that decided it.
--
-- A dead letter is evidence of a decision already taken, not a queue: the run
-- retried what its classification said was worth retrying and gave up here. That
-- is why `attempts` is recorded and why the row is append-only -- a later,
-- successful ingestion of the same item does not erase the fact that this run
-- could not, and nothing about this row is a signal to try again.
CREATE TABLE IF NOT EXISTS omnivia_connector_dead_letters (
    dead_letter_id   TEXT    NOT NULL PRIMARY KEY,
    workspace_id     TEXT    NOT NULL,
    connector_id     TEXT    NOT NULL,
    run_id           TEXT    NOT NULL,
    source_native_id TEXT    NOT NULL,
    failure_code     TEXT    NOT NULL,
    retry_class      TEXT    NOT NULL,
    message          TEXT    NOT NULL,
    attempts         INTEGER NOT NULL,
    recorded_at_us   INTEGER NOT NULL,

    CHECK (typeof(dead_letter_id) = 'text'
           AND length(dead_letter_id) BETWEEN 1 AND 128
           AND dead_letter_id GLOB '[A-Za-z0-9]*'
           AND dead_letter_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(dead_letter_id, char(0)) = 0),
    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(connector_id) = 'text' AND length(connector_id) BETWEEN 1 AND 128
           AND connector_id GLOB '[A-Za-z0-9]*'
           AND connector_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(connector_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:/-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (typeof(source_native_id) = 'text'
           AND length(source_native_id) BETWEEN 1 AND 128
           AND source_native_id GLOB '[A-Za-z0-9]*'
           AND source_native_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(source_native_id, char(0)) = 0),
    CHECK (typeof(failure_code) = 'text' AND length(failure_code) BETWEEN 1 AND 128
           AND failure_code GLOB '[a-z]*'
           AND failure_code NOT GLOB '*[^a-z0-9_.]*'
           AND failure_code NOT GLOB '*.'
           AND failure_code NOT GLOB '*.[^a-z]*'
           AND instr(failure_code, char(0)) = 0),
    CHECK (retry_class IN ('non_retryable', 'retryable', 'retryable_after_delay',
                           'retryable_after_precondition_refresh')),
    CHECK (typeof(message) = 'text' AND length(message) BETWEEN 1 AND 2048
           AND instr(message, char(0)) = 0),
    CHECK (typeof(attempts) = 'integer' AND attempts BETWEEN 1 AND 256),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),

    FOREIGN KEY (workspace_id, connector_id, run_id)
        REFERENCES omnivia_connector_sync_runs (workspace_id, connector_id, run_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_connector_dead_letters_run
    ON omnivia_connector_dead_letters (workspace_id, connector_id, recorded_at_us);

-- What a connector said about its source, in order, forever.
--
-- Health is a stream and not a column for the same reason a tombstone is: the
-- interesting question is almost never "is it healthy now" but "when did it stop
-- being healthy, and what was running then". A rewritable column answers the
-- first and destroys the second.
CREATE TABLE IF NOT EXISTS omnivia_connector_health_events (
    health_event_id TEXT    NOT NULL PRIMARY KEY,
    workspace_id    TEXT    NOT NULL,
    connector_id    TEXT    NOT NULL,
    health_sequence INTEGER NOT NULL,
    health_state    TEXT    NOT NULL,
    detail          TEXT,
    observed_at_us  INTEGER NOT NULL,

    CHECK (typeof(health_event_id) = 'text'
           AND length(health_event_id) BETWEEN 1 AND 128
           AND health_event_id GLOB '[A-Za-z0-9]*'
           AND health_event_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(health_event_id, char(0)) = 0),
    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(connector_id) = 'text' AND length(connector_id) BETWEEN 1 AND 128
           AND connector_id GLOB '[A-Za-z0-9]*'
           AND connector_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(connector_id, char(0)) = 0),
    CHECK (typeof(health_sequence) = 'integer' AND health_sequence > 0),
    CHECK (health_state IN ('healthy', 'degraded', 'unavailable')),
    CHECK (detail IS NULL OR (typeof(detail) = 'text'
           AND length(detail) BETWEEN 1 AND 2048
           AND instr(detail, char(0)) = 0)),
    CHECK (typeof(observed_at_us) = 'integer' AND observed_at_us > 0),

    UNIQUE (workspace_id, connector_id, health_sequence)
) WITHOUT ROWID;

-- The three INSERT guards below each repeat the complete connection-authority,
-- mutation-guard, workspace-state and lease predicate `0005` established, then add
-- the rules that are specific to this slice. UPDATE and DELETE abort
-- unconditionally on all three tables -- for the current fenced owner too, because
-- a run history the owner may rewrite is not a run history.

CREATE TRIGGER IF NOT EXISTS omnivia_guard_connector_sync_runs_insert
BEFORE INSERT ON omnivia_connector_sync_runs
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_connector_sync_runs')
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
    SELECT RAISE(ABORT, 'omnivia: connector sync sequence must be contiguous')
    WHERE NEW.sync_sequence IS NOT (
        SELECT COALESCE(MAX(sync_sequence), 0) + 1
        FROM omnivia_connector_sync_runs
        WHERE workspace_id = NEW.workspace_id AND connector_id = NEW.connector_id);
    SELECT RAISE(ABORT, 'omnivia: connector state version may not go backwards')
    WHERE NEW.state_version < COALESCE((
        SELECT state_version FROM omnivia_connector_sync_runs
        WHERE workspace_id = NEW.workspace_id AND connector_id = NEW.connector_id
          AND sync_sequence = NEW.sync_sequence - 1), NEW.state_version);
    SELECT RAISE(ABORT, 'omnivia: connector source kind is immutable across runs')
    WHERE NEW.source_kind IS NOT COALESCE((
        SELECT source_kind FROM omnivia_connector_sync_runs
        WHERE workspace_id = NEW.workspace_id AND connector_id = NEW.connector_id
          AND sync_sequence = NEW.sync_sequence - 1), NEW.source_kind);
    SELECT RAISE(ABORT, 'omnivia: a connector sync run must be an ingestion.import durable job')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_durable_jobs
        WHERE job_id = NEW.run_id AND job_type = 'ingestion.import');
    SELECT RAISE(ABORT, 'omnivia: a durable job may carry only one connector sync run')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_connector_sync_runs
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_connector_sync_runs_update
BEFORE UPDATE ON omnivia_connector_sync_runs
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_connector_sync_runs is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_connector_sync_runs_delete
BEFORE DELETE ON omnivia_connector_sync_runs
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_connector_sync_runs is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_connector_dead_letters_insert
BEFORE INSERT ON omnivia_connector_dead_letters
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_connector_dead_letters')
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
    SELECT RAISE(ABORT, 'omnivia: a dead letter must name one item of its run at most once')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_connector_dead_letters
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id
          AND source_native_id = NEW.source_native_id);
    SELECT RAISE(ABORT, 'omnivia: a dead letter must not predate the run that recorded it')
    WHERE NEW.recorded_at_us < (
        SELECT started_at_us FROM omnivia_connector_sync_runs
        WHERE workspace_id = NEW.workspace_id AND connector_id = NEW.connector_id
          AND run_id = NEW.run_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_connector_dead_letters_update
BEFORE UPDATE ON omnivia_connector_dead_letters
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_connector_dead_letters is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_connector_dead_letters_delete
BEFORE DELETE ON omnivia_connector_dead_letters
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_connector_dead_letters is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_connector_health_events_insert
BEFORE INSERT ON omnivia_connector_health_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_connector_health_events')
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
    SELECT RAISE(ABORT, 'omnivia: connector health sequence must be contiguous')
    WHERE NEW.health_sequence IS NOT (
        SELECT COALESCE(MAX(health_sequence), 0) + 1
        FROM omnivia_connector_health_events
        WHERE workspace_id = NEW.workspace_id AND connector_id = NEW.connector_id);
    SELECT RAISE(ABORT, 'omnivia: connector health observation time must not regress')
    WHERE NEW.observed_at_us < COALESCE((
        SELECT observed_at_us FROM omnivia_connector_health_events
        WHERE workspace_id = NEW.workspace_id AND connector_id = NEW.connector_id
          AND health_sequence = NEW.health_sequence - 1), NEW.observed_at_us);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_connector_health_events_update
BEFORE UPDATE ON omnivia_connector_health_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_connector_health_events is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_connector_health_events_delete
BEFORE DELETE ON omnivia_connector_health_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_connector_health_events is append-only; DELETE is never permitted');
END;
