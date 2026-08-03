-- Durable application job history (V06-1 M4).
--
-- `omnivia_durable_jobs` remains the mutable scheduler row. This migration adds
-- the application identity, attempt, progress, checkpoint, event and terminal
-- facts needed by the frozen provider-neutral job contract. It adds no handler,
-- repository, dispatcher, token implementation or projection state.

CREATE TABLE IF NOT EXISTS omnivia_job_application_metadata (
    workspace_id               TEXT    NOT NULL,
    job_id                     TEXT    NOT NULL,
    job_kind                   TEXT    NOT NULL,
    originating_operation      TEXT    NOT NULL,
    audit_ref                  TEXT    NOT NULL,
    created_at_us              INTEGER NOT NULL,
    terminal_result_kind       TEXT,
    supports_checkpoint_resume INTEGER NOT NULL,
    max_attempts               INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, job_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(job_id) = 'text' AND length(job_id) BETWEEN 1 AND 128
           AND job_id GLOB '[A-Za-z0-9]*'
           AND job_id NOT GLOB '*[^A-Za-z0-9._:/-]*'
           AND instr(job_id, char(0)) = 0),
    CHECK (typeof(job_kind) = 'text' AND length(job_kind) BETWEEN 1 AND 128
           AND job_kind GLOB '[a-z]*' AND job_kind NOT GLOB '*[^a-z0-9_.]*'
           AND job_kind NOT GLOB '*.' AND job_kind NOT GLOB '*.[^a-z]*'),
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
    CHECK (terminal_result_kind IS NULL OR (
           typeof(terminal_result_kind) = 'text'
           AND length(terminal_result_kind) BETWEEN 1 AND 128
           AND terminal_result_kind GLOB '[a-z]*'
           AND terminal_result_kind NOT GLOB '*[^a-z0-9_.]*'
           AND terminal_result_kind NOT GLOB '*.'
           AND terminal_result_kind NOT GLOB '*.[^a-z]*')),
    CHECK (typeof(supports_checkpoint_resume) = 'integer'
           AND supports_checkpoint_resume IN (0, 1)),
    CHECK (typeof(max_attempts) = 'integer' AND max_attempts BETWEEN 1 AND 256),

    FOREIGN KEY (job_id) REFERENCES omnivia_durable_jobs (job_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_job_attempts (
    workspace_id   TEXT    NOT NULL,
    job_id         TEXT    NOT NULL,
    attempt_number INTEGER NOT NULL,
    started_at_us  INTEGER NOT NULL,
    finished_at_us INTEGER,
    state          TEXT    NOT NULL,
    error_json     TEXT,

    PRIMARY KEY (workspace_id, job_id, attempt_number),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128),
    CHECK (typeof(job_id) = 'text' AND length(job_id) BETWEEN 1 AND 128),
    CHECK (typeof(attempt_number) = 'integer' AND attempt_number BETWEEN 1 AND 256),
    CHECK (typeof(started_at_us) = 'integer' AND started_at_us > 0),
    CHECK (finished_at_us IS NULL OR (
           typeof(finished_at_us) = 'integer' AND finished_at_us >= started_at_us)),
    CHECK (state IN ('running', 'succeeded', 'failed', 'cancelled')),
    CHECK (error_json IS NULL OR (
           typeof(error_json) = 'text'
           AND length(CAST(error_json AS BLOB)) BETWEEN 2 AND 65536)),
    CHECK (
        (state = 'running' AND finished_at_us IS NULL AND error_json IS NULL)
        OR (state = 'succeeded' AND finished_at_us IS NOT NULL AND error_json IS NULL)
        OR (state = 'failed' AND finished_at_us IS NOT NULL AND error_json IS NOT NULL)
        OR (state = 'cancelled' AND finished_at_us IS NOT NULL AND error_json IS NULL)
    ),

    FOREIGN KEY (workspace_id, job_id)
        REFERENCES omnivia_job_application_metadata (workspace_id, job_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_job_progress_events (
    workspace_id      TEXT    NOT NULL,
    job_id            TEXT    NOT NULL,
    progress_sequence INTEGER NOT NULL,
    attempt_number    INTEGER NOT NULL,
    occurred_at_us    INTEGER NOT NULL,
    unit              TEXT    NOT NULL,
    completed_units   INTEGER NOT NULL,
    total_units       INTEGER,
    message           TEXT,

    PRIMARY KEY (workspace_id, job_id, progress_sequence),

    CHECK (typeof(progress_sequence) = 'integer' AND progress_sequence >= 0),
    CHECK (typeof(attempt_number) = 'integer' AND attempt_number BETWEEN 1 AND 256),
    CHECK (typeof(occurred_at_us) = 'integer' AND occurred_at_us > 0),
    CHECK (typeof(unit) = 'text' AND length(unit) BETWEEN 1 AND 128
           AND unit GLOB '[a-z]*' AND unit NOT GLOB '*[^a-z0-9_.]*'
           AND unit NOT GLOB '*.' AND unit NOT GLOB '*.[^a-z]*'),
    CHECK (typeof(completed_units) = 'integer' AND completed_units >= 0),
    CHECK (total_units IS NULL OR (typeof(total_units) = 'integer' AND total_units >= 0)),
    CHECK (total_units IS NULL OR completed_units <= total_units),
    CHECK (message IS NULL OR (typeof(message) = 'text' AND length(message) <= 2048)),

    FOREIGN KEY (workspace_id, job_id, attempt_number)
        REFERENCES omnivia_job_attempts (workspace_id, job_id, attempt_number)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_job_checkpoints (
    workspace_id        TEXT    NOT NULL,
    job_id              TEXT    NOT NULL,
    checkpoint_sequence INTEGER NOT NULL,
    attempt_number      INTEGER NOT NULL,
    created_at_us       INTEGER NOT NULL,
    checkpoint_kind     TEXT    NOT NULL,
    checkpoint_json     TEXT    NOT NULL,
    checkpoint_digest   TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, job_id, checkpoint_sequence),

    CHECK (typeof(checkpoint_sequence) = 'integer' AND checkpoint_sequence >= 0),
    CHECK (typeof(attempt_number) = 'integer' AND attempt_number BETWEEN 1 AND 256),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (typeof(checkpoint_kind) = 'text' AND length(checkpoint_kind) BETWEEN 1 AND 128
           AND checkpoint_kind GLOB '[a-z]*'
           AND checkpoint_kind NOT GLOB '*[^a-z0-9_.]*'
           AND checkpoint_kind NOT GLOB '*.'
           AND checkpoint_kind NOT GLOB '*.[^a-z]*'),
    CHECK (typeof(checkpoint_json) = 'text'
           AND length(CAST(checkpoint_json AS BLOB)) BETWEEN 2 AND 1048576),
    CHECK (typeof(checkpoint_digest) = 'text' AND length(checkpoint_digest) = 71
           AND substr(checkpoint_digest, 1, 7) = 'sha256:'
           AND substr(checkpoint_digest, 8) NOT GLOB '*[^0-9a-f]*'),

    FOREIGN KEY (workspace_id, job_id, attempt_number)
        REFERENCES omnivia_job_attempts (workspace_id, job_id, attempt_number)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_job_events (
    workspace_id   TEXT    NOT NULL,
    job_id         TEXT    NOT NULL,
    sequence       INTEGER NOT NULL,
    occurred_at_us INTEGER NOT NULL,
    state          TEXT    NOT NULL,
    message        TEXT,
    details_json   TEXT,

    PRIMARY KEY (workspace_id, job_id, sequence),

    CHECK (typeof(sequence) = 'integer' AND sequence >= 0),
    CHECK (typeof(occurred_at_us) = 'integer' AND occurred_at_us > 0),
    CHECK (state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    CHECK (message IS NULL OR (typeof(message) = 'text' AND length(message) <= 2048)),
    CHECK (details_json IS NULL OR (
           typeof(details_json) = 'text'
           AND length(CAST(details_json AS BLOB)) BETWEEN 2 AND 65536)),

    FOREIGN KEY (workspace_id, job_id)
        REFERENCES omnivia_job_application_metadata (workspace_id, job_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_job_terminal_results (
    workspace_id        TEXT    NOT NULL,
    job_id              TEXT    NOT NULL,
    terminal_state      TEXT    NOT NULL,
    finished_at_us      INTEGER NOT NULL,
    result_kind         TEXT,
    result_json         TEXT,
    error_json          TEXT,
    cancellation_reason TEXT,

    PRIMARY KEY (workspace_id, job_id),

    CHECK (terminal_state IN ('succeeded', 'failed', 'cancelled')),
    CHECK (typeof(finished_at_us) = 'integer' AND finished_at_us > 0),
    CHECK (result_kind IS NULL OR (typeof(result_kind) = 'text'
           AND length(result_kind) BETWEEN 1 AND 128
           AND result_kind GLOB '[a-z]*' AND result_kind NOT GLOB '*[^a-z0-9_.]*'
           AND result_kind NOT GLOB '*.' AND result_kind NOT GLOB '*.[^a-z]*')),
    CHECK (result_json IS NULL OR (typeof(result_json) = 'text'
           AND length(CAST(result_json AS BLOB)) BETWEEN 2 AND 1048576)),
    CHECK (error_json IS NULL OR (typeof(error_json) = 'text'
           AND length(CAST(error_json AS BLOB)) BETWEEN 2 AND 65536)),
    CHECK (cancellation_reason IS NULL OR (typeof(cancellation_reason) = 'text'
           AND length(cancellation_reason) BETWEEN 1 AND 128
           AND cancellation_reason GLOB '[a-z]*'
           AND cancellation_reason NOT GLOB '*[^a-z0-9_.]*'
           AND cancellation_reason NOT GLOB '*.'
           AND cancellation_reason NOT GLOB '*.[^a-z]*')),
    CHECK (
        (terminal_state = 'succeeded' AND result_kind IS NOT NULL
         AND result_json IS NOT NULL AND error_json IS NULL
         AND cancellation_reason IS NULL)
        OR (terminal_state = 'failed' AND result_kind IS NULL
            AND result_json IS NULL AND error_json IS NOT NULL
            AND cancellation_reason IS NULL)
        OR (terminal_state = 'cancelled' AND result_kind IS NULL
            AND result_json IS NULL AND error_json IS NULL
            AND cancellation_reason IS NOT NULL)
    ),

    FOREIGN KEY (workspace_id, job_id)
        REFERENCES omnivia_job_application_metadata (workspace_id, job_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_job_application_metadata_audit
    ON omnivia_job_application_metadata (workspace_id, audit_ref);
CREATE INDEX IF NOT EXISTS omnivia_idx_job_attempts_latest
    ON omnivia_job_attempts (workspace_id, job_id, attempt_number DESC);
CREATE INDEX IF NOT EXISTS omnivia_idx_job_attempts_state
    ON omnivia_job_attempts (workspace_id, state, job_id);
CREATE INDEX IF NOT EXISTS omnivia_idx_job_progress_latest
    ON omnivia_job_progress_events (workspace_id, job_id, progress_sequence DESC);
CREATE INDEX IF NOT EXISTS omnivia_idx_job_progress_attempt
    ON omnivia_job_progress_events (workspace_id, job_id, attempt_number, progress_sequence);
CREATE INDEX IF NOT EXISTS omnivia_idx_job_checkpoints_latest
    ON omnivia_job_checkpoints (workspace_id, job_id, checkpoint_sequence DESC);
CREATE INDEX IF NOT EXISTS omnivia_idx_job_events_page
    ON omnivia_job_events (workspace_id, job_id, sequence);
CREATE INDEX IF NOT EXISTS omnivia_idx_job_events_state
    ON omnivia_job_events (workspace_id, state, occurred_at_us);
CREATE INDEX IF NOT EXISTS omnivia_idx_job_terminal_results_state
    ON omnivia_job_terminal_results (workspace_id, terminal_state, finished_at_us);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_durable_jobs_application_metadata_update
BEFORE UPDATE ON omnivia_durable_jobs
WHEN NEW.job_type IS NOT OLD.job_type
 AND EXISTS (
    SELECT 1 FROM omnivia_job_application_metadata
    WHERE job_id = OLD.job_id
 )
BEGIN
    SELECT RAISE(ABORT, 'omnivia: durable job kind is immutable after application metadata is recorded');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_job_application_metadata_insert
BEFORE INSERT ON omnivia_job_application_metadata
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_job_application_metadata')
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
              AND l.lifecycle IN ('acquiring', 'held', 'draining')
       )
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
       );
    SELECT RAISE(ABORT, 'omnivia: job application metadata kind does not match durable job')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_durable_jobs
        WHERE job_id = NEW.job_id AND job_type = NEW.job_kind
    );
    SELECT RAISE(ABORT, 'omnivia: job application metadata audit workspace mismatch')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events
        WHERE audit_ref = NEW.audit_ref AND workspace_id = NEW.workspace_id
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_job_application_metadata_update
BEFORE UPDATE ON omnivia_job_application_metadata
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_job_application_metadata is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_job_application_metadata_delete
BEFORE DELETE ON omnivia_job_application_metadata
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_job_application_metadata is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_job_attempts_insert
BEFORE INSERT ON omnivia_job_attempts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_job_attempts')
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
              AND l.lifecycle IN ('acquiring', 'held', 'draining')
       )
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
       );
    SELECT RAISE(ABORT, 'omnivia: attempt requires a claimed durable job')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_durable_jobs
        WHERE job_id = NEW.job_id AND state = 'claimed'
    );
    SELECT RAISE(ABORT, 'omnivia: a new attempt must start in running state')
    WHERE NEW.state <> 'running' OR NEW.finished_at_us IS NOT NULL
       OR NEW.error_json IS NOT NULL;
    SELECT RAISE(ABORT, 'omnivia: attempt number must be contiguous and within job budget')
    WHERE NEW.attempt_number IS NOT (
            SELECT COALESCE(MAX(attempt_number), 0) + 1
            FROM omnivia_job_attempts
            WHERE workspace_id = NEW.workspace_id AND job_id = NEW.job_id
          )
       OR NEW.attempt_number > (
            SELECT max_attempts FROM omnivia_job_application_metadata
            WHERE workspace_id = NEW.workspace_id AND job_id = NEW.job_id
          );
    SELECT RAISE(ABORT, 'omnivia: only a failed or cancelled attempt may be retried')
    WHERE NEW.attempt_number > 1
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_job_attempts
        WHERE workspace_id = NEW.workspace_id AND job_id = NEW.job_id
          AND attempt_number = NEW.attempt_number - 1
          AND state IN ('failed', 'cancelled') AND finished_at_us IS NOT NULL
      );
    SELECT RAISE(ABORT, 'omnivia: a retry cannot start before its predecessor finished')
    WHERE NEW.attempt_number > 1
      AND NEW.started_at_us < (
        SELECT finished_at_us FROM omnivia_job_attempts
        WHERE workspace_id = NEW.workspace_id AND job_id = NEW.job_id
          AND attempt_number = NEW.attempt_number - 1
      );
    SELECT RAISE(ABORT, 'omnivia: terminal job cannot start another attempt')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_job_terminal_results
        WHERE workspace_id = NEW.workspace_id AND job_id = NEW.job_id
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_job_attempts_update
BEFORE UPDATE ON omnivia_job_attempts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_job_attempts')
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
              AND l.lifecycle IN ('acquiring', 'held', 'draining')
       )
       OR NEW.workspace_id IS NOT OLD.workspace_id
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
       );
    SELECT RAISE(ABORT, 'omnivia: attempt identity and start are immutable')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id
       OR NEW.job_id IS NOT OLD.job_id
       OR NEW.attempt_number IS NOT OLD.attempt_number
       OR NEW.started_at_us IS NOT OLD.started_at_us;
    SELECT RAISE(ABORT, 'omnivia: attempt permits exactly one terminalization')
    WHERE OLD.state <> 'running' OR OLD.finished_at_us IS NOT NULL
       OR NEW.state NOT IN ('succeeded', 'failed', 'cancelled')
       OR NEW.finished_at_us IS NULL;
    SELECT RAISE(ABORT, 'omnivia: attempt terminal state does not match durable job')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_durable_jobs j
        WHERE j.job_id = NEW.job_id
          AND (
            (NEW.state = 'succeeded' AND j.state = 'succeeded')
            OR (NEW.state = 'failed' AND j.state IN ('queued', 'failed'))
            OR (NEW.state = 'cancelled' AND j.state = 'cancelled')
          )
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_job_attempts_delete
BEFORE DELETE ON omnivia_job_attempts
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_job_attempts is append-preserved; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_job_progress_events_insert
BEFORE INSERT ON omnivia_job_progress_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_job_progress_events')
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
              AND l.lifecycle IN ('acquiring', 'held', 'draining')
       )
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
       );
    SELECT RAISE(ABORT, 'omnivia: progress requires the running attempt')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_job_attempts a
        JOIN omnivia_durable_jobs j ON j.job_id = a.job_id
        WHERE a.workspace_id = NEW.workspace_id AND a.job_id = NEW.job_id
          AND a.attempt_number = NEW.attempt_number
          AND a.state = 'running' AND j.state = 'claimed'
          AND NEW.occurred_at_us >= a.started_at_us
    );
    SELECT RAISE(ABORT, 'omnivia: progress sequence must be contiguous')
    WHERE NEW.progress_sequence IS NOT (
        SELECT COALESCE(MAX(progress_sequence), -1) + 1
        FROM omnivia_job_progress_events
        WHERE workspace_id = NEW.workspace_id AND job_id = NEW.job_id
    );
    SELECT RAISE(ABORT, 'omnivia: progress regressed or changed unit/total')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_job_progress_events p
        WHERE p.workspace_id = NEW.workspace_id AND p.job_id = NEW.job_id
          AND p.attempt_number = NEW.attempt_number
          AND p.progress_sequence = (
            SELECT MAX(progress_sequence) FROM omnivia_job_progress_events
            WHERE workspace_id = NEW.workspace_id AND job_id = NEW.job_id
              AND attempt_number = NEW.attempt_number
          )
          AND (NEW.unit <> p.unit
               OR NEW.completed_units < p.completed_units
               OR NEW.occurred_at_us < p.occurred_at_us
               OR (p.total_units IS NOT NULL AND NEW.total_units IS NOT p.total_units))
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_job_progress_events_update
BEFORE UPDATE ON omnivia_job_progress_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_job_progress_events is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_job_progress_events_delete
BEFORE DELETE ON omnivia_job_progress_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_job_progress_events is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_job_checkpoints_insert
BEFORE INSERT ON omnivia_job_checkpoints
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_job_checkpoints')
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
              AND l.lifecycle IN ('acquiring', 'held', 'draining')
       )
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
       );
    SELECT RAISE(ABORT, 'omnivia: checkpoint requires a resumable running attempt')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_job_attempts a
        JOIN omnivia_job_application_metadata m
          ON m.workspace_id = a.workspace_id AND m.job_id = a.job_id
        JOIN omnivia_durable_jobs j ON j.job_id = a.job_id
        WHERE a.workspace_id = NEW.workspace_id AND a.job_id = NEW.job_id
          AND a.attempt_number = NEW.attempt_number
          AND a.state = 'running' AND j.state = 'claimed'
          AND m.supports_checkpoint_resume = 1
          AND NEW.created_at_us >= a.started_at_us
    );
    SELECT RAISE(ABORT, 'omnivia: checkpoint sequence must be contiguous')
    WHERE NEW.checkpoint_sequence IS NOT (
        SELECT COALESCE(MAX(checkpoint_sequence), -1) + 1
        FROM omnivia_job_checkpoints
        WHERE workspace_id = NEW.workspace_id AND job_id = NEW.job_id
    );
    SELECT RAISE(ABORT, 'omnivia: checkpoint time must not regress')
    WHERE NEW.checkpoint_sequence > 0
      AND NEW.created_at_us < (
        SELECT created_at_us FROM omnivia_job_checkpoints
        WHERE workspace_id = NEW.workspace_id AND job_id = NEW.job_id
          AND checkpoint_sequence = NEW.checkpoint_sequence - 1
      );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_job_checkpoints_update
BEFORE UPDATE ON omnivia_job_checkpoints
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_job_checkpoints is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_job_checkpoints_delete
BEFORE DELETE ON omnivia_job_checkpoints
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_job_checkpoints is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_job_events_insert
BEFORE INSERT ON omnivia_job_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_job_events')
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
              AND l.lifecycle IN ('acquiring', 'held', 'draining')
       )
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
       );
    SELECT RAISE(ABORT, 'omnivia: job event sequence must be contiguous')
    WHERE NEW.sequence IS NOT (
        SELECT COALESCE(MAX(sequence), -1) + 1 FROM omnivia_job_events
        WHERE workspace_id = NEW.workspace_id AND job_id = NEW.job_id
    );
    SELECT RAISE(ABORT, 'omnivia: job event time must not regress')
    WHERE NEW.sequence > 0
      AND NEW.occurred_at_us < (
        SELECT occurred_at_us FROM omnivia_job_events
        WHERE workspace_id = NEW.workspace_id AND job_id = NEW.job_id
          AND sequence = NEW.sequence - 1
      );
    SELECT RAISE(ABORT, 'omnivia: job event state does not match scheduler state')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_durable_jobs
        WHERE job_id = NEW.job_id
          AND state = CASE NEW.state WHEN 'running' THEN 'claimed' ELSE NEW.state END
    );
    SELECT RAISE(ABORT, 'omnivia: terminal job event is final')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_job_terminal_results
        WHERE workspace_id = NEW.workspace_id AND job_id = NEW.job_id
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_job_events_update
BEFORE UPDATE ON omnivia_job_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_job_events is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_job_events_delete
BEFORE DELETE ON omnivia_job_events
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_job_events is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_job_terminal_results_insert
BEFORE INSERT ON omnivia_job_terminal_results
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_job_terminal_results')
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
              AND l.lifecycle IN ('acquiring', 'held', 'draining')
       )
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
       );
    SELECT RAISE(ABORT, 'omnivia: terminal result does not match scheduler state')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_durable_jobs
        WHERE job_id = NEW.job_id AND state = NEW.terminal_state
    );
    SELECT RAISE(ABORT, 'omnivia: terminal result requires the matching final event')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_job_events
        WHERE workspace_id = NEW.workspace_id AND job_id = NEW.job_id
          AND state = NEW.terminal_state
          AND occurred_at_us = NEW.finished_at_us
          AND sequence = (
            SELECT MAX(sequence) FROM omnivia_job_events
            WHERE workspace_id = NEW.workspace_id AND job_id = NEW.job_id
          )
    );
    SELECT RAISE(ABORT, 'omnivia: terminal result attempt history is inconsistent')
    WHERE (NEW.terminal_state IN ('succeeded', 'failed') AND NOT EXISTS (
            SELECT 1 FROM omnivia_job_attempts
            WHERE workspace_id = NEW.workspace_id AND job_id = NEW.job_id
              AND state = NEW.terminal_state
              AND finished_at_us = NEW.finished_at_us
              AND attempt_number = (
                SELECT MAX(attempt_number) FROM omnivia_job_attempts
                WHERE workspace_id = NEW.workspace_id AND job_id = NEW.job_id
              )
          ))
       OR (NEW.terminal_state = 'cancelled' AND EXISTS (
            SELECT 1 FROM omnivia_job_attempts
            WHERE workspace_id = NEW.workspace_id AND job_id = NEW.job_id
              AND attempt_number = (
                SELECT MAX(attempt_number) FROM omnivia_job_attempts
                WHERE workspace_id = NEW.workspace_id AND job_id = NEW.job_id
              )
              AND (state <> 'cancelled' OR finished_at_us <> NEW.finished_at_us)
          ));
    SELECT RAISE(ABORT, 'omnivia: terminal success result kind does not match job metadata')
    WHERE NEW.terminal_state = 'succeeded'
      AND EXISTS (
        SELECT 1 FROM omnivia_job_application_metadata
        WHERE workspace_id = NEW.workspace_id AND job_id = NEW.job_id
          AND terminal_result_kind IS NOT NULL
          AND terminal_result_kind <> NEW.result_kind
      );
    SELECT RAISE(ABORT, 'omnivia: terminal failure must repeat the final attempt error')
    WHERE NEW.terminal_state = 'failed'
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_job_attempts
        WHERE workspace_id = NEW.workspace_id AND job_id = NEW.job_id
          AND attempt_number = (
            SELECT MAX(attempt_number) FROM omnivia_job_attempts
            WHERE workspace_id = NEW.workspace_id AND job_id = NEW.job_id
          )
          AND error_json = NEW.error_json
      );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_job_terminal_results_update
BEFORE UPDATE ON omnivia_job_terminal_results
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_job_terminal_results is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_job_terminal_results_delete
BEFORE DELETE ON omnivia_job_terminal_results
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_job_terminal_results is append-only; DELETE is never permitted');
END;
