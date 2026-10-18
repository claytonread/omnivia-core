-- Resumable bounded scan state for engineering conflict discovery (AC-049).
--
-- Each progress row is one immutable, fenced record-id page. The cursor is a
-- stable governed-record identity, obtained through the explicit scan index below.
-- The row carries the accumulated authorized-input digest and the bounded global
-- top structural/lexical matches, so restart needs no process memory and a crash
-- either commits a whole page or leaves it available to retry.

CREATE INDEX IF NOT EXISTS omnivia_idx_engineering_discovery_record_scan
    ON omnivia_governed_records
        (workspace_id, domain_scope, governed_record_id, record_type, recorded_at_us);

CREATE TABLE IF NOT EXISTS omnivia_engineering_discovery_scan_progress (
    workspace_id             TEXT    NOT NULL,
    discovery_run_id         TEXT    NOT NULL,
    batch_sequence           INTEGER NOT NULL,
    progress_id              TEXT    NOT NULL,
    cursor_record_id         TEXT    NOT NULL,
    frontier_digest          TEXT    NOT NULL,
    authorized_frontier_size INTEGER NOT NULL,
    structural_considered    INTEGER NOT NULL,
    lexical_considered       INTEGER NOT NULL,
    top_matches_json         TEXT    NOT NULL,
    recorded_at_us           INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, discovery_run_id, batch_sequence),
    UNIQUE (workspace_id, progress_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(discovery_run_id) = 'text'
           AND length(discovery_run_id) BETWEEN 1 AND 128
           AND instr(discovery_run_id, char(0)) = 0),
    CHECK (typeof(batch_sequence) = 'integer' AND batch_sequence > 0),
    CHECK (typeof(progress_id) = 'text' AND length(progress_id) BETWEEN 1 AND 128
           AND progress_id GLOB '[A-Za-z0-9]*'
           AND progress_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(progress_id, char(0)) = 0),
    CHECK (typeof(cursor_record_id) = 'text'
           AND length(cursor_record_id) BETWEEN 1 AND 128
           AND cursor_record_id GLOB '[A-Za-z0-9]*'
           AND cursor_record_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(cursor_record_id, char(0)) = 0),
    CHECK (typeof(frontier_digest) = 'text' AND length(frontier_digest) = 71
           AND substr(frontier_digest, 1, 7) = 'sha256:'
           AND substr(frontier_digest, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(authorized_frontier_size) = 'integer'
           AND authorized_frontier_size BETWEEN 0 AND 2147483647),
    CHECK (typeof(structural_considered) = 'integer'
           AND structural_considered BETWEEN 0 AND 2147483647),
    CHECK (typeof(lexical_considered) = 'integer'
           AND lexical_considered BETWEEN 0 AND 2147483647),
    CHECK (typeof(top_matches_json) = 'text'
           AND length(CAST(top_matches_json AS BLOB)) BETWEEN 29 AND 262144
           AND json_valid(top_matches_json) = 1
           AND json_type(top_matches_json) = 'object'
           AND json(top_matches_json) = top_matches_json),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us > 0),

    FOREIGN KEY (workspace_id, discovery_run_id)
        REFERENCES omnivia_engineering_discovery_runs (workspace_id, discovery_run_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_engineering_discovery_scan_progress_latest
    ON omnivia_engineering_discovery_scan_progress
        (workspace_id, discovery_run_id, batch_sequence DESC);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_discovery_scan_progress_insert
BEFORE INSERT ON omnivia_engineering_discovery_scan_progress
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_discovery_scan_progress')
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
    SELECT RAISE(ABORT, 'omnivia: discovery scan progress requires a queued run and a contiguous increasing cursor')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_engineering_discovery_runs r
        JOIN omnivia_engineering_discovery_run_events q
          ON q.workspace_id = r.workspace_id
         AND q.discovery_run_id = r.discovery_run_id
         AND q.event_sequence = 1 AND q.state = 'queued'
        WHERE r.workspace_id = NEW.workspace_id
          AND r.discovery_run_id = NEW.discovery_run_id
          AND NEW.recorded_at_us >= r.enqueued_at_us
          AND NOT EXISTS (
              SELECT 1 FROM omnivia_engineering_discovery_run_events terminal
              WHERE terminal.workspace_id = r.workspace_id
                AND terminal.discovery_run_id = r.discovery_run_id
                AND terminal.event_sequence = 2)
          AND NEW.batch_sequence IS (
              SELECT COALESCE(MAX(p.batch_sequence), 0) + 1
              FROM omnivia_engineering_discovery_scan_progress p
              WHERE p.workspace_id = NEW.workspace_id
                AND p.discovery_run_id = NEW.discovery_run_id)
          AND (NEW.batch_sequence = 1 OR EXISTS (
              SELECT 1 FROM omnivia_engineering_discovery_scan_progress prior
              WHERE prior.workspace_id = NEW.workspace_id
                AND prior.discovery_run_id = NEW.discovery_run_id
                AND prior.batch_sequence = NEW.batch_sequence - 1
                AND prior.cursor_record_id < NEW.cursor_record_id
                AND prior.authorized_frontier_size <= NEW.authorized_frontier_size
                AND prior.structural_considered <= NEW.structural_considered
                AND prior.lexical_considered <= NEW.lexical_considered
                AND prior.recorded_at_us <= NEW.recorded_at_us)));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_discovery_scan_progress_update
BEFORE UPDATE ON omnivia_engineering_discovery_scan_progress
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_discovery_scan_progress is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_discovery_scan_progress_delete
BEFORE DELETE ON omnivia_engineering_discovery_scan_progress
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_discovery_scan_progress is append-only; DELETE is never permitted');
END;
