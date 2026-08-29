-- Chat Gate B successor state.
--
-- Additive only. Migration 0029 remains the immutable Chat foundation; this
-- migration adds the durable facts and mutable projections needed by the
-- direct supported-host Chat scenarios without reopening 0029 rows:
--
-- * job status projection, including the non-terminal `retryable` state;
-- * append-only attempt terminal outcomes;
-- * append-only provider text chunks for crash/replay reconstruction; and
-- * a CAS-protected queue-order projection separate from immutable
--   `queue_sequence`.
--
-- No Provider credential, SDK object, endpoint, request header or raw external
-- body is stored here. Text chunks are only the normalized Chat content the
-- assistant is allowed to display and later materialize as a Message.

CREATE TABLE IF NOT EXISTS omnivia_chat_generation_job_status_projection (
    workspace_id            TEXT    NOT NULL,
    conversation_id         TEXT    NOT NULL,
    generation_job_id       TEXT    NOT NULL,
    state                   TEXT    NOT NULL,
    current_attempt_id      TEXT,
    result_message_id       TEXT,
    sanitized_error_code    TEXT,
    sanitized_error_detail  TEXT,
    version                 INTEGER NOT NULL,
    schema_version          INTEGER NOT NULL,
    updated_at_us           INTEGER NOT NULL,
    finished_at_us          INTEGER,

    PRIMARY KEY (workspace_id, generation_job_id),
    UNIQUE (workspace_id, conversation_id, generation_job_id),

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
    CHECK (state IN ('queued', 'running', 'retryable', 'succeeded', 'failed', 'cancelled')),
    CHECK (current_attempt_id IS NULL OR (typeof(current_attempt_id) = 'text'
           AND length(current_attempt_id) BETWEEN 1 AND 128
           AND current_attempt_id GLOB '[A-Za-z0-9]*'
           AND current_attempt_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(current_attempt_id, char(0)) = 0)),
    CHECK (result_message_id IS NULL OR (typeof(result_message_id) = 'text'
           AND length(result_message_id) BETWEEN 1 AND 128
           AND result_message_id GLOB '[A-Za-z0-9]*'
           AND result_message_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(result_message_id, char(0)) = 0)),
    CHECK (sanitized_error_code IS NULL OR (typeof(sanitized_error_code) = 'text'
           AND length(sanitized_error_code) BETWEEN 1 AND 128
           AND sanitized_error_code GLOB '[a-z]*'
           AND sanitized_error_code NOT GLOB '*[^a-z0-9._-]*')),
    CHECK (sanitized_error_detail IS NULL
           OR (typeof(sanitized_error_detail) = 'text'
               AND length(CAST(sanitized_error_detail AS BLOB)) <= 4096
               AND instr(sanitized_error_detail, char(0)) = 0)),
    CHECK (typeof(version) = 'integer' AND version >= 1),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),
    CHECK (typeof(updated_at_us) = 'integer' AND updated_at_us > 0),
    CHECK (finished_at_us IS NULL OR (typeof(finished_at_us) = 'integer'
           AND finished_at_us >= updated_at_us)),
    CHECK ((state = 'running' AND current_attempt_id IS NOT NULL)
           OR state <> 'running'),
    CHECK ((state = 'retryable'
            AND current_attempt_id IS NOT NULL
            AND sanitized_error_code IS NOT NULL
            AND result_message_id IS NULL
            AND finished_at_us IS NULL)
           OR state <> 'retryable'),
    CHECK ((state = 'succeeded'
            AND current_attempt_id IS NOT NULL
            AND result_message_id IS NOT NULL
            AND sanitized_error_code IS NULL
            AND finished_at_us IS NOT NULL)
           OR state <> 'succeeded'),
    CHECK ((state IN ('failed', 'cancelled')
            AND current_attempt_id IS NOT NULL
            AND result_message_id IS NULL
            AND sanitized_error_code IS NOT NULL
            AND finished_at_us IS NOT NULL)
           OR state NOT IN ('failed', 'cancelled')),

    FOREIGN KEY (workspace_id, conversation_id, generation_job_id)
        REFERENCES omnivia_chat_generation_jobs
            (workspace_id, conversation_id, generation_job_id),
    FOREIGN KEY (workspace_id, generation_job_id, current_attempt_id)
        REFERENCES omnivia_chat_generation_attempts
            (workspace_id, generation_job_id, generation_attempt_id),
    FOREIGN KEY (workspace_id, conversation_id, result_message_id)
        REFERENCES omnivia_chat_messages (workspace_id, conversation_id, message_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_chat_generation_attempt_outcomes (
    workspace_id              TEXT    NOT NULL,
    conversation_id           TEXT    NOT NULL,
    generation_job_id         TEXT    NOT NULL,
    generation_attempt_id     TEXT    NOT NULL,
    terminal_state            TEXT    NOT NULL,
    result_message_id         TEXT,
    provider_event_id         TEXT,
    retryable                 INTEGER NOT NULL,
    sanitized_error_code      TEXT,
    sanitized_error_detail    TEXT,
    schema_version            INTEGER NOT NULL,
    occurred_at_us            INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, generation_attempt_id),
    UNIQUE (workspace_id, generation_job_id, generation_attempt_id),
    UNIQUE (workspace_id, generation_job_id, provider_event_id),

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
    CHECK (terminal_state IN ('succeeded', 'failed', 'cancelled')),
    CHECK (result_message_id IS NULL OR (typeof(result_message_id) = 'text'
           AND length(result_message_id) BETWEEN 1 AND 128
           AND result_message_id GLOB '[A-Za-z0-9]*'
           AND result_message_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(result_message_id, char(0)) = 0)),
    CHECK (provider_event_id IS NULL OR (typeof(provider_event_id) = 'text'
           AND length(provider_event_id) BETWEEN 1 AND 128
           AND provider_event_id GLOB '[A-Za-z0-9]*'
           AND provider_event_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(provider_event_id, char(0)) = 0)),
    CHECK (retryable IN (0, 1)),
    CHECK (sanitized_error_code IS NULL OR (typeof(sanitized_error_code) = 'text'
           AND length(sanitized_error_code) BETWEEN 1 AND 128
           AND sanitized_error_code GLOB '[a-z]*'
           AND sanitized_error_code NOT GLOB '*[^a-z0-9._-]*')),
    CHECK (sanitized_error_detail IS NULL
           OR (typeof(sanitized_error_detail) = 'text'
               AND length(CAST(sanitized_error_detail AS BLOB)) <= 4096
               AND instr(sanitized_error_detail, char(0)) = 0)),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),
    CHECK (typeof(occurred_at_us) = 'integer' AND occurred_at_us > 0),
    CHECK ((terminal_state = 'succeeded'
            AND result_message_id IS NOT NULL
            AND retryable = 0
            AND sanitized_error_code IS NULL)
           OR terminal_state <> 'succeeded'),
    CHECK ((terminal_state IN ('failed', 'cancelled')
            AND result_message_id IS NULL
            AND sanitized_error_code IS NOT NULL)
           OR terminal_state NOT IN ('failed', 'cancelled')),

    FOREIGN KEY (workspace_id, conversation_id, generation_job_id)
        REFERENCES omnivia_chat_generation_jobs
            (workspace_id, conversation_id, generation_job_id),
    FOREIGN KEY (workspace_id, generation_job_id, generation_attempt_id)
        REFERENCES omnivia_chat_generation_attempts
            (workspace_id, generation_job_id, generation_attempt_id),
    FOREIGN KEY (workspace_id, conversation_id, result_message_id)
        REFERENCES omnivia_chat_messages (workspace_id, conversation_id, message_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_chat_generation_text_chunks (
    workspace_id              TEXT    NOT NULL,
    conversation_id           TEXT    NOT NULL,
    generation_job_id         TEXT    NOT NULL,
    generation_attempt_id     TEXT    NOT NULL,
    chunk_ordinal             INTEGER NOT NULL,
    provider_event_id         TEXT,
    text_content              TEXT    NOT NULL,
    content_hash              TEXT    NOT NULL,
    schema_version            INTEGER NOT NULL,
    occurred_at_us            INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, generation_job_id, generation_attempt_id, chunk_ordinal),
    UNIQUE (workspace_id, generation_job_id, provider_event_id),

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
    CHECK (typeof(chunk_ordinal) = 'integer' AND chunk_ordinal BETWEEN 0 AND 1000000),
    CHECK (provider_event_id IS NULL OR (typeof(provider_event_id) = 'text'
           AND length(provider_event_id) BETWEEN 1 AND 128
           AND provider_event_id GLOB '[A-Za-z0-9]*'
           AND provider_event_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(provider_event_id, char(0)) = 0)),
    CHECK (typeof(text_content) = 'text'
           AND length(CAST(text_content AS BLOB)) BETWEEN 1 AND 262144
           AND instr(text_content, char(0)) = 0),
    CHECK (typeof(content_hash) = 'text' AND length(content_hash) = 71
           AND substr(content_hash, 1, 7) = 'sha256:'
           AND substr(content_hash, 8) NOT GLOB '*[^0-9a-f]*'),
    CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),
    CHECK (typeof(occurred_at_us) = 'integer' AND occurred_at_us > 0),

    FOREIGN KEY (workspace_id, conversation_id, generation_job_id)
        REFERENCES omnivia_chat_generation_jobs
            (workspace_id, conversation_id, generation_job_id),
    FOREIGN KEY (workspace_id, generation_job_id, generation_attempt_id)
        REFERENCES omnivia_chat_generation_attempts
            (workspace_id, generation_job_id, generation_attempt_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_chat_queue_order_projection (
    workspace_id            TEXT    NOT NULL,
    conversation_id         TEXT    NOT NULL,
    queued_submission_id    TEXT    NOT NULL,
    queue_position          INTEGER NOT NULL,
    version                 INTEGER NOT NULL,
    updated_by_actor_id     TEXT    NOT NULL,
    updated_at_us           INTEGER NOT NULL,

    PRIMARY KEY (workspace_id, queued_submission_id),
    UNIQUE (workspace_id, conversation_id, queued_submission_id),
    UNIQUE (workspace_id, conversation_id, queue_position),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(conversation_id) = 'text'
           AND length(conversation_id) BETWEEN 1 AND 128
           AND conversation_id GLOB '[A-Za-z0-9]*'
           AND conversation_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(conversation_id, char(0)) = 0),
    CHECK (typeof(queued_submission_id) = 'text'
           AND length(queued_submission_id) BETWEEN 1 AND 128
           AND queued_submission_id GLOB '[A-Za-z0-9]*'
           AND queued_submission_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(queued_submission_id, char(0)) = 0),
    CHECK (typeof(queue_position) = 'integer'
           AND queue_position BETWEEN 1 AND 100000),
    CHECK (typeof(version) = 'integer' AND version >= 1),
    CHECK (typeof(updated_by_actor_id) = 'text'
           AND length(updated_by_actor_id) BETWEEN 1 AND 128
           AND updated_by_actor_id GLOB '[A-Za-z0-9]*'
           AND updated_by_actor_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(updated_by_actor_id, char(0)) = 0),
    CHECK (typeof(updated_at_us) = 'integer' AND updated_at_us > 0),

    FOREIGN KEY (workspace_id, conversation_id, queued_submission_id)
        REFERENCES omnivia_chat_queued_submissions
            (workspace_id, conversation_id, queued_submission_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_job_status_projection_state
    ON omnivia_chat_generation_job_status_projection
        (workspace_id, state, updated_at_us);

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_generation_text_chunks_attempt
    ON omnivia_chat_generation_text_chunks
        (workspace_id, generation_job_id, generation_attempt_id, chunk_ordinal);

CREATE INDEX IF NOT EXISTS omnivia_idx_chat_queue_order_projection_conversation
    ON omnivia_chat_queue_order_projection
        (workspace_id, conversation_id, queue_position);

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_job_status_projection_insert
BEFORE INSERT ON omnivia_chat_generation_job_status_projection
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_generation_job_status_projection')
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

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_job_status_projection_update
BEFORE UPDATE ON omnivia_chat_generation_job_status_projection
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_chat_generation_job_status_projection')
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
    SELECT RAISE(ABORT, 'omnivia: job status projection identity is immutable')
    WHERE NEW.workspace_id <> OLD.workspace_id
       OR NEW.conversation_id <> OLD.conversation_id
       OR NEW.generation_job_id <> OLD.generation_job_id
       OR NEW.schema_version <> OLD.schema_version;
    SELECT RAISE(ABORT, 'omnivia: job status projection version must advance by one')
    WHERE NEW.version <> OLD.version + 1;
    SELECT RAISE(ABORT, 'omnivia: job status projection transition is invalid')
    WHERE NOT ((OLD.state = 'queued'
                AND NEW.state IN ('queued', 'running', 'failed', 'cancelled'))
            OR (OLD.state = 'running'
                AND NEW.state IN ('running', 'retryable', 'succeeded', 'failed', 'cancelled'))
            OR (OLD.state = 'retryable'
                AND NEW.state IN ('retryable', 'running', 'failed', 'cancelled'))
            OR (OLD.state IN ('succeeded', 'failed', 'cancelled')
                AND NEW.state = OLD.state));
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_job_status_projection_delete
BEFORE DELETE ON omnivia_chat_generation_job_status_projection
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_generation_job_status_projection forbids DELETE');
END;

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

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_text_chunks_insert
BEFORE INSERT ON omnivia_chat_generation_text_chunks
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_generation_text_chunks')
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
    SELECT RAISE(ABORT, 'omnivia: generation text chunks must be contiguous from zero')
    WHERE NEW.chunk_ordinal IS NOT (
        SELECT COALESCE(MAX(chunk_ordinal), -1) + 1
        FROM omnivia_chat_generation_text_chunks
        WHERE workspace_id = NEW.workspace_id
          AND generation_job_id = NEW.generation_job_id
          AND generation_attempt_id = NEW.generation_attempt_id);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_text_chunks_update
BEFORE UPDATE ON omnivia_chat_generation_text_chunks
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_generation_text_chunks is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_generation_text_chunks_delete
BEFORE DELETE ON omnivia_chat_generation_text_chunks
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_generation_text_chunks forbids DELETE');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_queue_order_projection_insert
BEFORE INSERT ON omnivia_chat_queue_order_projection
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_chat_queue_order_projection')
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

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_queue_order_projection_update
BEFORE UPDATE ON omnivia_chat_queue_order_projection
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded UPDATE on omnivia_chat_queue_order_projection')
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
    SELECT RAISE(ABORT, 'omnivia: queue order projection identity is immutable')
    WHERE NEW.workspace_id <> OLD.workspace_id
       OR NEW.conversation_id <> OLD.conversation_id
       OR NEW.queued_submission_id <> OLD.queued_submission_id;
    SELECT RAISE(ABORT, 'omnivia: queue order projection version must advance by one')
    WHERE NEW.version <> OLD.version + 1;
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_chat_queue_order_projection_delete
BEFORE DELETE ON omnivia_chat_queue_order_projection
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_chat_queue_order_projection forbids DELETE');
END;
