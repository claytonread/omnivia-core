-- Durable bounded scheduling for captured Engineering Memory source.
--
-- The immutable 0056 capture header remains the provenance truth.  This migration
-- adds only mutable, service-owned scheduling projections:
--
-- * one queue row per exact (workspace, installation, snapshot), carrying the
--   checkout/stream binding and, for captures made by the live producer, the exact
--   source frontier observed before the filesystem effect;
-- * one scheduler row per installation, carrying lane, queue, checkout and bounded
--   legacy-seeding cursors across service restarts; and
-- * covering indexes for keyset lookup.  Runtime seeding walks pre-0058 capture
--   headers in bounded batches.  Migration application therefore never performs an
--   unbounded history copy.

CREATE INDEX IF NOT EXISTS omnivia_idx_engineering_capture_producer_seed
    ON omnivia_engineering_snapshot_captures
        (workspace_id, installation_id, captured_at_us, snapshot_id,
         repository_id, checkout_id);

CREATE INDEX IF NOT EXISTS omnivia_idx_engineering_checkout_producer
    ON omnivia_engineering_checkouts
        (workspace_id, installation_id, checkout_id, repository_id, checkout_hint);

CREATE TABLE IF NOT EXISTS omnivia_engineering_source_producer_queue (
    workspace_id                     TEXT    NOT NULL,
    installation_id                  TEXT    NOT NULL,
    snapshot_id                      TEXT    NOT NULL,
    repository_id                    TEXT    NOT NULL,
    checkout_id                      TEXT    NOT NULL,
    stream_id                        TEXT    NOT NULL,
    expected_frontier                INTEGER,
    expected_predecessor_snapshot_id TEXT,
    state                            TEXT    NOT NULL,
    available_at_us                  INTEGER NOT NULL,
    attempt_count                    INTEGER NOT NULL,
    enqueued_at_us                   INTEGER NOT NULL,
    last_attempt_at_us               INTEGER,
    settled_at_us                    INTEGER,

    PRIMARY KEY (workspace_id, installation_id, snapshot_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(installation_id) = 'text'
           AND length(installation_id) BETWEEN 1 AND 128
           AND instr(installation_id, char(0)) = 0),
    CHECK (typeof(snapshot_id) = 'text' AND length(snapshot_id) BETWEEN 1 AND 128
           AND snapshot_id GLOB '[A-Za-z0-9]*'
           AND snapshot_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(snapshot_id, char(0)) = 0),
    CHECK (typeof(repository_id) = 'text' AND length(repository_id) BETWEEN 1 AND 128
           AND repository_id GLOB '[A-Za-z0-9]*'
           AND repository_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(repository_id, char(0)) = 0),
    CHECK (typeof(checkout_id) = 'text' AND length(checkout_id) BETWEEN 1 AND 128
           AND checkout_id GLOB '[A-Za-z0-9]*'
           AND checkout_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(checkout_id, char(0)) = 0),
    CHECK (typeof(stream_id) = 'text' AND length(stream_id) BETWEEN 1 AND 128
           AND stream_id GLOB '[A-Za-z0-9]*'
           AND stream_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(stream_id, char(0)) = 0),
    CHECK (expected_frontier IS NULL
           OR (typeof(expected_frontier) = 'integer'
               AND expected_frontier BETWEEN 0 AND 2147483647)),
    CHECK (expected_predecessor_snapshot_id IS NULL
           OR (typeof(expected_predecessor_snapshot_id) = 'text'
               AND length(expected_predecessor_snapshot_id) BETWEEN 1 AND 128
               AND expected_predecessor_snapshot_id GLOB '[A-Za-z0-9]*'
               AND expected_predecessor_snapshot_id
                   NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(expected_predecessor_snapshot_id, char(0)) = 0)),
    CHECK ((expected_frontier IS NULL
            AND expected_predecessor_snapshot_id IS NULL)
           OR (expected_frontier = 0
               AND expected_predecessor_snapshot_id IS NULL)
           OR (expected_frontier > 0
               AND expected_predecessor_snapshot_id IS NOT NULL)),
    CHECK (state IN ('pending', 'retry', 'settled')),
    CHECK (typeof(available_at_us) = 'integer' AND available_at_us > 0),
    CHECK (typeof(attempt_count) = 'integer'
           AND attempt_count BETWEEN 0 AND 1000000),
    CHECK (typeof(enqueued_at_us) = 'integer' AND enqueued_at_us > 0),
    CHECK (last_attempt_at_us IS NULL
           OR (typeof(last_attempt_at_us) = 'integer'
               AND last_attempt_at_us >= enqueued_at_us)),
    CHECK (settled_at_us IS NULL
           OR (typeof(settled_at_us) = 'integer'
               AND settled_at_us >= enqueued_at_us)),
    CHECK ((attempt_count = 0) = (last_attempt_at_us IS NULL)),
    CHECK ((state = 'pending' AND attempt_count = 0 AND settled_at_us IS NULL)
           OR (state = 'retry' AND attempt_count > 0 AND settled_at_us IS NULL)
           OR (state = 'settled' AND settled_at_us IS NOT NULL)),

    FOREIGN KEY (workspace_id, snapshot_id)
        REFERENCES omnivia_engineering_snapshot_captures
            (workspace_id, snapshot_id),
    FOREIGN KEY (workspace_id, checkout_id)
        REFERENCES omnivia_engineering_checkouts (workspace_id, checkout_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_engineering_source_producer_eligible
    ON omnivia_engineering_source_producer_queue
        (workspace_id, installation_id, state, available_at_us, snapshot_id,
         repository_id, checkout_id, stream_id, expected_frontier,
         expected_predecessor_snapshot_id);

CREATE TABLE IF NOT EXISTS omnivia_engineering_source_producer_state (
    workspace_id                  TEXT    NOT NULL,
    installation_id               TEXT    NOT NULL,
    next_lane                     TEXT    NOT NULL,
    queue_cursor_available_at_us  INTEGER,
    queue_cursor_snapshot_id      TEXT,
    checkout_cursor               TEXT,
    legacy_cursor_captured_at_us INTEGER,
    legacy_cursor_snapshot_id     TEXT,
    legacy_seed_through_captured_at_us INTEGER,
    legacy_seed_through_snapshot_id TEXT,
    legacy_seed_complete          INTEGER NOT NULL,
    updated_at_us                 INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, installation_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(installation_id) = 'text'
           AND length(installation_id) BETWEEN 1 AND 128
           AND instr(installation_id, char(0)) = 0),
    CHECK (next_lane IN ('recovery', 'checkout')),
    CHECK ((queue_cursor_available_at_us IS NULL)
           = (queue_cursor_snapshot_id IS NULL)),
    CHECK (queue_cursor_available_at_us IS NULL
           OR (typeof(queue_cursor_available_at_us) = 'integer'
               AND queue_cursor_available_at_us > 0)),
    CHECK (queue_cursor_snapshot_id IS NULL
           OR (typeof(queue_cursor_snapshot_id) = 'text'
               AND length(queue_cursor_snapshot_id) BETWEEN 1 AND 128
               AND queue_cursor_snapshot_id GLOB '[A-Za-z0-9]*'
               AND queue_cursor_snapshot_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(queue_cursor_snapshot_id, char(0)) = 0)),
    CHECK (checkout_cursor IS NULL
           OR (typeof(checkout_cursor) = 'text'
               AND length(checkout_cursor) BETWEEN 1 AND 128
               AND checkout_cursor GLOB '[A-Za-z0-9]*'
               AND checkout_cursor NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(checkout_cursor, char(0)) = 0)),
    CHECK ((legacy_cursor_captured_at_us IS NULL)
           = (legacy_cursor_snapshot_id IS NULL)),
    CHECK (legacy_cursor_captured_at_us IS NULL
           OR (typeof(legacy_cursor_captured_at_us) = 'integer'
               AND legacy_cursor_captured_at_us > 0)),
    CHECK (legacy_cursor_snapshot_id IS NULL
           OR (typeof(legacy_cursor_snapshot_id) = 'text'
               AND length(legacy_cursor_snapshot_id) BETWEEN 1 AND 128
               AND legacy_cursor_snapshot_id GLOB '[A-Za-z0-9]*'
               AND legacy_cursor_snapshot_id NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(legacy_cursor_snapshot_id, char(0)) = 0)),
    CHECK ((legacy_seed_through_captured_at_us IS NULL)
           = (legacy_seed_through_snapshot_id IS NULL)),
    CHECK (legacy_seed_through_captured_at_us IS NULL
           OR (typeof(legacy_seed_through_captured_at_us) = 'integer'
               AND legacy_seed_through_captured_at_us > 0)),
    CHECK (legacy_seed_through_snapshot_id IS NULL
           OR (typeof(legacy_seed_through_snapshot_id) = 'text'
               AND length(legacy_seed_through_snapshot_id) BETWEEN 1 AND 128
               AND legacy_seed_through_snapshot_id GLOB '[A-Za-z0-9]*'
               AND legacy_seed_through_snapshot_id
                   NOT GLOB '*[^A-Za-z0-9._:-]*'
               AND instr(legacy_seed_through_snapshot_id, char(0)) = 0)),
    CHECK (legacy_cursor_captured_at_us IS NULL
           OR (legacy_seed_through_captured_at_us IS NOT NULL
               AND (legacy_cursor_captured_at_us, legacy_cursor_snapshot_id)
                   <= (legacy_seed_through_captured_at_us,
                       legacy_seed_through_snapshot_id))),
    CHECK (legacy_seed_complete IN (0, 1)),
    CHECK (typeof(updated_at_us) = 'integer' AND updated_at_us > 0)
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_source_producer_queue_insert
BEFORE INSERT ON omnivia_engineering_source_producer_queue
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_source_producer_queue')
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
    SELECT RAISE(ABORT, 'omnivia: source producer work must match its exact sealed capture')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_engineering_snapshot_captures c
        WHERE c.workspace_id = NEW.workspace_id
          AND c.snapshot_id = NEW.snapshot_id
          AND c.repository_id = NEW.repository_id
          AND c.installation_id = NEW.installation_id
          AND c.checkout_id = NEW.checkout_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_source_producer_queue_update
BEFORE UPDATE ON omnivia_engineering_source_producer_queue
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_engineering_source_producer_queue')
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
    SELECT RAISE(ABORT, 'omnivia: source producer work identity and intent are immutable')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id
       OR NEW.installation_id IS NOT OLD.installation_id
       OR NEW.snapshot_id IS NOT OLD.snapshot_id
       OR NEW.repository_id IS NOT OLD.repository_id
       OR NEW.checkout_id IS NOT OLD.checkout_id
       OR NEW.stream_id IS NOT OLD.stream_id
       OR ((NEW.expected_frontier IS NOT OLD.expected_frontier
            OR NEW.expected_predecessor_snapshot_id
                IS NOT OLD.expected_predecessor_snapshot_id)
           AND NOT (
                OLD.expected_frontier IS NULL
                AND OLD.expected_predecessor_snapshot_id IS NULL
                AND NEW.expected_frontier IS NOT NULL
                AND OLD.state IN ('pending', 'retry')
                AND NEW.state = OLD.state
                AND OLD.available_at_us = NEW.available_at_us
                AND OLD.attempt_count = NEW.attempt_count
                AND NEW.last_attempt_at_us IS OLD.last_attempt_at_us
                AND OLD.settled_at_us IS NULL AND NEW.settled_at_us IS NULL
           ))
       OR NEW.enqueued_at_us IS NOT OLD.enqueued_at_us;
    SELECT RAISE(ABORT, 'omnivia: source producer work transition is invalid')
    WHERE (OLD.state = 'settled'
           AND (NEW.state IS NOT OLD.state
                OR NEW.available_at_us IS NOT OLD.available_at_us
                OR NEW.attempt_count IS NOT OLD.attempt_count
                OR NEW.last_attempt_at_us IS NOT OLD.last_attempt_at_us
                OR NEW.settled_at_us IS NOT OLD.settled_at_us))
       OR (OLD.state = 'pending' AND NEW.state = 'pending'
           AND NOT (
                OLD.expected_frontier IS NULL
                AND OLD.expected_predecessor_snapshot_id IS NULL
                AND NEW.expected_frontier IS NOT NULL
           ))
       OR (OLD.state = 'pending'
           AND NEW.state NOT IN ('pending', 'retry', 'settled'))
       OR (OLD.state = 'retry' AND NEW.state NOT IN ('retry', 'settled'))
       OR NEW.attempt_count < OLD.attempt_count
       OR NEW.attempt_count > OLD.attempt_count + 1
       OR NEW.available_at_us < OLD.available_at_us
       OR (OLD.last_attempt_at_us IS NOT NULL
           AND NEW.last_attempt_at_us < OLD.last_attempt_at_us)
       OR (OLD.settled_at_us IS NOT NULL
           AND NEW.settled_at_us IS NOT OLD.settled_at_us);
END;

-- A successful captured-source append settles its matching projection in the
-- same fenced transaction.  This closes the reply-loss window without making
-- the append-only event depend on a later producer poll.
CREATE TRIGGER IF NOT EXISTS omnivia_settle_engineering_source_producer_queue
BEFORE INSERT ON omnivia_engineering_source_events
WHEN NEW.manifest_format = 'captured_v1'
BEGIN
    UPDATE omnivia_engineering_source_producer_queue
    SET state = 'settled',
        available_at_us = max(available_at_us, NEW.recorded_at_us),
        attempt_count = min(attempt_count + 1, 1000000),
        last_attempt_at_us = max(enqueued_at_us,
                                 coalesce(last_attempt_at_us, 0),
                                 NEW.recorded_at_us),
        settled_at_us = max(enqueued_at_us,
                            coalesce(last_attempt_at_us, 0),
                            NEW.recorded_at_us)
    WHERE workspace_id = NEW.workspace_id
      AND snapshot_id = NEW.snapshot_id
      AND stream_id = NEW.stream_id
      AND state != 'settled';
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_source_producer_queue_delete
BEFORE DELETE ON omnivia_engineering_source_producer_queue
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_source_producer_queue is durable; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_source_producer_state_insert
BEFORE INSERT ON omnivia_engineering_source_producer_state
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_source_producer_state')
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
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_source_producer_state_update
BEFORE UPDATE ON omnivia_engineering_source_producer_state
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_engineering_source_producer_state')
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
    SELECT RAISE(ABORT, 'omnivia: source producer scheduler identity is immutable')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id
       OR NEW.installation_id IS NOT OLD.installation_id
       OR NEW.legacy_seed_through_captured_at_us
            IS NOT OLD.legacy_seed_through_captured_at_us
       OR NEW.legacy_seed_through_snapshot_id
            IS NOT OLD.legacy_seed_through_snapshot_id;
    SELECT RAISE(ABORT, 'omnivia: completed source producer seeding cannot reopen')
    WHERE NEW.legacy_seed_complete < OLD.legacy_seed_complete
       OR (OLD.legacy_seed_complete = 1
           AND (NEW.legacy_cursor_captured_at_us
                    IS NOT OLD.legacy_cursor_captured_at_us
                OR NEW.legacy_cursor_snapshot_id
                    IS NOT OLD.legacy_cursor_snapshot_id));
    SELECT RAISE(ABORT, 'omnivia: source producer seed cursor cannot move backwards')
    WHERE OLD.legacy_cursor_captured_at_us IS NOT NULL
      AND (NEW.legacy_cursor_captured_at_us IS NULL
           OR (NEW.legacy_cursor_captured_at_us,
               NEW.legacy_cursor_snapshot_id)
              < (OLD.legacy_cursor_captured_at_us,
                 OLD.legacy_cursor_snapshot_id));
    SELECT RAISE(ABORT, 'omnivia: source producer scheduler time cannot move backwards')
    WHERE NEW.updated_at_us < OLD.updated_at_us;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_engineering_source_producer_state_delete
BEFORE DELETE ON omnivia_engineering_source_producer_state
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_engineering_source_producer_state is durable; DELETE is never permitted');
END;
