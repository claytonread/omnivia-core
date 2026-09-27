-- Engineering source invalidation (SPEC-CORE-ENGMEM-001; spec §15.3;
-- AC-057/AC-061). Allocation 0059 (Engineering Memory, predecessor 0058).
--
-- Additive only. This gives the durable invalidation worker (0059's Python
-- module) two things the record and evaluator already had no need of:
--
--   omnivia_engineering_source_streams  gains `processed_sequence` (the
--                                       worker's watermark: every contiguous
--                                       covered event up to and including it
--                                       has had its dependents durably
--                                       reassessed) and a nullable
--                                       `pending_dependent_record_id` /
--                                       `pending_dependent_version` pair: a
--                                       durable keyset cursor over the
--                                       *next* event's scoped dependency sets,
--                                       when one event's fan-out does not fit
--                                       one bounded pass. NULL means "this
--                                       event's fan-out has not started (or
--                                       just completed)"; otherwise it is the
--                                       last (record_id, version) a page
--                                       durably scanned, so the next page
--                                       resumes strictly after it -- stable
--                                       under concurrent inserts elsewhere in
--                                       the ordering, unlike an OFFSET count.
--                                       `processed_sequence` only advances,
--                                       and the pair resets to NULL exactly
--                                       when it does. The coverage barrier is
--                                       the durable announcement of new work;
--                                       the gap between it and the watermark
--                                       below it *is* the queue, so no
--                                       separate work table is needed.
--   omnivia_engineering_dependency_sets gains a repository/stream scope index
--                                       so every event pages bounded dependency
--                                       sets and probes only their at-most-64
--                                       sealed selectors. This prevents either
--                                       same-path history in another scope or a
--                                       captured_v1 10,000-file manifest from
--                                       making one tick unbounded.
--
-- The two source-stream guard triggers are replaced under their own names
-- (SQLite cannot alter a trigger), exactly as 0052 replaced a dependency-set
-- trigger: every 0050 check is kept verbatim, and what follows is only the
-- new invariant over the two added columns.

ALTER TABLE omnivia_engineering_source_streams
ADD COLUMN processed_sequence INTEGER NOT NULL DEFAULT 0
CHECK (typeof(processed_sequence) = 'integer' AND processed_sequence >= 0);

ALTER TABLE omnivia_engineering_source_streams
ADD COLUMN pending_dependent_record_id TEXT
CHECK (pending_dependent_record_id IS NULL
       OR (typeof(pending_dependent_record_id) = 'text'
           AND length(pending_dependent_record_id) BETWEEN 1 AND 128
           AND pending_dependent_record_id GLOB '[A-Za-z0-9]*'
           AND pending_dependent_record_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(pending_dependent_record_id, char(0)) = 0));

ALTER TABLE omnivia_engineering_source_streams
ADD COLUMN pending_dependent_version TEXT
CHECK (pending_dependent_version IS NULL
       OR (typeof(pending_dependent_version) = 'text'
           AND length(pending_dependent_version) BETWEEN 1 AND 128
           AND pending_dependent_version GLOB '[A-Za-z0-9]*'
           AND pending_dependent_version NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(pending_dependent_version, char(0)) = 0));

CREATE INDEX IF NOT EXISTS omnivia_idx_engineering_dependency_sets_scope
    ON omnivia_engineering_dependency_sets
       (workspace_id, repository_id, stream_id, record_id, version);

DROP TRIGGER omnivia_guard_omnivia_engineering_source_streams_insert;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_source_streams_insert
BEFORE INSERT ON omnivia_engineering_source_streams
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_engineering_source_streams')
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
    SELECT RAISE(ABORT, 'omnivia: a source stream starts uncovered, unprocessed and is owned by the principal whose audited source record or capture commit opens it')
    WHERE NEW.covered_sequence IS NOT 0
       OR NEW.processed_sequence IS NOT 0
       OR NEW.pending_dependent_record_id IS NOT NULL
       OR NEW.pending_dependent_version IS NOT NULL
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_application_audit_events a
            WHERE a.audit_ref = NEW.audit_ref AND a.workspace_id = NEW.workspace_id
              AND a.principal_id = NEW.principal_id
              AND a.operation IN ('engineering.source.record', 'engineering.source.capture.commit'));
END;

DROP TRIGGER omnivia_guard_omnivia_engineering_source_streams_update;

-- The coverage barrier check: the old barrier was validated when it was
-- written and never decreases, so only the newly covered range (OLD, NEW] is
-- counted: one primary-key range scan of at most one pending window, however
-- long the stream's history.
--
-- The invalidation-progress check below it: the watermark never runs ahead of
-- coverage and advances one event at a time, so it can never skip an event's
-- dependents unaccounted for. Its per-event keyset cursor -- the last
-- (record_id, version) a page durably scanned -- is meaningful only while
-- that event is still in progress: advancing the watermark resets it to NULL
-- in the same write, and holding the watermark still may leave it unchanged
-- while a newer source head is announced or carry it forward in
-- (record_id, version) order, never back to NULL or an earlier key. No comment
-- sits inside the trigger body itself: the
-- migrator's statement splitter and a plain `executescript` replay must store
-- the same trigger byte for byte.
CREATE TRIGGER IF NOT EXISTS omnivia_guard_omnivia_engineering_source_streams_update
BEFORE UPDATE ON omnivia_engineering_source_streams
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_engineering_source_streams')
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
    SELECT RAISE(ABORT, 'omnivia: a source stream binding is immutable; only its head, coverage and invalidation progress advance')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id
       OR NEW.stream_id IS NOT OLD.stream_id
       OR NEW.repository_id IS NOT OLD.repository_id
       OR NEW.principal_id IS NOT OLD.principal_id
       OR NEW.registered_at_us IS NOT OLD.registered_at_us
       OR NEW.announced_sequence < OLD.announced_sequence
       OR NEW.covered_sequence < OLD.covered_sequence
       OR NEW.processed_sequence < OLD.processed_sequence
       OR NEW.updated_at_us < OLD.updated_at_us;
    SELECT RAISE(ABORT, 'omnivia: the source coverage barrier may not cross a missing event or advance past one pending window')
    WHERE NEW.covered_sequence > OLD.covered_sequence
      AND (NEW.covered_sequence - OLD.covered_sequence > 64
           OR (SELECT COUNT(*) FROM omnivia_engineering_source_events e
               WHERE e.workspace_id = NEW.workspace_id AND e.stream_id = NEW.stream_id
                 AND e.sequence > OLD.covered_sequence
                 AND e.sequence <= NEW.covered_sequence)
              IS NOT NEW.covered_sequence - OLD.covered_sequence);
    SELECT RAISE(ABORT, 'omnivia: invalidation progress may not exceed coverage, may not skip an unprocessed event, and its dependent keyset cursor resets to NULL exactly when its watermark advances and otherwise never regresses')
    WHERE NEW.processed_sequence > NEW.covered_sequence
       OR NEW.processed_sequence - OLD.processed_sequence > 1
       OR ((NEW.pending_dependent_record_id IS NULL)
           IS NOT (NEW.pending_dependent_version IS NULL))
       OR (NEW.processed_sequence > OLD.processed_sequence
           AND (NEW.pending_dependent_record_id IS NOT NULL
                OR NEW.pending_dependent_version IS NOT NULL))
       OR (NEW.processed_sequence IS OLD.processed_sequence
           AND OLD.pending_dependent_record_id IS NOT NULL
           AND (NEW.pending_dependent_record_id IS NULL
                OR NEW.pending_dependent_record_id < OLD.pending_dependent_record_id
                OR (NEW.pending_dependent_record_id IS OLD.pending_dependent_record_id
                    AND NEW.pending_dependent_version < OLD.pending_dependent_version)));
    SELECT RAISE(ABORT, 'omnivia: only the stream owner''s audited source record or capture commit may advance it')
    WHERE NOT EXISTS (
            SELECT 1 FROM omnivia_application_audit_events a
            WHERE a.audit_ref = NEW.audit_ref AND a.workspace_id = NEW.workspace_id
              AND a.principal_id = NEW.principal_id
              AND a.operation IN ('engineering.source.record', 'engineering.source.capture.commit'));
END;
