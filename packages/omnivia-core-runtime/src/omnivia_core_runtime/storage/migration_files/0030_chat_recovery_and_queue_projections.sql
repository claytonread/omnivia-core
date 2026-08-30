-- Chat recovery and queue projections: the terminal Generation Attempt outcome
-- fact, durable generation text chunks, and the reorderable queue projection.
--
-- Additive successor to the immutable 0029 Chat foundation. Three tables, three
-- named indexes and guarded storage triggers. 0029 is not edited, relaxed or
-- rebuilt: every table here is new, every 0029 table keeps the writer identity,
-- mutation guard, fencing, workspace isolation, foreign keys, no-delete
-- discipline, canonical JSON rules and timestamp bounds it already had.
--
-- Why each table exists.
--
-- `omnivia_chat_generation_attempt_outcomes` closes a gap 0029 left open by
-- construction. An Attempt row is append-only and its CHECK requires
-- `ended_at_us` exactly when `state` is not `running`, so an Attempt written at
-- start -- the only time its end is unknown -- can never be given an end. The
-- durable start row and this terminal outcome fact therefore *compose* the
-- public GenerationAttempt projection: `state` reads `running` until an outcome
-- exists, and then reads that outcome with the end timestamp it binds. The
-- primary key is the Attempt, so an Attempt has at most one terminal outcome,
-- and the insert guard refuses an outcome for an Attempt 0029 already wrote
-- terminal -- a pre-0030 record keeps its own terminal statement rather than
-- gaining a second one.
--
-- That fact is also what makes a Job `retryable`: a Job whose base row is still
-- `running` and whose latest Attempt carries a failed terminal outcome projects
-- as the non-terminal `retryable` state, and appending the next Attempt projects
-- it as `running` again. Nothing here reopens a terminal Job. `retryable` is a
-- projected state only; it is never written to `omnivia_chat_generation_jobs.state`,
-- whose 0029 CHECK and transition trigger are untouched.
--
-- `omnivia_chat_generation_chunks` is the durable text of a generation stream,
-- keyed to the exact workspace/conversation/job/attempt, contiguous by ordinal,
-- deduplicated on the provider event id where the provider supplied one, and
-- bounded in UTF-8 bytes. Append-only, like every other stream fact in 0029:
-- there is no per-token transaction requirement, because a writer appends a
-- bounded batch of chunks inside one fenced transaction.
--
-- `omnivia_chat_queued_submission_order` is the reorderable projection over
-- 0029's queue. 0029's `queue_sequence` is creation identity -- it is `UNIQUE`
-- per conversation and its update trigger refuses to move it -- so a reorder
-- cannot be a rewrite of that column and is not attempted here. The projection
-- is instead one row per conversation holding the ordered submission ids as
-- exact canonical JSON, under one `version`.
--
-- One row rather than one row per submission, deliberately. SQLite evaluates a
-- UNIQUE index per updated row, not at end of statement, so a per-submission
-- `UNIQUE (workspace_id, conversation_id, position)` makes *every* reorder
-- impossible: swapping two positions collides on the first row written, in one
-- statement or two. Holding the whole order in one row makes the reorder
-- atomic by construction -- one compare-and-set, no partial reorder to observe,
-- no staging band to leak -- and lets the membership rule be stated in SQL:
-- every named submission must still be `queued` in this conversation, so a
-- claimed or terminal submission can never be moved. A conversation with no
-- projection row is not disordered: it reads in `queue_sequence` order, which
-- is what every 0029 database has.

CREATE TABLE IF NOT EXISTS omnivia_chat_generation_attempt_outcomes (
    workspace_id             TEXT    NOT NULL,
    conversation_id          TEXT    NOT NULL,
    generation_job_id        TEXT    NOT NULL,
    generation_attempt_id    TEXT    NOT NULL,
    outcome                  TEXT    NOT NULL,
    error_class              TEXT,
    error_detail             TEXT,
    schema_version           INTEGER NOT NULL,
    ended_at_us              INTEGER NOT NULL,
    recorded_at_us           INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, generation_attempt_id),
    UNIQUE (workspace_id, generation_job_id, generation_attempt_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(conversation_id) = 'text'
           AND length(conversation_id) BETWEEN 1 AND 128
           AND conversation_id GLOB '[A-Za-z0-9]*'
           AND conversation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(conversation_id, char(0)) = 0),
    CHECK (typeof(generation_job_id) = 'text'
           AND length(generation_job_id) BETWEEN 1 AND 128
           AND generation_job_id GLOB '[A-Za-z0-9]*'
           AND generation_job_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(generation_job_id, char(0)) = 0),
    CHECK (typeof(generation_attempt_id) = 'text'
           AND length(generation_attempt_id) BETWEEN 1 AND 128
           AND generation_attempt_id GLOB '[A-Za-z0-9]*'
           AND generation_attempt_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(generation_attempt_id, char(0)) = 0),
    CHECK (outcome IN ('succeeded', 'failed', 'cancelled')),
    CHECK (error_class IS NULL OR (typeof(error_class) = 'text'
           AND length(error_class) BETWEEN 1 AND 128
           AND error_class GLOB '[a-z]*'
           AND error_class NOT GLOB '*[^a-z0-9._-]*')),
    CHECK (error_detail IS NULL OR (typeof(error_detail) = 'text'
           AND length(CAST(error_detail AS BLOB)) <= 4096
           AND instr(error_detail, char(0)) = 0)),
    CHECK ((outcome = 'succeeded' AND error_class IS NULL AND error_detail IS NULL)
           OR (outcome IN ('failed', 'cancelled') AND error_class IS NOT NULL)),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),
    CHECK (typeof(ended_at_us) = 'integer' AND ended_at_us > 0),
    CHECK (typeof(recorded_at_us) = 'integer' AND recorded_at_us >= ended_at_us),

    FOREIGN KEY (workspace_id, conversation_id, generation_job_id)
        REFERENCES omnivia_chat_generation_jobs
            (workspace_id, conversation_id, generation_job_id),
    FOREIGN KEY (workspace_id, generation_job_id, generation_attempt_id)
        REFERENCES omnivia_chat_generation_attempts
            (workspace_id, generation_job_id, generation_attempt_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_chat_generation_chunks (
    workspace_id             TEXT    NOT NULL,
    conversation_id          TEXT    NOT NULL,
    generation_job_id        TEXT    NOT NULL,
    generation_attempt_id    TEXT    NOT NULL,
    chunk_ordinal            INTEGER NOT NULL,
    provider_event_id        TEXT,
    text_content             TEXT    NOT NULL,
    schema_version           INTEGER NOT NULL,
    created_at_us            INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, generation_attempt_id, chunk_ordinal),
    -- NULLs are distinct in SQLite, so this deduplicates exactly where the
    -- provider supplied an event id and constrains nothing where it did not,
    -- the same rule 0029 applies to `omnivia_chat_generation_events`.
    UNIQUE (workspace_id, generation_job_id, generation_attempt_id, provider_event_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(conversation_id) = 'text'
           AND length(conversation_id) BETWEEN 1 AND 128
           AND conversation_id GLOB '[A-Za-z0-9]*'
           AND conversation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(conversation_id, char(0)) = 0),
    CHECK (typeof(generation_job_id) = 'text'
           AND length(generation_job_id) BETWEEN 1 AND 128
           AND generation_job_id GLOB '[A-Za-z0-9]*'
           AND generation_job_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(generation_job_id, char(0)) = 0),
    CHECK (typeof(generation_attempt_id) = 'text'
           AND length(generation_attempt_id) BETWEEN 1 AND 128
           AND generation_attempt_id GLOB '[A-Za-z0-9]*'
           AND generation_attempt_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(generation_attempt_id, char(0)) = 0),
    CHECK (typeof(chunk_ordinal) = 'integer'
           AND chunk_ordinal BETWEEN 1 AND 1000000),
    CHECK (provider_event_id IS NULL OR (typeof(provider_event_id) = 'text'
           AND length(provider_event_id) BETWEEN 1 AND 128
           AND provider_event_id GLOB '[A-Za-z0-9]*'
           AND provider_event_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(provider_event_id, char(0)) = 0)),
    CHECK (typeof(text_content) = 'text'
           AND length(CAST(text_content AS BLOB)) BETWEEN 1 AND 65536
           AND instr(text_content, char(0)) = 0),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),

    FOREIGN KEY (workspace_id, conversation_id, generation_job_id)
        REFERENCES omnivia_chat_generation_jobs
            (workspace_id, conversation_id, generation_job_id),
    FOREIGN KEY (workspace_id, generation_job_id, generation_attempt_id)
        REFERENCES omnivia_chat_generation_attempts
            (workspace_id, generation_job_id, generation_attempt_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_chat_queued_submission_order (
    workspace_id       TEXT    NOT NULL,
    conversation_id    TEXT    NOT NULL,
    order_json         TEXT    NOT NULL,
    version            INTEGER NOT NULL,
    created_at_us      INTEGER NOT NULL,
    updated_at_us      INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, conversation_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(conversation_id) = 'text'
           AND length(conversation_id) BETWEEN 1 AND 128
           AND conversation_id GLOB '[A-Za-z0-9]*'
           AND conversation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(conversation_id, char(0)) = 0),
    CHECK (typeof(order_json) = 'text'
           AND length(CAST(order_json AS BLOB)) BETWEEN 2 AND 65536
           AND instr(order_json, char(0)) = 0),
    CHECK (typeof(version) = 'integer' AND version >= 1),
    CHECK (typeof(created_at_us) = 'integer' AND created_at_us > 0),
    CHECK (typeof(updated_at_us) = 'integer' AND updated_at_us >= created_at_us),

    FOREIGN KEY (workspace_id, conversation_id)
        REFERENCES omnivia_chat_conversations (workspace_id, conversation_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_generation_attempt_outcomes_job
    ON omnivia_chat_generation_attempt_outcomes
        (workspace_id, generation_job_id, ended_at_us, generation_attempt_id);

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_generation_chunks_order
    ON omnivia_chat_generation_chunks
        (workspace_id, generation_job_id, generation_attempt_id, chunk_ordinal);

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_queued_submission_order_conversation
    ON omnivia_chat_queued_submission_order
        (workspace_id, conversation_id, version);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_attempt_outcomes_insert
BEFORE INSERT ON omnivia_chat_generation_attempt_outcomes
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_generation_attempt_outcomes')
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
    SELECT RAISE(ABORT, 'omnivia: generation attempt outcome must name its own attempt conversation')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_chat_generation_attempts
        WHERE workspace_id = NEW.workspace_id
          AND generation_attempt_id = NEW.generation_attempt_id
          AND generation_job_id = NEW.generation_job_id
          AND conversation_id = NEW.conversation_id);
    SELECT RAISE(ABORT, 'omnivia: generation attempt already carries a terminal outcome')
    WHERE (
        SELECT state FROM omnivia_chat_generation_attempts
        WHERE workspace_id = NEW.workspace_id
          AND generation_attempt_id = NEW.generation_attempt_id) <> 'running';
    SELECT RAISE(ABORT, 'omnivia: generation attempt cannot end before it started')
    WHERE NEW.ended_at_us < (
        SELECT started_at_us FROM omnivia_chat_generation_attempts
        WHERE workspace_id = NEW.workspace_id
          AND generation_attempt_id = NEW.generation_attempt_id);
    SELECT RAISE(ABORT, 'omnivia: generation attempt cannot end before its own durable chunks')
    WHERE NEW.ended_at_us < (
        SELECT MAX(created_at_us) FROM omnivia_chat_generation_chunks
        WHERE workspace_id = NEW.workspace_id
          AND generation_job_id = NEW.generation_job_id
          AND generation_attempt_id = NEW.generation_attempt_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_attempt_outcomes_update
BEFORE UPDATE ON omnivia_chat_generation_attempt_outcomes
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_generation_attempt_outcomes is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_attempt_outcomes_delete
BEFORE DELETE ON omnivia_chat_generation_attempt_outcomes
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_generation_attempt_outcomes forbids DELETE');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_chunks_insert
BEFORE INSERT ON omnivia_chat_generation_chunks
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_generation_chunks')
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
    SELECT RAISE(ABORT, 'omnivia: generation chunk must name its own attempt conversation')
    WHERE NOT EXISTS (
        SELECT 1 FROM omnivia_chat_generation_attempts
        WHERE workspace_id = NEW.workspace_id
          AND generation_attempt_id = NEW.generation_attempt_id
          AND generation_job_id = NEW.generation_job_id
          AND conversation_id = NEW.conversation_id);
    SELECT RAISE(ABORT, 'omnivia: generation chunks must be contiguous from one')
    WHERE NEW.chunk_ordinal IS NOT (
        SELECT COALESCE(MAX(chunk_ordinal), 0) + 1
        FROM omnivia_chat_generation_chunks
        WHERE workspace_id = NEW.workspace_id
          AND generation_attempt_id = NEW.generation_attempt_id);
    SELECT RAISE(ABORT, 'omnivia: generation chunk cannot predate its attempt')
    WHERE NEW.created_at_us < (
        SELECT started_at_us FROM omnivia_chat_generation_attempts
        WHERE workspace_id = NEW.workspace_id
          AND generation_attempt_id = NEW.generation_attempt_id);
    SELECT RAISE(ABORT, 'omnivia: a terminated generation attempt admits no further chunk')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_chat_generation_attempt_outcomes
        WHERE workspace_id = NEW.workspace_id
          AND generation_attempt_id = NEW.generation_attempt_id)
       OR (SELECT state FROM omnivia_chat_generation_attempts
           WHERE workspace_id = NEW.workspace_id
             AND generation_attempt_id = NEW.generation_attempt_id) <> 'running';
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_chunks_update
BEFORE UPDATE ON omnivia_chat_generation_chunks
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_generation_chunks is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_chunks_delete
BEFORE DELETE ON omnivia_chat_generation_chunks
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_generation_chunks forbids DELETE');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_queued_submission_order_insert
BEFORE INSERT ON omnivia_chat_queued_submission_order
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_queued_submission_order')
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
    SELECT RAISE(ABORT, 'omnivia: queue order must be an exact canonical JSON array')
    WHERE json_valid(NEW.order_json) IS NOT 1
       OR json(NEW.order_json) <> NEW.order_json
       OR json_type(NEW.order_json) <> 'array'
       OR json_array_length(NEW.order_json) NOT BETWEEN 1 AND 1000;
    SELECT RAISE(ABORT, 'omnivia: queue order names only string submission identifiers')
    WHERE EXISTS (
        SELECT 1 FROM json_each(NEW.order_json) member WHERE member.type <> 'text');
    SELECT RAISE(ABORT, 'omnivia: queue order names a submission more than once')
    WHERE json_array_length(NEW.order_json)
       <> (SELECT COUNT(DISTINCT value) FROM json_each(NEW.order_json));
    SELECT RAISE(ABORT, 'omnivia: queue order may name only queued submissions of this conversation')
    WHERE json_array_length(NEW.order_json) <> (
        SELECT COUNT(*)
        FROM json_each(NEW.order_json) member
        JOIN omnivia_chat_queued_submissions q
          ON q.workspace_id = NEW.workspace_id
         AND q.conversation_id = NEW.conversation_id
         AND q.queued_submission_id = member.value
         AND q.state = 'queued');
    SELECT RAISE(ABORT, 'omnivia: queue order is inserted only at version one')
    WHERE NEW.version <> 1 OR NEW.updated_at_us <> NEW.created_at_us;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_queued_submission_order_update
BEFORE UPDATE ON omnivia_chat_queued_submission_order
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_chat_queued_submission_order')
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
       OR NEW.workspace_id <> OLD.workspace_id;
    SELECT RAISE(ABORT, 'omnivia: queue order identity and creation are immutable')
    WHERE NEW.workspace_id <> OLD.workspace_id
       OR NEW.conversation_id <> OLD.conversation_id
       OR NEW.created_at_us <> OLD.created_at_us;
    SELECT RAISE(ABORT, 'omnivia: queue order version must advance by one')
    WHERE NEW.version <> OLD.version + 1;
    SELECT RAISE(ABORT, 'omnivia: queue order updated_at_us must not move backwards')
    WHERE NEW.updated_at_us < OLD.updated_at_us;
    SELECT RAISE(ABORT, 'omnivia: queue order must be an exact canonical JSON array')
    WHERE json_valid(NEW.order_json) IS NOT 1
       OR json(NEW.order_json) <> NEW.order_json
       OR json_type(NEW.order_json) <> 'array'
       OR json_array_length(NEW.order_json) NOT BETWEEN 1 AND 1000;
    SELECT RAISE(ABORT, 'omnivia: queue order names only string submission identifiers')
    WHERE EXISTS (
        SELECT 1 FROM json_each(NEW.order_json) member WHERE member.type <> 'text');
    SELECT RAISE(ABORT, 'omnivia: queue order names a submission more than once')
    WHERE json_array_length(NEW.order_json)
       <> (SELECT COUNT(DISTINCT value) FROM json_each(NEW.order_json));
    SELECT RAISE(ABORT, 'omnivia: queue order may name only queued submissions of this conversation')
    WHERE json_array_length(NEW.order_json) <> (
        SELECT COUNT(*)
        FROM json_each(NEW.order_json) member
        JOIN omnivia_chat_queued_submissions q
          ON q.workspace_id = NEW.workspace_id
         AND q.conversation_id = NEW.conversation_id
         AND q.queued_submission_id = member.value
         AND q.state = 'queued');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_queued_submission_order_delete
BEFORE DELETE ON omnivia_chat_queued_submission_order
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_queued_submission_order forbids DELETE');
END;
