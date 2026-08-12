-- V06-5 S3 immutable import, job-control and retryable terminal-history bridges.
--
-- The scheduler row remains mutable.  These three relations preserve the accepted
-- application input and every control/terminal fact needed to reconstruct a job.
-- Migration 0010 remains byte-immutable; only its three INSERT guards whose old
-- definition made the first terminal result permanently final are replaced below.

CREATE TABLE omnivia_application_import_claims (
    workspace_id        TEXT    NOT NULL,
    job_id              TEXT    NOT NULL,
    audit_ref           TEXT    NOT NULL,
    staged_source_ref   TEXT    NOT NULL,
    source_kind         TEXT    NOT NULL,
    content_checksum    TEXT    NOT NULL,
    content_length_bytes INTEGER NOT NULL,
    media_type          TEXT    NOT NULL,
    source_version      TEXT,
    input_json          TEXT    NOT NULL,
    input_digest        TEXT    NOT NULL,
    input_byte_length   INTEGER NOT NULL,
    settled_at_us       INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, job_id),

    CHECK (length(workspace_id) BETWEEN 1 AND 128),
    CHECK (length(job_id) BETWEEN 1 AND 128),
    CHECK (length(audit_ref) BETWEEN 1 AND 128),
    CHECK (length(staged_source_ref) BETWEEN 1 AND 512),
    CHECK (length(source_kind) BETWEEN 1 AND 128),
    CHECK (length(content_checksum) = 71
           AND substr(content_checksum, 1, 7) = 'sha256:'
           AND substr(content_checksum, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(content_length_bytes) = 'integer' AND content_length_bytes >= 0),
    CHECK (length(media_type) BETWEEN 3 AND 255),
    CHECK (source_version IS NULL OR length(source_version) BETWEEN 1 AND 128),
    CHECK (typeof(input_json) = 'text'
           AND length(CAST(input_json AS BLOB)) BETWEEN 2 AND 1048576),
    CHECK (typeof(input_byte_length) = 'integer'
           AND input_byte_length = length(CAST(input_json AS BLOB))),
    CHECK (length(input_digest) = 71
           AND substr(input_digest, 1, 7) = 'sha256:'
           AND substr(input_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(settled_at_us) = 'integer' AND settled_at_us > 0),

    FOREIGN KEY (workspace_id, job_id)
        REFERENCES omnivia_job_application_metadata (workspace_id, job_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id),
    FOREIGN KEY (staged_source_ref)
        REFERENCES omnivia_staged_sources (staged_source_ref)
) WITHOUT ROWID;

CREATE TABLE omnivia_job_terminal_observations (
    workspace_id                TEXT    NOT NULL,
    job_id                      TEXT    NOT NULL,
    terminal_observation_number INTEGER NOT NULL,
    attempt_number              INTEGER,
    terminal_state              TEXT    NOT NULL,
    finished_at_us              INTEGER NOT NULL,
    result_kind                 TEXT,
    result_json                 TEXT,
    error_json                  TEXT,
    cancellation_reason         TEXT,
    provenance_kind             TEXT    NOT NULL,
    fencing_generation          INTEGER,

    PRIMARY KEY (workspace_id, job_id, terminal_observation_number),

    CHECK (typeof(terminal_observation_number) = 'integer'
           AND terminal_observation_number BETWEEN 1 AND 256),
    CHECK (attempt_number IS NULL OR
           (typeof(attempt_number) = 'integer' AND attempt_number BETWEEN 1 AND 256)),
    CHECK (terminal_state IN ('succeeded', 'failed', 'cancelled')),
    CHECK (typeof(finished_at_us) = 'integer' AND finished_at_us > 0),
    CHECK (result_kind IS NULL OR (typeof(result_kind) = 'text'
           AND length(result_kind) BETWEEN 1 AND 128
           AND result_kind GLOB '[a-z]*'
           AND result_kind NOT GLOB '*[^a-z0-9_.]*'
           AND result_kind NOT GLOB '*.'
           AND result_kind NOT GLOB '*.[^a-z]*')),
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
    CHECK (
        (provenance_kind = 'legacy_unrecorded' AND fencing_generation IS NULL)
        OR (provenance_kind = 'service_committed'
            AND typeof(fencing_generation) = 'integer' AND fencing_generation > 0)
    ),

    FOREIGN KEY (workspace_id, job_id)
        REFERENCES omnivia_job_application_metadata (workspace_id, job_id),
    FOREIGN KEY (workspace_id, job_id, attempt_number)
        REFERENCES omnivia_job_attempts (workspace_id, job_id, attempt_number)
) WITHOUT ROWID;

CREATE TABLE omnivia_application_job_controls (
    workspace_id                       TEXT    NOT NULL,
    control_id                         TEXT    NOT NULL,
    job_id                             TEXT    NOT NULL,
    control_kind                       TEXT    NOT NULL,
    operation                          TEXT    NOT NULL,
    disposition                        TEXT    NOT NULL,
    source_state                       TEXT    NOT NULL,
    resulting_state                    TEXT    NOT NULL,
    source_terminal_observation_number INTEGER,
    audit_ref                          TEXT,
    fencing_generation                 INTEGER NOT NULL,
    control_json                       TEXT    NOT NULL,
    control_digest                     TEXT    NOT NULL,
    control_byte_length                INTEGER NOT NULL,
    settled_at_us                      INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, control_id),

    CHECK (length(workspace_id) BETWEEN 1 AND 128),
    CHECK (length(control_id) BETWEEN 1 AND 128),
    CHECK (length(job_id) BETWEEN 1 AND 128),
    CHECK (control_kind IN ('user', 'system')),
    CHECK (operation IN ('job.cancel', 'job.retry', 'system.recovery')),
    CHECK (length(disposition) BETWEEN 1 AND 128),
    CHECK (source_state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    CHECK (resulting_state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    CHECK (source_terminal_observation_number IS NULL OR
           (typeof(source_terminal_observation_number) = 'integer'
            AND source_terminal_observation_number BETWEEN 1 AND 256)),
    CHECK (audit_ref IS NULL OR length(audit_ref) BETWEEN 1 AND 128),
    CHECK (typeof(fencing_generation) = 'integer' AND fencing_generation > 0),
    CHECK (typeof(control_json) = 'text'
           AND length(CAST(control_json AS BLOB)) BETWEEN 2 AND 65536),
    CHECK (typeof(control_byte_length) = 'integer'
           AND control_byte_length = length(CAST(control_json AS BLOB))),
    CHECK (length(control_digest) = 71
           AND substr(control_digest, 1, 7) = 'sha256:'
           AND substr(control_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(settled_at_us) = 'integer' AND settled_at_us > 0),
    CHECK (
        (control_kind = 'system' AND operation = 'system.recovery'
         AND disposition = 'recovery_requeued' AND source_state = 'running'
         AND resulting_state = 'queued'
         AND source_terminal_observation_number IS NULL AND audit_ref IS NULL)
        OR (control_kind = 'user' AND operation IN ('job.cancel', 'job.retry')
            AND audit_ref IS NOT NULL)
    ),
    CHECK (
        operation <> 'job.retry'
        OR (disposition IN ('retry_scheduled', 'resume_scheduled')
            AND source_terminal_observation_number IS NOT NULL
            AND source_state IN ('failed', 'cancelled')
            AND resulting_state = 'queued')
        OR (disposition = 'not_retryable'
            AND source_terminal_observation_number IS NULL
            AND source_state = resulting_state)
    ),
    CHECK (
        operation <> 'job.cancel'
        OR (disposition = 'cancellation_requested'
            AND source_terminal_observation_number IS NULL
            AND source_state IN ('queued', 'running')
            AND source_state = resulting_state)
        OR (disposition IN ('cancelled', 'not_cancellable')
            AND source_terminal_observation_number IS NULL
            AND source_state = resulting_state)
    ),

    FOREIGN KEY (workspace_id, job_id)
        REFERENCES omnivia_job_application_metadata (workspace_id, job_id),
    FOREIGN KEY (workspace_id, job_id, source_terminal_observation_number)
        REFERENCES omnivia_job_terminal_observations
            (workspace_id, job_id, terminal_observation_number),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE INDEX omnivia_idx_application_import_claims_audit
    ON omnivia_application_import_claims (workspace_id, audit_ref);
CREATE INDEX omnivia_idx_application_job_controls_job_time
    ON omnivia_application_job_controls (workspace_id, job_id, settled_at_us);
CREATE UNIQUE INDEX omnivia_idx_application_job_controls_recovery_once
    ON omnivia_application_job_controls
       (workspace_id, job_id, source_terminal_observation_number)
    WHERE operation = 'job.retry'
      AND disposition IN ('retry_scheduled', 'resume_scheduled');
CREATE UNIQUE INDEX omnivia_idx_application_job_controls_system_settlement
    ON omnivia_application_job_controls (workspace_id, job_id, settled_at_us)
    WHERE control_kind = 'system';
CREATE INDEX omnivia_idx_job_terminal_observations_latest
    ON omnivia_job_terminal_observations
       (workspace_id, job_id, terminal_observation_number DESC);
CREATE UNIQUE INDEX omnivia_idx_job_terminal_observations_attempt
    ON omnivia_job_terminal_observations (workspace_id, job_id, attempt_number)
    WHERE attempt_number IS NOT NULL;

-- Copy each migration-0010 terminal fact exactly once.  Its schema did not record
-- the committing fence, so the provenance is explicitly unknown rather than copied
-- from the mutable scheduler row.
INSERT INTO omnivia_job_terminal_observations
    (workspace_id, job_id, terminal_observation_number, attempt_number,
     terminal_state, finished_at_us, result_kind, result_json, error_json,
     cancellation_reason, provenance_kind, fencing_generation)
SELECT r.workspace_id, r.job_id, 1,
       CASE WHEN EXISTS (
           SELECT 1 FROM omnivia_job_attempts a
           WHERE a.workspace_id = r.workspace_id AND a.job_id = r.job_id
             AND a.attempt_number = (
                 SELECT MAX(a2.attempt_number) FROM omnivia_job_attempts a2
                 WHERE a2.workspace_id = r.workspace_id AND a2.job_id = r.job_id
             )
             AND a.state = r.terminal_state
             AND a.finished_at_us = r.finished_at_us
       ) THEN (
           SELECT MAX(a3.attempt_number) FROM omnivia_job_attempts a3
           WHERE a3.workspace_id = r.workspace_id AND a3.job_id = r.job_id
       ) ELSE NULL END,
       r.terminal_state, r.finished_at_us, r.result_kind, r.result_json,
       r.error_json, r.cancellation_reason, 'legacy_unrecorded', NULL
FROM omnivia_job_terminal_results r;

CREATE TABLE omnivia_migration_0015_backfill_gate (
    singleton INTEGER NOT NULL PRIMARY KEY CHECK (singleton = 1)
) WITHOUT ROWID;

CREATE TRIGGER omnivia_migration_0015_backfill_gate_insert
BEFORE INSERT ON omnivia_migration_0015_backfill_gate
BEGIN
    SELECT RAISE(ABORT, 'omnivia: migration 0015 terminal backfill count differs')
    WHERE (SELECT COUNT(*) FROM omnivia_job_terminal_observations)
       <> (SELECT COUNT(*) FROM omnivia_job_terminal_results);
    SELECT RAISE(ABORT, 'omnivia: migration 0015 terminal backfill payload differs')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_job_terminal_results r
        WHERE NOT EXISTS (
            SELECT 1 FROM omnivia_job_terminal_observations o
            WHERE o.workspace_id = r.workspace_id AND o.job_id = r.job_id
              AND o.terminal_observation_number = 1
              AND o.terminal_state = r.terminal_state
              AND o.finished_at_us = r.finished_at_us
              AND o.result_kind IS r.result_kind
              AND o.result_json IS r.result_json
              AND o.error_json IS r.error_json
              AND o.cancellation_reason IS r.cancellation_reason
              AND o.provenance_kind = 'legacy_unrecorded'
              AND o.fencing_generation IS NULL
        )
    );
END;

INSERT INTO omnivia_migration_0015_backfill_gate (singleton) VALUES (1);
DROP TRIGGER omnivia_migration_0015_backfill_gate_insert;
DROP TABLE omnivia_migration_0015_backfill_gate;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_application_import_claims_insert
BEFORE INSERT ON omnivia_application_import_claims
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_application_import_claims')
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
    SELECT RAISE(ABORT, 'omnivia: import claim contradicts job metadata or audit')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_job_application_metadata m
        JOIN omnivia_application_audit_events a
          ON a.audit_ref = m.audit_ref AND a.workspace_id = m.workspace_id
        WHERE m.workspace_id = NEW.workspace_id AND m.job_id = NEW.job_id
          AND m.job_kind = 'ingestion.import'
          AND m.originating_operation = 'import.start'
          AND m.audit_ref = NEW.audit_ref
          AND a.operation = 'import.start'
          AND a.recorded_at_us = NEW.settled_at_us
    );
    SELECT RAISE(ABORT, 'omnivia: import claim does not name the exact verified staged source')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_staged_sources s
        WHERE s.staged_source_ref = NEW.staged_source_ref
          AND s.workspace_id = NEW.workspace_id
          AND s.source_kind = NEW.source_kind
          AND s.declared_checksum = NEW.content_checksum
          AND s.content_length_bytes = NEW.content_length_bytes
          AND s.media_type = NEW.media_type
          AND s.source_version IS NEW.source_version
          AND s.staging_outcome = 'verified'
          AND s.blob_workspace_id = NEW.workspace_id
          AND s.blob_content_digest = NEW.content_checksum
    );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_application_import_claims_update
BEFORE UPDATE ON omnivia_application_import_claims
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_application_import_claims is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_application_import_claims_delete
BEFORE DELETE ON omnivia_application_import_claims
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_application_import_claims is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_application_job_controls_insert
BEFORE INSERT ON omnivia_application_job_controls
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_application_job_controls')
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
              AND NEW.fencing_generation = g.fencing_generation
       )
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
       );
    SELECT RAISE(ABORT, 'omnivia: job control resulting state disagrees with scheduler')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_durable_jobs j
        WHERE j.job_id = NEW.job_id
          AND j.state = CASE NEW.resulting_state WHEN 'running' THEN 'claimed'
                        ELSE NEW.resulting_state END
    );
    SELECT RAISE(ABORT, 'omnivia: user job control contradicts its operation audit')
    WHERE NEW.control_kind = 'user'
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_application_audit_events a
        WHERE a.audit_ref = NEW.audit_ref AND a.workspace_id = NEW.workspace_id
          AND a.operation = NEW.operation AND a.recorded_at_us = NEW.settled_at_us
      );
    SELECT RAISE(ABORT, 'omnivia: accepted recovery does not bind the latest terminal observation')
    WHERE NEW.operation = 'job.retry'
      AND NEW.disposition IN ('retry_scheduled', 'resume_scheduled')
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_job_terminal_observations o
        WHERE o.workspace_id = NEW.workspace_id AND o.job_id = NEW.job_id
          AND o.terminal_observation_number = NEW.source_terminal_observation_number
          AND o.terminal_observation_number = (
              SELECT MAX(o2.terminal_observation_number)
              FROM omnivia_job_terminal_observations o2
              WHERE o2.workspace_id = NEW.workspace_id AND o2.job_id = NEW.job_id
          )
          AND o.terminal_state = NEW.source_state
          AND ((NEW.disposition = 'retry_scheduled' AND o.terminal_state = 'failed'
                AND json_extract(o.error_json, '$.retry_class') = 'retryable')
               OR (NEW.disposition = 'resume_scheduled' AND o.terminal_state = 'cancelled'
                   AND EXISTS (
                       SELECT 1 FROM omnivia_job_application_metadata m
                       WHERE m.workspace_id = NEW.workspace_id AND m.job_id = NEW.job_id
                         AND m.supports_checkpoint_resume = 1
                   )
                   AND EXISTS (
                       SELECT 1 FROM omnivia_job_checkpoints c
                       JOIN omnivia_job_attempts a
                         ON a.workspace_id = c.workspace_id AND a.job_id = c.job_id
                        AND a.attempt_number = c.attempt_number
                       WHERE c.workspace_id = NEW.workspace_id AND c.job_id = NEW.job_id
                         AND a.finished_at_us IS NOT NULL
                   )))
      );
    SELECT RAISE(ABORT, 'omnivia: system recovery lacks the exact interrupted attempt')
    WHERE NEW.control_kind = 'system'
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_job_attempts a
        WHERE a.workspace_id = NEW.workspace_id AND a.job_id = NEW.job_id
          AND a.attempt_number = (
              SELECT MAX(a2.attempt_number) FROM omnivia_job_attempts a2
              WHERE a2.workspace_id = NEW.workspace_id AND a2.job_id = NEW.job_id
          )
          AND a.state = 'failed' AND a.finished_at_us = NEW.settled_at_us
          AND json_extract(a.error_json, '$.code') = 'internal_recoverable'
          AND json_extract(a.error_json, '$.retry_class') = 'retryable'
      );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_application_job_controls_update
BEFORE UPDATE ON omnivia_application_job_controls
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_application_job_controls is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_application_job_controls_delete
BEFORE DELETE ON omnivia_application_job_controls
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_application_job_controls is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_job_terminal_observations_insert
BEFORE INSERT ON omnivia_job_terminal_observations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_job_terminal_observations')
    WHERE omnivia_service_writer() IS NOT 1
       OR NEW.provenance_kind <> 'service_committed'
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
              AND NEW.fencing_generation = g.fencing_generation
       )
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1
       );
    SELECT RAISE(ABORT, 'omnivia: terminal observation number must be contiguous')
    WHERE NEW.terminal_observation_number IS NOT (
        SELECT COALESCE(MAX(terminal_observation_number), 0) + 1
        FROM omnivia_job_terminal_observations
        WHERE workspace_id = NEW.workspace_id AND job_id = NEW.job_id
    );
    SELECT RAISE(ABORT, 'omnivia: terminal observation time must not regress')
    WHERE NEW.terminal_observation_number > 1
      AND NEW.finished_at_us < (
        SELECT finished_at_us FROM omnivia_job_terminal_observations
        WHERE workspace_id = NEW.workspace_id AND job_id = NEW.job_id
          AND terminal_observation_number = NEW.terminal_observation_number - 1
      );
    SELECT RAISE(ABORT, 'omnivia: terminal observation does not match scheduler and final event')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_durable_jobs j
        JOIN omnivia_job_events e ON e.job_id = j.job_id
        WHERE j.job_id = NEW.job_id AND j.state = NEW.terminal_state
          AND e.workspace_id = NEW.workspace_id AND e.state = NEW.terminal_state
          AND e.occurred_at_us = NEW.finished_at_us
          AND e.sequence = (
              SELECT MAX(e2.sequence) FROM omnivia_job_events e2
              WHERE e2.workspace_id = NEW.workspace_id AND e2.job_id = NEW.job_id
          )
    );
    SELECT RAISE(ABORT, 'omnivia: terminal observation attempt history is inconsistent')
    WHERE (NEW.terminal_state IN ('succeeded', 'failed') AND NOT EXISTS (
            SELECT 1 FROM omnivia_job_attempts a
            WHERE a.workspace_id = NEW.workspace_id AND a.job_id = NEW.job_id
              AND a.attempt_number = NEW.attempt_number
              AND a.attempt_number = (
                  SELECT MAX(a2.attempt_number) FROM omnivia_job_attempts a2
                  WHERE a2.workspace_id = NEW.workspace_id AND a2.job_id = NEW.job_id
              )
              AND a.state = NEW.terminal_state
              AND a.finished_at_us = NEW.finished_at_us
          ))
       OR (NEW.terminal_state = 'cancelled' AND (
            (NOT EXISTS (
                SELECT 1 FROM omnivia_job_attempts a3
                WHERE a3.workspace_id = NEW.workspace_id AND a3.job_id = NEW.job_id
             ) AND NEW.attempt_number IS NOT NULL)
            OR (EXISTS (
                SELECT 1 FROM omnivia_job_attempts a4
                WHERE a4.workspace_id = NEW.workspace_id AND a4.job_id = NEW.job_id
             ) AND NOT EXISTS (
                SELECT 1 FROM omnivia_job_attempts a5
                WHERE a5.workspace_id = NEW.workspace_id AND a5.job_id = NEW.job_id
                  AND a5.attempt_number = NEW.attempt_number
                  AND a5.attempt_number = (
                      SELECT MAX(a6.attempt_number) FROM omnivia_job_attempts a6
                      WHERE a6.workspace_id = NEW.workspace_id AND a6.job_id = NEW.job_id
                  )
                  AND a5.state = 'cancelled'
                  AND a5.finished_at_us = NEW.finished_at_us
             ))
          ));
    SELECT RAISE(ABORT, 'omnivia: terminal observation does not follow accepted recovery')
    WHERE NEW.terminal_observation_number > 1
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_application_job_controls c
        WHERE c.workspace_id = NEW.workspace_id AND c.job_id = NEW.job_id
          AND ((c.operation = 'job.retry'
                AND c.disposition IN ('retry_scheduled', 'resume_scheduled')
                AND c.source_terminal_observation_number = NEW.terminal_observation_number - 1)
               OR (c.control_kind = 'system'
                   AND c.operation = 'system.recovery'
                   AND c.disposition = 'recovery_requeued'))
          AND EXISTS (
              SELECT 1 FROM omnivia_job_events q
              WHERE q.workspace_id = NEW.workspace_id AND q.job_id = NEW.job_id
                AND q.state = 'queued' AND q.occurred_at_us = c.settled_at_us
          )
      );
    SELECT RAISE(ABORT, 'omnivia: cancelled observation has no accepted cancellation control')
    WHERE NEW.terminal_state = 'cancelled'
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_application_job_controls c
        WHERE c.workspace_id = NEW.workspace_id AND c.job_id = NEW.job_id
          AND c.operation = 'job.cancel'
          AND c.disposition = 'cancellation_requested'
          AND c.settled_at_us <= NEW.finished_at_us
          AND (NEW.terminal_observation_number = 1 OR c.settled_at_us >= (
              SELECT o.finished_at_us FROM omnivia_job_terminal_observations o
              WHERE o.workspace_id = NEW.workspace_id AND o.job_id = NEW.job_id
                AND o.terminal_observation_number = NEW.terminal_observation_number - 1
          ))
      );
    SELECT RAISE(ABORT, 'omnivia: terminal success result kind does not match job metadata')
    WHERE NEW.terminal_state = 'succeeded'
      AND EXISTS (
        SELECT 1 FROM omnivia_job_application_metadata m
        WHERE m.workspace_id = NEW.workspace_id AND m.job_id = NEW.job_id
          AND m.terminal_result_kind IS NOT NULL
          AND m.terminal_result_kind <> NEW.result_kind
      );
    SELECT RAISE(ABORT, 'omnivia: terminal failure must repeat the final attempt error')
    WHERE NEW.terminal_state = 'failed'
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_job_attempts a
        WHERE a.workspace_id = NEW.workspace_id AND a.job_id = NEW.job_id
          AND a.attempt_number = NEW.attempt_number
          AND a.error_json = NEW.error_json
      );
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_job_terminal_observations_update
BEFORE UPDATE ON omnivia_job_terminal_observations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_job_terminal_observations is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_job_terminal_observations_delete
BEFORE DELETE ON omnivia_job_terminal_observations
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_job_terminal_observations is append-only; DELETE is never permitted');
END;

-- The S0 executor writes this row after its domain callback.  Executed application
-- mutations must close over the exact bridge settlement; honest replay spends its
-- fresh grant against the original outcome and creates no second bridge row.
CREATE TRIGGER IF NOT EXISTS omnivia_guard_mutation_executions_application_job_closure
BEFORE INSERT ON omnivia_mutation_executions
WHEN NEW.execution_kind = 'executed'
 AND NEW.operation IN ('import.start', 'job.cancel', 'job.retry')
BEGIN
    SELECT RAISE(ABORT, 'omnivia: import.start execution lacks exact import settlement')
    WHERE NEW.operation = 'import.start'
      AND (SELECT COUNT(*) FROM omnivia_application_import_claims c
           WHERE c.workspace_id = NEW.workspace_id AND c.audit_ref = NEW.audit_ref
             AND c.settled_at_us = NEW.recorded_at_us) <> 1;
    SELECT RAISE(ABORT, 'omnivia: job control execution lacks exact control settlement')
    WHERE NEW.operation IN ('job.cancel', 'job.retry')
      AND (SELECT COUNT(*) FROM omnivia_application_job_controls c
           WHERE c.workspace_id = NEW.workspace_id AND c.audit_ref = NEW.audit_ref
             AND c.operation = NEW.operation
             AND c.fencing_generation = NEW.fencing_generation
             AND c.settled_at_us = NEW.recorded_at_us) <> 1;
END;

DROP TRIGGER omnivia_guard_job_attempts_insert;

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
        SELECT 1 FROM omnivia_durable_jobs j
        JOIN omnivia_mutation_guard g ON g.singleton = 1
        WHERE j.job_id = NEW.job_id AND j.state = 'claimed'
          AND j.fencing_generation = g.fencing_generation
          AND j.claimed_by_service_instance = g.service_instance_id
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
    SELECT RAISE(ABORT, 'omnivia: later attempt requires exact immutable recovery lineage')
    WHERE NEW.attempt_number > 1
      AND NOT EXISTS (
        SELECT 1 FROM omnivia_application_job_controls c
        WHERE c.workspace_id = NEW.workspace_id AND c.job_id = NEW.job_id
          AND c.resulting_state = 'queued'
          AND (c.operation = 'job.retry' OR c.control_kind = 'system')
          AND c.settled_at_us = (
              SELECT MAX(c2.settled_at_us) FROM omnivia_application_job_controls c2
              WHERE c2.workspace_id = NEW.workspace_id AND c2.job_id = NEW.job_id
                AND c2.resulting_state = 'queued'
                AND (c2.operation = 'job.retry' OR c2.control_kind = 'system')
          )
          AND EXISTS (
              SELECT 1 FROM omnivia_job_events q
              WHERE q.workspace_id = NEW.workspace_id AND q.job_id = NEW.job_id
                AND q.state = 'queued' AND q.occurred_at_us = c.settled_at_us
          )
          AND (
              (c.operation = 'job.retry'
               AND c.disposition IN ('retry_scheduled', 'resume_scheduled')
               AND c.source_terminal_observation_number = (
                   SELECT MAX(o.terminal_observation_number)
                   FROM omnivia_job_terminal_observations o
                   WHERE o.workspace_id = NEW.workspace_id AND o.job_id = NEW.job_id
               ))
              OR (c.control_kind = 'system' AND c.operation = 'system.recovery'
                  AND c.disposition = 'recovery_requeued'
                  AND c.settled_at_us = (
                      SELECT a.finished_at_us FROM omnivia_job_attempts a
                      WHERE a.workspace_id = NEW.workspace_id AND a.job_id = NEW.job_id
                        AND a.attempt_number = NEW.attempt_number - 1
                  ))
          )
      );
END;

DROP TRIGGER omnivia_guard_job_events_insert;

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
    SELECT RAISE(ABORT, 'omnivia: post-terminal event lacks recovery/control lineage')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_job_terminal_observations o
        WHERE o.workspace_id = NEW.workspace_id AND o.job_id = NEW.job_id
    )
      AND NEW.occurred_at_us >= (
        SELECT MAX(o.finished_at_us) FROM omnivia_job_terminal_observations o
        WHERE o.workspace_id = NEW.workspace_id AND o.job_id = NEW.job_id
      )
      AND NOT (
        (NEW.state = 'queued' AND EXISTS (
            SELECT 1 FROM omnivia_application_job_controls c
            WHERE c.workspace_id = NEW.workspace_id AND c.job_id = NEW.job_id
              AND c.resulting_state = 'queued'
              AND c.settled_at_us = NEW.occurred_at_us
              AND (
                  (c.operation = 'job.retry'
                   AND c.disposition IN ('retry_scheduled', 'resume_scheduled'))
                  OR c.control_kind = 'system'
                  OR (c.operation = 'job.cancel'
                      AND c.disposition = 'cancellation_requested')
              )
        ))
        OR (EXISTS (
            SELECT 1 FROM omnivia_application_job_controls c
            JOIN omnivia_job_events q
              ON q.workspace_id = c.workspace_id AND q.job_id = c.job_id
             AND q.state = 'queued' AND q.occurred_at_us = c.settled_at_us
            WHERE c.workspace_id = NEW.workspace_id AND c.job_id = NEW.job_id
              AND c.resulting_state = 'queued'
              AND (c.operation = 'job.retry' OR c.control_kind = 'system')
              AND c.settled_at_us >= (
                  SELECT MAX(o2.finished_at_us) FROM omnivia_job_terminal_observations o2
                  WHERE o2.workspace_id = NEW.workspace_id AND o2.job_id = NEW.job_id
              )
        ) AND (
            EXISTS (
                SELECT 1 FROM omnivia_job_attempts a
                WHERE a.workspace_id = NEW.workspace_id AND a.job_id = NEW.job_id
                  AND a.attempt_number = (
                      SELECT MAX(a2.attempt_number) FROM omnivia_job_attempts a2
                      WHERE a2.workspace_id = NEW.workspace_id AND a2.job_id = NEW.job_id
                  )
                  AND a.state = NEW.state
                  AND (NEW.state = 'running' OR a.finished_at_us = NEW.occurred_at_us)
            )
            OR EXISTS (
                SELECT 1 FROM omnivia_application_job_controls x
                WHERE x.workspace_id = NEW.workspace_id AND x.job_id = NEW.job_id
                  AND x.operation = 'job.cancel'
                  AND x.disposition = 'cancellation_requested'
                  AND x.resulting_state = NEW.state
                  AND x.settled_at_us = NEW.occurred_at_us
            )
        ))
      );
END;

DROP TRIGGER omnivia_guard_job_terminal_results_insert;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_job_terminal_results_insert
BEFORE INSERT ON omnivia_job_terminal_results
BEGIN
    SELECT RAISE(ABORT, 'omnivia: migration 0015 freezes new legacy terminal-result inserts');
END;
